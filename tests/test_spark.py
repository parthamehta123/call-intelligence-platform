"""Spark parity tests.

Two implementations of aggregation exist -- the single-node dict version
and the Spark shuffle version -- and that is only defensible if a test
pins them together. These run on local Spark; they are skipped when
pyspark is absent.

Run them in the isolated venv, NOT the base environment:

    .venv-spark/bin/python -m pytest tests/test_spark.py -q

Installing plain `pyspark` alongside `databricks-connect` breaks both:
databricks-connect ships its own `pyspark` package, and pip leaves the
loser's files orphaned in site-packages.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytest.importorskip("pyspark")
pytest.importorskip("pandas")

# `import pyspark` also succeeds when databricks-connect is installed -- it
# ships its own pyspark package -- but that build is a Spark Connect client
# with no local execution engine, so `master("local[2]")` would hang or
# fail. Detect it and skip rather than pretend.
if importlib.util.find_spec("pyspark.databricks") is not None:  # pragma: no cover
    pytest.skip(
        "databricks-connect provides pyspark here; run these in .venv-spark "
        "(see docs/DATABRICKS.md)", allow_module_level=True)

from dataclasses import asdict  # noqa: E402

from cip.config import SPARK  # noqa: E402
from cip.pipeline.aggregate import aggregate as aggregate_local  # noqa: E402
from cip.pipeline.reconcile import reconcile  # noqa: E402
from cip.schemas import CallRecord, Observation  # noqa: E402


@pytest.fixture(scope="module")
def spark(tmp_path_factory):
    import os
    import sys

    from pyspark.sql import SparkSession

    # Python workers are separate processes and do not inherit the driver's
    # sys.path. This is the local mirror of the Databricks packaging problem:
    # on a cluster the fix is to install the wheel as a job library, here it
    # is PYTHONPATH. Forgetting either gives `ModuleNotFoundError: cip`
    # inside the executor, never on the driver.
    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [src] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)

    warehouse = tmp_path_factory.mktemp("warehouse")
    session = (SparkSession.builder
               .appName("cip-tests")
               .master("local[2]")
               .config("spark.sql.shuffle.partitions", "4")
               .config("spark.sql.session.timeZone", "UTC")
               .config("spark.sql.warehouse.dir", str(warehouse))
               .getOrCreate())
    yield session
    session.stop()


def _obs(i: int, customer: str, **kw) -> Observation:
    base = dict(
        observation_id=f"O{i}", segment_id=f"S{i}", call_id=f"C{i}",
        customer_id=customer, product_id="X100", product_version="7.2",
        type="bug_report", issue_key="VPN_DISCONNECT", summary="VPN disconnects",
        severity="high", evidence="vpn drops", confidence=0.9, region="US",
        timestamp=f"2026-08-22T{i % 24:02d}:00:00+00:00", trust="derived",
        extractor="rules-v1")
    base.update(kw)
    return Observation(**base)


def _observations() -> list[Observation]:
    items = [_obs(i, f"U{i}") for i in range(30)]
    items += [_obs(100 + i, f"V{i}", summary="RESOLVED CLAIM: VPN disconnects",
                   severity="low") for i in range(9)]
    items += [_obs(200 + i, f"W{i}", issue_key="OVERHEATING", product_id="PULSE7",
                   summary="Device runs hot", severity="critical", region="EU",
                   product_version="1.9") for i in range(12)]
    # A repeat caller: many mentions, one customer.
    items += [_obs(300 + i, "U_repeat", issue_key="EXPORT_TIMEOUT",
                   product_id="MERIDIAN", summary="Export times out",
                   severity="medium", product_version=None) for i in range(20)]
    return items


def test_spark_aggregation_matches_single_node(spark):
    from cip.spark.aggregate import aggregate_observations
    from cip.spark.schemas import OBSERVATION

    observations = _observations()
    local = {(c.product_id, c.issue_key): c for c in aggregate_local(observations)}

    df = spark.createDataFrame([asdict(o) for o in observations], schema=OBSERVATION)
    distributed = {(r["product_id"], r["issue_key"]): r
                   for r in aggregate_observations(df).collect()}

    assert set(local) == set(distributed)
    for key, expected in local.items():
        actual = distributed[key]
        assert actual["mentions"] == expected.mentions, key
        assert actual["distinct_customers"] == expected.distinct_customers, key
        assert actual["severity"] == expected.severity, key
        assert actual["type"] == expected.type, key
        assert actual["summary"] == expected.summary, key
        assert sorted(actual["regions"]) == sorted(expected.regions), key
        assert sorted(v for v in actual["versions"] if v) == sorted(expected.versions), key
        assert actual["first_seen"] == expected.first_seen, key
        assert actual["last_seen"] == expected.last_seen, key
        assert abs(actual["mean_confidence"] - expected.mean_confidence) < 1e-6, key
        assert bool(actual["conflicts"]) == bool(expected.conflicts), key


def test_reconcile_decisions_match_across_engines(spark):
    from cip.spark.aggregate import aggregate_observations
    from cip.spark.job import _to_candidates
    from cip.spark.schemas import OBSERVATION

    observations = _observations()
    local = {(c.product_id, c.issue_key): c.decision
             for c in reconcile(aggregate_local(observations))}

    df = spark.createDataFrame([asdict(o) for o in observations], schema=OBSERVATION)
    candidates = reconcile(_to_candidates(aggregate_observations(df).collect()))
    distributed = {(c.product_id, c.issue_key): c.decision for c in candidates}

    assert local == distributed
    # The repeat caller must not be promoted on volume alone.
    assert distributed[("MERIDIAN", "EXPORT_TIMEOUT")] == "review"
    # The contradicted issue must go to a human.
    assert distributed[("X100", "VPN_DISCONNECT")] == "review"


def test_preprocess_stage_matches_single_node(spark):
    from cip.pipeline.preprocess import segment_call
    from cip.spark.schemas import CALL, SEGMENT
    from cip.spark.stages import preprocess_batches

    calls = [
        CallRecord(call_id="C1", customer_id="U1", timestamp="2026-08-22T10:00:00+00:00",
                   region="US", channel="voice", product_hint="X100",
                   turns=[{"speaker": "customer",
                           "text": "The VPN drops every ten minutes, call me on 415-555-0199",
                           "start_time": 0.0}]),
        CallRecord(call_id="C2", customer_id="U2", timestamp="2026-08-22T11:00:00+00:00",
                   region="EU", channel="chat", product_hint=None,
                   turns=[{"speaker": "customer", "text": "Hi, how are you?",
                           "start_time": 0.0}]),
    ]
    expected = [s for c in calls for s in segment_call(c)]

    df = spark.createDataFrame([asdict(c) for c in calls], schema=CALL)
    actual = df.mapInPandas(preprocess_batches, schema=SEGMENT).collect()

    assert len(actual) == len(expected)
    by_id = {r["segment_id"]: r for r in actual}
    for segment in expected:
        row = by_id[segment.segment_id]
        assert row["text"] == segment.text
        assert row["pii_redactions"] == segment.pii_redactions
        assert row["trust"] == "untrusted"
    # PII redaction must have happened before anything was materialised.
    assert all("415-555-0199" not in r["text"] for r in actual)


def test_global_dedupe_catches_cross_partition_duplicates(spark):
    from cip.spark.dedupe import dedupe_within_day
    from cip.spark.schemas import CALL, SEGMENT
    from cip.spark.stages import preprocess_batches

    text = "The X100 VPN keeps disconnecting"
    calls = [
        CallRecord(call_id=f"C{i}", customer_id="U1",
                   timestamp="2026-08-22T10:00:00+00:00", region="US",
                   channel="voice", product_hint="X100",
                   turns=[{"speaker": "customer", "text": text, "start_time": 0.0}])
        for i in range(6)
    ]
    df = spark.createDataFrame([asdict(c) for c in calls], schema=CALL).repartition(4)
    segments = df.mapInPandas(preprocess_batches, schema=SEGMENT)

    assert segments.count() == 6
    assert dedupe_within_day(segments).count() == 1


def test_day_partition_write_survives_column_reordering(spark, tmp_path):
    """A join reorders columns; `insertInto` matches by position.

    dedupe_across_days joins on (customer_id, content_hash), which hoists
    both to the front. Writing that frame positionally shifted every column
    and surfaced as "cannot cast speaker_mix STRING to MAP".
    """
    from pyspark.sql import functions as F

    from cip.config import SPARK
    from cip.spark import ddl
    from cip.spark.publish import write_day_partition

    SPARK.table_format = "parquet"
    SPARK.schema = "cip_reorder"
    ddl.create_all(spark, config=SPARK)

    rows = [{
        "segment_id": "S1", "call_id": "C1", "customer_id": "U1",
        "timestamp": "2026-08-22T10:00:00+00:00", "region": "US", "text": "hi",
        "speaker_mix": {"customer": 1}, "product_hint": "X100", "trust": "untrusted",
        "product_id": "X100", "product_confidence": 0.9, "relevance": 0.8,
        "pii_redactions": 0, "content_hash": "abc",
    }]
    from cip.spark.schemas import SEGMENT
    df = (spark.createDataFrame(rows, schema=SEGMENT)
          .withColumn("run_id", F.lit("R1"))
          .withColumn("day", F.lit("2026-08-22")))

    # Reorder exactly the way a join on (customer_id, content_hash) would.
    shuffled = df.select("customer_id", "content_hash",
                         *[c for c in df.columns
                           if c not in ("customer_id", "content_hash")])
    assert shuffled.columns != spark.table("cip_reorder.segments").columns

    write_day_partition(shuffled, table="cip_reorder.segments",
                        day="2026-08-22", config=SPARK)

    written = spark.table("cip_reorder.segments").collect()[0]
    assert written["speaker_mix"] == {"customer": 1}
    assert written["trust"] == "untrusted"
    assert written["segment_id"] == "S1"


def test_rerunning_the_same_day_is_not_eaten_by_cross_day_dedupe(spark, tmp_path):
    """The bug that shipped: a re-run reported SUCCESS with 0 segments.

    The first run records every hash for the day. If cross-day dedupe does
    not exclude the current day, the second run anti-joins the day against
    itself, drops all of it, and the job reports success having processed
    nothing -- indistinguishable from a genuinely quiet day.
    """
    from pyspark.sql import functions as F

    from cip.config import SPARK
    from cip.spark import ddl
    from cip.spark.dedupe import dedupe_across_days, dedupe_within_day, record_seen
    from cip.spark.schemas import SEGMENT

    SPARK.table_format = "parquet"
    SPARK.schema = "cip_rerun"
    ddl.create_all(spark, config=SPARK)
    seen_table = "cip_rerun.seen_segments"
    day = "2026-08-22"

    rows = [{
        "segment_id": f"S{i}", "call_id": f"C{i}", "customer_id": f"U{i}",
        "timestamp": "2026-08-22T10:00:00+00:00", "region": "US",
        "text": "customer: the VPN keeps dropping", "speaker_mix": {"customer": 1},
        "product_hint": "X100", "trust": "untrusted", "product_id": None,
        "product_confidence": 0.0, "relevance": 0.0, "pii_redactions": 0,
        "content_hash": f"hash{i}",
    } for i in range(5)]
    segments = spark.createDataFrame(rows, schema=SEGMENT)

    first = dedupe_across_days(dedupe_within_day(segments), spark=spark, day=day,
                               seen_table=seen_table)
    assert first.count() == 5
    record_seen(first, day=day, spark=spark, seen_table=seen_table, config=SPARK)

    # Same day again: must reprocess in full, because the partition-replacing
    # writers downstream assume a complete day.
    second = dedupe_across_days(dedupe_within_day(segments), spark=spark, day=day,
                                seen_table=seen_table)
    assert second.count() == 5, "re-running the same day must not self-cancel"

    # A genuinely later day must still be deduplicated against this one.
    later = dedupe_across_days(dedupe_within_day(segments), spark=spark,
                               day="2026-08-23", seen_table=seen_table)
    assert later.count() == 0, "cross-day dedupe must still work"


def test_cross_module_spark_calls_are_keyword_only():
    """Structural guard against the bug class that produced three defects.

    Three separate failures had one shape: a function grew a parameter and a
    caller kept passing positionally.

      publish_candidates gained `day`  -> config bound to day, and Delta got
                                          replaceWhere: day = 'SparkConfig(...)'
      dedupe_across_days gained `day`  -> defaulted to None, the filter
                                          switched off, and a re-run reported
                                          SUCCESS having processed 0 segments
      record_seen        gained `config` -> ignored, module global used instead

    Tests caught none of them; the cluster did. Keyword-only arguments make
    the mistake impossible to express, so this asserts the convention rather
    than trusting review to enforce it.
    """
    import inspect

    from cip.spark import audit_sink, ddl, dedupe, job, publish

    # (function, how many leading positional "subject" params are allowed)
    contracts = [
        (publish.write_day_partition, 1),   # the DataFrame
        (publish.write_evidence, 1),
        (publish.publish_candidates, 1),    # the spark session
        (dedupe.dedupe_across_days, 1),
        (dedupe.record_seen, 1),
        (ddl.create_all, 1),
        (audit_sink.flush, 1),
        (job.run, 1),                       # the day
        (job._table, 1),
        (job._write_metrics, 1),
    ]

    offenders = []
    for fn, allowed in contracts:
        params = list(inspect.signature(fn).parameters.values())
        positional = [p for p in params
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if len(positional) > allowed:
            offenders.append(
                f"{fn.__module__}.{fn.__qualname__} accepts "
                f"{len(positional)} positional args ({[p.name for p in positional]}); "
                f"only the first {allowed} may be positional")
    assert not offenders, "\n".join(offenders)
