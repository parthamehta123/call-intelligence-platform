"""Partition functions -- the part that genuinely is a straight lift.

Each function takes an iterator of pandas frames and yields pandas frames.
The bodies call the same single-node functions in ``cip.pipeline`` that
the local runner and the unit tests use, so there is no second copy of the
domain logic to keep in sync.

**Why ``mapInPandas`` and not ``mapPartitions``.** The RDD API is not
available on Spark Connect, which is what ``databricks-connect`` 13+ and
serverless compute speak. ``df.rdd.mapPartitions(...)`` fails there with
no RDD attribute. ``mapInPandas`` has the same
partition-in / partition-out contract, runs on classic clusters, Connect
and serverless alike, and moves data over Arrow instead of pickle.

Two things did have to change for the cluster, and neither is cosmetic:

  * dedupe moved out. The single-node version holds a ``seen`` set, which
    here would deduplicate only *within a partition* (see ``dedupe.py``).
  * metrics moved out of a ``Counter`` into real aggregates. A Counter on
    an executor is discarded, and an accumulator double-counts whenever a
    task is retried or speculatively re-run.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Iterator

import pandas as pd

from ..pipeline.extract import extract
from ..pipeline.preprocess import segment_call
from ..pipeline.route import route
from ..schemas import CallRecord, Segment
from .schemas import OBSERVATION, SEGMENT

SEGMENT_COLUMNS = [f.name for f in SEGMENT.fields]
OBSERVATION_COLUMNS = [f.name for f in OBSERVATION.fields]


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def _null(value):
    """pandas represents a SQL NULL in an object column as float NaN, not None.

    Left unhandled, a null `product_hint` arrives as `nan`, flows through
    entity resolution as the product id, and blows up in schema validation
    with `expected string, got float`. Every nullable column crossing the
    Arrow boundary goes through here.
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    return None if value is pd.NaT or value is pd.NA else value


def _num(value, default: float) -> float:
    """Numeric coercion that survives partitions written before a column
    existed. `or default` would be wrong: 0.0 is a meaningful
    attribution_confidence (no customer turns at all), not a missing value."""
    value = _null(value)
    return default if value is None else float(value)


def _frame(records: list[dict], columns: list[str]) -> pd.DataFrame:
    # An empty partition still has to yield a correctly-shaped frame, or
    # Spark fails the whole stage on schema mismatch rather than the task.
    return pd.DataFrame(records, columns=columns) if records else pd.DataFrame(columns=columns)


def preprocess_batches(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    """Raw calls -> PII-redacted, segmented, untrusted-stamped segments."""
    for batch in batches:
        records: list[dict] = []
        for row in batch.to_dict("records"):
            turns = row.get("turns")
            call = CallRecord(
                call_id=row["call_id"],
                customer_id=row["customer_id"],
                timestamp=row["timestamp"],
                region=row["region"],
                channel=row["channel"],
                product_hint=_null(row.get("product_hint")),
                turns=[dict(t) for t in (turns if turns is not None else [])],
            )
            for segment in segment_call(call):
                record = asdict(segment)
                record["content_hash"] = _content_hash(segment.text)
                records.append(record)
        yield _frame(records, SEGMENT_COLUMNS)


def make_route_and_extract(*, extractor: str, extract_limit: int = 0,
                           api_key: str | None = None,
                           claude_model: str | None = None,
                           extract_effort: str | None = None):
    """Build the partition function with configuration captured in a closure.

    Executors are separate processes. They re-import `cip.config`, which
    reads *their* environment, so anything the driver set at runtime --
    `os.environ["CIP_EXTRACTOR"]`, a mutated CONFIG -- is simply not there.
    A job configured for Claude therefore ran the rules extractor on every
    worker and reported success, with the wrong backend recorded in the
    `extractor` column as the only trace.

    Closures are serialised to the workers, so values captured here do
    arrive. The driver's preflight proves the secret exists; this is what
    makes the workers use it.
    """
    def run(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
        import os

        from ..config import CONFIG

        CONFIG.extractor = extractor
        CONFIG.extract_limit = extract_limit
        if claude_model:
            CONFIG.claude_model = claude_model
        if extract_effort:
            CONFIG.extract_effort = extract_effort
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        yield from route_and_extract_batches(batches)

    return run


def route_and_extract_batches(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    """Funnel + schema-constrained extraction in a single pass.

    Fusing the two means a segment the router discards is never handed to
    the extractor at all. At 10 TB the ~70% the router drops is the bulk of
    the data, and this is exactly where it stops costing money.
    """
    for batch in batches:
        segments = [
            Segment(
                segment_id=row["segment_id"], call_id=row["call_id"],
                customer_id=row["customer_id"], timestamp=row["timestamp"],
                region=row["region"], text=row["text"],
                speaker_mix=dict(_null(row["speaker_mix"]) or {}),
                customer_turns=int(_num(row.get("customer_turns"), 0)),
                # Missing means the partition predates diarization capture, so
                # it is trusted -- consistent with preprocessing treating an
                # absent per-turn speaker_confidence as 1.0.
                attribution_confidence=_num(row.get("attribution_confidence"), 1.0),
                product_hint=_null(row.get("product_hint")), trust=row["trust"],
                pii_redactions=int(row["pii_redactions"]),
            )
            for row in batch.to_dict("records")
        ]
        records = [asdict(o) for o in extract(route(segments))]
        yield _frame(records, OBSERVATION_COLUMNS)


def score_relevance_batches(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    """Router only -- used when you want funnel metrics without paying for
    extraction, e.g. tuning the threshold against a labelled sample."""
    for batch in batches:
        records = [asdict(s) for s in route([
            Segment(
                segment_id=row["segment_id"], call_id=row["call_id"],
                customer_id=row["customer_id"], timestamp=row["timestamp"],
                region=row["region"], text=row["text"],
                speaker_mix=dict(_null(row["speaker_mix"]) or {}),
                customer_turns=int(_num(row.get("customer_turns"), 0)),
                # Missing means the partition predates diarization capture, so
                # it is trusted -- consistent with preprocessing treating an
                # absent per-turn speaker_confidence as 1.0.
                attribution_confidence=_num(row.get("attribution_confidence"), 1.0),
                product_hint=_null(row.get("product_hint")), trust=row["trust"],
                pii_redactions=int(row["pii_redactions"]),
            )
            for row in batch.to_dict("records")
        ])]
        for record in records:
            record["content_hash"] = _content_hash(record["text"])
        yield _frame(records, SEGMENT_COLUMNS)
