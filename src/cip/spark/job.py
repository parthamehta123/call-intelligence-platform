"""Daily Databricks job.

Shape of the run:

    read raw (schema-on-read, partition pruned)
      -> mapInPandas: preprocess          [distributed]
      -> shuffle: global dedupe           [distributed]
      -> mapInPandas: route + extract     [distributed]
      -> write evidence (append)          [distributed]
      -> shuffle: aggregate               [distributed]
      -> collect candidates               [driver -- thousands of rows]
      -> reconcile + declassify + publish [driver, policy-gated]

The collect is deliberate and is the point of the whole aggregation stage:
10 TB of calls reduce to a few thousand issue candidates, and decision
logic on a few thousand rows belongs on the driver where it is ordinary,
testable Python. Distributing the *decisions* would buy nothing and would
put the policy engine on 200 executors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone

from pyspark.sql import functions as F

from ..config import CONFIG, SPARK
from ..pipeline.reconcile import reconcile
from ..schemas import IssueCandidate
from ..security.audit import audit
from . import audit_sink, ddl, publish
from .aggregate import aggregate_observations
from .dedupe import dedupe_across_days, dedupe_within_day, record_seen
from .schemas import CALL, OBSERVATION, ROUTE_DECISION, SEGMENT
from .session import get_spark
from .stages import (make_route_and_extract, preprocess_batches,
                     route_decisions_batches)


def _run_id(day: str) -> str:
    stamp = datetime.now(timezone.utc).isoformat()
    return "R" + hashlib.sha1(f"{day}:{stamp}".encode()).hexdigest()[:10]


def _table(name: str, *, config=SPARK) -> str:
    return config.table(name) if config.table_format == "delta" \
        else f"{config.schema}.{name}"


def _to_candidates(rows) -> list[IssueCandidate]:
    return [
        IssueCandidate(
            product_id=r["product_id"], issue_key=r["issue_key"], type=r["type"],
            summary=r["summary"], severity=r["severity"], mentions=int(r["mentions"]),
            distinct_customers=int(r["distinct_customers"]),
            regions=list(r["regions"] or []),
            versions=[v for v in (r["versions"] or []) if v],
            first_seen=r["first_seen"], last_seen=r["last_seen"],
            mean_confidence=float(r["mean_confidence"]),
            evidence_ids=list(r["evidence_ids"] or []),
            conflicts=list(r["conflicts"] or []),
        )
        for r in rows
    ]


def run(day: str, *, raw_path: str | None = None, config=SPARK, spark=None,
        write_seen: bool = True) -> dict:
    spark = spark or get_spark()
    run_id = _run_id(day)
    ddl.create_all(spark, config=config)
    audit_mark = audit_sink.line_count()
    audit.write("spark_run_started", run_id=run_id, day=day,
                extractor=CONFIG.extractor, table_format=config.table_format)

    raw_path = raw_path or config.raw_path
    # Explicit glob, not the bare directory. The day directory also holds
    # `_MANIFEST.json` and the eval sidecar `_LABELS.jsonl`; reading the
    # directory wholesale parses those as calls, which silently doubles the
    # call count and injects rows with no turns.
    calls = (spark.read.schema(CALL)
             .json(f"{raw_path}/date={day}/region=*/centre=*/part-*.jsonl")
             .repartition(config.shuffle_partitions))
    call_count = calls.count()

    # --- silver: redacted, segmented, globally deduplicated ----------------
    # Materialised rather than cached. `.cache()` is rejected on serverless
    # (NOT_SUPPORTED_WITH_SERVERLESS: PERSIST TABLE), and without it every
    # downstream action would re-run preprocessing and, worse, extraction.
    # Landing each stage as a Delta table is the medallion shape anyway, and
    # it buys replay: a bad extractor can be re-run from silver without
    # re-reading the raw day.
    segments_table = _table("segments", config=config)
    raw_segments = calls.mapInPandas(preprocess_batches, schema=SEGMENT)
    deduped = dedupe_across_days(
        dedupe_within_day(raw_segments), spark=spark, day=day,
        seen_table=_table("seen_segments", config=config))
    publish.write_day_partition(
        deduped.withColumn("run_id", F.lit(run_id)).withColumn("day", F.lit(day)),
        table=segments_table, day=day, config=config)

    silver = spark.table(segments_table).filter(F.col("day") == day)
    segment_count, pii = silver.agg(
        F.count("*"), F.coalesce(F.sum("pii_redactions"), F.lit(0))).collect()[0]

    # Calls in, nothing out, exit code zero: the worst outcome available to a
    # daily job, because it is indistinguishable from a quiet day. Fail here
    # instead of publishing an empty knowledge base and calling it a success.
    if call_count and not segment_count:
        raise RuntimeError(
            f"{call_count} calls produced 0 segments for {day}. Likely causes: "
            f"every hash already recorded in {_table('seen_segments', config=config)} "
            f"for an earlier day, or a preprocessing failure. Refusing to "
            f"report success on an empty run.")

    # --- routing decisions ---------------------------------------------------
    # Runs before extraction and over the *uncapped* segments, deliberately.
    # `extract_limit` is a spend cap on model calls; letting it also bound
    # this pass would mean a cheap capped run inspected less of the day than
    # a full one, which is the wrong thing to make cheaper. It calls no model.
    decisions_table = _table("route_decisions", config=config)
    decisions = silver.select(*[f.name for f in SEGMENT.fields]).mapInPandas(
        route_decisions_batches, schema=ROUTE_DECISION)
    publish.write_day_partition(
        decisions.withColumn("run_id", F.lit(run_id)).withColumn("day", F.lit(day)),
        table=decisions_table, day=day, config=config)
    route_decisions = spark.table(decisions_table).filter(F.col("day") == day)

    injections = route_decisions.filter(F.col("injection_signatures") != "")
    injection_count = injections.count()
    inspection_only = injections.filter(~F.col("reached_extraction")).count()
    # The exact number of segments handed to the extractor, and therefore
    # the exact number of metered calls. Read from here rather than counted
    # from observation rows, which miss every call that returned no signal.
    segments_to_model = route_decisions.filter(F.col("reached_extraction")).count()
    print(f"[cip] injections detected {injection_count} "
          f"({inspection_only} forwarded for inspection only)")

    # --- extraction --------------------------------------------------------
    observations_table = _table("observations", config=config)
    to_extract = silver.select(*[f.name for f in SEGMENT.fields])

    # A global cap, applied before the work fans out. CONFIG.extract_limit
    # alone is per-partition: 50 across 200 partitions is up to 10,000 model
    # calls, not 50, which is the opposite of what a spend cap is for.
    if CONFIG.extract_limit:
        to_extract = to_extract.limit(CONFIG.extract_limit)
        print(f"[cip] extract_limit={CONFIG.extract_limit} segments "
              f"(a capped run -- published counts will be partial)")

    # Configuration is captured in the closure, not read from the worker's
    # environment -- see make_route_and_extract.
    import os

    extract_fn = make_route_and_extract(
        extractor=CONFIG.extractor, extract_limit=CONFIG.extract_limit,
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        claude_model=CONFIG.claude_model,
        extract_effort=CONFIG.extract_effort)
    extracted = to_extract.mapInPandas(extract_fn, schema=OBSERVATION)
    publish.write_day_partition(
        extracted.withColumn("run_id", F.lit(run_id)).withColumn("day", F.lit(day)),
        table=observations_table, day=day, config=config)

    observations = spark.table(observations_table).filter(F.col("day") == day)
    observation_count = observations.count()

    # The backend that actually ran, read back from what the workers wrote.
    # A driver configured for Claude whose executors quietly used the rules
    # extractor produces a plausible, complete-looking run -- this is the
    # only place that discrepancy is visible.
    backends = [r["extractor"] for r in
                observations.select("extractor").distinct().collect()]
    if CONFIG.extractor == "claude" and not observation_count:
        raise RuntimeError(
            "extractor=claude produced 0 observations from "
            f"{to_extract.count()} segments. Every model call failed. Check "
            "the API key in the secret scope and the account's credit "
            "balance; refusing to report an empty run as a success.")
    if CONFIG.extractor == "claude" and any(b.startswith("rules") for b in backends):
        raise RuntimeError(
            f"extractor=claude was requested but workers wrote {backends}. "
            f"Executor configuration did not reach the partition function; "
            f"refusing to report a run made by a different backend.")

    # Summed from the rows the workers wrote, which is the only place a
    # per-call figure from an executor survives.
    # Tokens are only ever carried on observation rows, so a call that
    # returned no signal contributes none. `rows_with_usage` is therefore
    # the count of *attributed* calls, not the count of calls made --
    # `segments_to_model` above is that. The difference is reported rather
    # than hidden, and the spend below is explicitly a lower bound.
    token_totals = observations.agg(
        F.coalesce(F.sum("input_tokens"), F.lit(0)),
        F.coalesce(F.sum("output_tokens"), F.lit(0)),
        F.sum(F.when(F.col("input_tokens") > 0, 1).otherwise(0))).collect()[0]

    evidence_rows = publish.write_evidence(
        observations, run_id=run_id, day=day, config=config)

    candidates_df = aggregate_observations(observations)
    candidates = reconcile(_to_candidates(candidates_df.collect()))
    outcome = publish.publish_candidates(
        spark, candidates=candidates, run_id=run_id, day=day, config=config)

    if write_seen:
        record_seen(silver, day=day, spark=spark,
                    seen_table=_table("seen_segments", config=config),
                    config=config)

    stats = {
        "run_id": run_id,
        "extract_limit": CONFIG.extract_limit,
        "day": day,
        "calls": call_count,
        # Post-dedupe: what silver actually holds. There is deliberately no
        # pre-dedupe count -- obtaining one means running preprocessing a
        # second time over the full day, and a metric is not worth that.
        "segments_landed": segment_count,
        "pii_redactions": int(pii),
        "injections_detected": injection_count,
        "injections_inspection_only": inspection_only,
        "observations": observation_count,
        "evidence_rows": evidence_rows,
        "candidates": len(candidates),
        **outcome,
    }

    from ..pricing import estimate

    rows_with_usage = int(token_totals[2] or 0)
    spend = estimate(CONFIG.claude_model, rows_with_usage,
                     int(token_totals[0] or 0), int(token_totals[1] or 0))
    if spend.calls:
        # `model_calls` is what was actually billed; `calls_with_usage` is
        # what we could measure. Reporting only the second one understated
        # the first Claude day's spend by 34 calls.
        unattributed = max(0, segments_to_model - rows_with_usage)
        stats.update(model_calls=segments_to_model,
                     calls_with_usage=rows_with_usage,
                     calls_without_usage=unattributed,
                     input_tokens=spend.input_tokens,
                     output_tokens=spend.output_tokens,
                     estimated_usd=round(spend.usd, 4) if spend.usd is not None else None)
        print(spend.render())
        if unattributed:
            per_call = ((spend.usd or 0.0) / rows_with_usage
                        if rows_with_usage else 0.0)
            print(f"[cip] {unattributed} of {segments_to_model} calls returned "
                  f"no observation and carry no usage; measured spend is a "
                  f"LOWER BOUND. At the measured mean they add about "
                  f"${per_call * unattributed:.2f}.")
    _write_metrics(spark, stats=stats, config=config)
    audit.write("spark_run_finished", **stats)
    stats["audit_rows"] = audit_sink.flush(
        spark, run_id=run_id, day=day, since=audit_mark, config=config)
    return stats


def _write_metrics(spark, *, stats: dict, config=SPARK) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {"run_id": stats["run_id"], "day": stats["day"], "metric": key,
         "value": float(value), "recorded_at": now}
        for key, value in stats.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not rows:
        return
    table = _table("run_metrics", config=config)
    spark.createDataFrame(rows, schema=spark.table(table).schema) \
         .write.mode("append").insertInto(table)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cip-spark")
    parser.add_argument("--day", required=True)
    parser.add_argument("--raw-path", default=None)
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--schema", default=None)
    parser.add_argument("--table-format", default=None)
    args = parser.parse_args(argv)

    if args.catalog:
        SPARK.catalog = args.catalog
    if args.schema:
        SPARK.schema = args.schema
    if args.table_format:
        SPARK.table_format = args.table_format

    stats = run(args.day, raw_path=args.raw_path)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
