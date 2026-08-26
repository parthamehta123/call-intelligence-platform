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

from ..config import CONFIG
from ..pipeline.extract import LEDGER, extract
from ..pipeline.preprocess import segment_call
from ..pipeline.route import for_extraction, route
from ..schemas import CallRecord, Segment
from .schemas import EXTRACTION, OBSERVATION, ROUTE_DECISION, SEGMENT

SEGMENT_COLUMNS = [f.name for f in SEGMENT.fields]
OBSERVATION_COLUMNS = [f.name for f in OBSERVATION.fields]
EXTRACTION_COLUMNS = [f.name for f in EXTRACTION.fields]
ROUTE_DECISION_COLUMNS = [f.name for f in ROUTE_DECISION.fields]


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


def _frame(records: list[dict], schema) -> pd.DataFrame:
    """Records -> a frame Arrow will accept against `schema`.

    Takes the schema, not a column list, because the column *types* are the
    part that goes wrong. A record missing a key becomes NaN, which is a
    float: in a StringType column Arrow rejects it, and in an IntegerType
    column the whole column silently becomes float64. Both fail only at
    serialisation, on the cluster, minutes into a run -- pandas holds them
    without complaint, so nothing local notices.

    Usage-only rows leave every observation column unset, so this is the
    ordinary case rather than an edge one. Each column is built as `object`
    holding native Python values or None, and Spark applies the declared
    types on conversion.
    """
    columns = [f.name for f in schema.fields]
    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        {c: pd.Series([r.get(c) for r in records], dtype=object)
         for c in columns},
        columns=columns)


def _segment_row(segment: Segment) -> dict:
    """`Segment` -> a row matching SEGMENT, with list fields flattened.

    `injection_signatures` is a list on the dataclass and a comma-joined
    string in the table. `asdict()` does not know that, so every stage that
    built a row by hand shipped a Python list into a StringType column --
    which pandas accepts as dtype `object`, and Arrow rejects only on the
    cluster, several minutes into a run. Flattened here so there is one
    place for the conversion and one place to update when a field is added.
    """
    record = asdict(segment)
    record["injection_signatures"] = ",".join(segment.injection_signatures)
    record["content_hash"] = _content_hash(segment.text)
    return record


def _segments_from(batch: pd.DataFrame) -> list[Segment]:
    """Rebuild `Segment`s from a silver batch.

    Shared by all three stages that route: three copies of this constructor
    is three places for a new field to be forgotten, and a field missing
    here is silently defaulted rather than raised.
    """
    return [
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
            records += [_segment_row(s) for s in segment_call(call)]
        yield _frame(records, SEGMENT)


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
        segments = _segments_from(batch)
        # `for_extraction` drops the security-only segments the router
        # forwards for inspection. Without it the executor would pay a
        # model to read every injection payload in the partition.
        observations = list(extract(for_extraction(route(segments))))
        records = [dict(asdict(o), produced_observation=True, call_failed=False)
                   for o in observations]

        # Every metered call that returned nothing. With the rules
        # extractor the ledger is empty and this adds no rows at all.
        #
        # Filtered on the call's OWN flag, not on whether this batch
        # produced an observation for that segment. The ledger is
        # module-level, and an executor runs several tasks in one process,
        # so a batch can drain a call belonging to a batch still running.
        # Cross-referencing this batch's observations mislabelled those as
        # abstentions -- 2 of 291 on a metered run, which overstated the
        # abstention count while leaving spend correct. The call itself
        # always knows whether it produced an observation.
        for call in LEDGER.drain():
            if call.produced_observation:
                # Its tokens ride on the observation row, wherever that row
                # is emitted from.
                continue
            # A usage-only row: every observation column stays null, which
            # is why the stage emits EXTRACTION rather than OBSERVATION.
            # The driver projects observations out of the non-null rows.
            records.append({
                "segment_id": call.segment_id,
                "extractor": CONFIG.claude_model,
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                "cache_read_input_tokens": call.cache_read_input_tokens,
                "produced_observation": False,
                "call_failed": call.failed,
            })
        yield _frame(records, EXTRACTION)


def score_relevance_batches(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    """Router only -- used when you want funnel metrics without paying for
    extraction, e.g. tuning the threshold against a labelled sample."""
    for batch in batches:
        records = [_segment_row(s) for s in route(_segments_from(batch))]
        yield _frame(records, SEGMENT)


def route_decisions_batches(batches: Iterator[pd.DataFrame]) -> Iterator[pd.DataFrame]:
    """Every routing decision, as its own pass over the segments.

    `mapInPandas` yields exactly one schema, so the fused route+extract
    stage can emit observations and nothing else. Two things that are not
    observations still have to reach the driver:

    * an **injection** forwarded for inspection, which never becomes an
      Observation and would otherwise leave no cluster-side trace at all;
    * the **number of segments sent to the model**, which is the call count.
      Deriving it from observation rows undercounts by every call that
      returned no signal -- 34 of 1225 on the first full Claude day, a 3%
      understatement of spend that grows as the extractor abstains more.

    Both fall out of one row per kept segment. The security channel is this
    filtered to a non-empty signature; the call count is this filtered to
    `reached_extraction`.

    The pass is pure CPU -- no model call, no network -- which is what makes
    re-running the router cheaper than widening the extraction output.
    """
    for batch in batches:
        rows = []
        for segment in route(_segments_from(batch)):
            rows.append({
                "segment_id": segment.segment_id, "call_id": segment.call_id,
                "customer_id": segment.customer_id,
                "timestamp": segment.timestamp, "region": segment.region,
                "route_reason": segment.route_reason,
                "injection_signatures": ",".join(segment.injection_signatures),
                "relevance": float(segment.relevance),
                "reached_extraction": segment.route_reason != "injection",
            })
        yield _frame(rows, ROUTE_DECISION)
