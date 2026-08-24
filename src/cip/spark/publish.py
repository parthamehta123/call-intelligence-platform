"""Publication on the cluster -- the security boundary, storage-swapped.

The interesting property here: nothing about the trust model changed when
the storage went from SQLite to Delta. Declassification and the policy
engine sit *in front of* the tool, not inside it, so swapping the executor
underneath leaves every control intact.

Each candidate is authorised individually (so a denial names the exact
product and issue), then the approved set is written in one MERGE rather
than one MERGE per row -- thousands of single-row MERGEs against a Delta
table is a well-known way to spend an hour rewriting the same files.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterable

from pyspark.sql import DataFrame, functions as F

from ..config import SPARK
from ..schemas import IssueCandidate
from ..security.audit import audit
from ..security.declassify import DeclassificationRefused, declassify_candidate
from ..security.policy import ENGINE, Capability, PolicyViolation, ToolCall, guarded_tool
from ..tools import WRITER_SERVICE
import re

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")


def write_day_partition(df, table: str, day: str, config) -> None:
    """Replace exactly one day's partition.

    A daily job is retried. Blind `append` makes a retry double every
    evidence row, which silently inflates the very corroboration counts the
    reconciliation thresholds depend on. Replacing the partition makes the
    run idempotent: the same day processed twice yields the same table.

    Two different APIs, for a real reason. `saveAsTable("overwrite")` drops
    and recreates the table, which throws away the DDL and ignores dynamic
    partition overwrite entirely. Delta gets `replaceWhere`; everything else
    gets `insertInto`, which writes into the *existing* table positionally
    and honours `partitionOverwriteMode=dynamic`.
    """
    # Align to the table's own column order before writing. A join hoists its
    # keys to the front of the DataFrame -- dedupe_across_days joins on
    # (customer_id, content_hash), so `deduped` arrives with those two columns
    # first. `insertInto` matches BY POSITION, so that silently shifts every
    # column by two and Spark reports it as a bogus type error
    # ("cannot cast speaker_mix STRING to MAP"). Selecting by name makes the
    # write independent of upstream column order, and raises a clear
    # unresolved-column error if a column is genuinely missing.
    df = df.select(*df.sparkSession.table(table).columns)

    if config.table_format == "delta":
        (df.write.mode("overwrite").format("delta")
           .option("replaceWhere", f"day = '{day}'")
           .partitionBy("day").saveAsTable(table))
    else:
        df.sparkSession.conf.set("spark.sql.sources.partitionOverwriteMode", "dynamic")
        # insertInto matches columns by position, so the partition column
        # must be last -- which is how every caller selects it.
        df.write.mode("overwrite").insertInto(table)


@guarded_tool(Capability(
    name="commit_issue_batch",
    effect="write",
    risk="high",
    roles=frozenset({WRITER_SERVICE}),
    accepts_untrusted_args=False,
    arg_validators={
        # The table name is interpolated into a MERGE statement, so it is
        # validated as a SQL identifier. This is the one place where a
        # string reaches the SQL parser, and it never comes from a call.
        "table": lambda v: bool(_IDENT.match(str(v))),
        "row_count": lambda v: isinstance(v, int) and v >= 0,
    },
))
def commit_issue_batch(*, spark, table: str, candidates: list, run_id: str,
                       row_count: int, table_format: str) -> str:
    if not candidates:
        return "no candidates to publish"

    now = datetime.now(timezone.utc)
    rows = [
        {
            "product_id": c.product_id, "issue_key": c.issue_key, "type": c.type,
            "summary": c.summary, "severity": c.severity, "status": "observed",
            "mentions": int(c.mentions), "customers": int(c.distinct_customers),
            "regions": list(c.regions), "versions": list(c.versions),
            "first_seen": c.first_seen, "last_seen": c.last_seen,
            "confidence": float(c.mean_confidence), "updated_at": now, "run_id": run_id,
        }
        for c in candidates
    ]
    updates = spark.createDataFrame(rows)
    updates.createOrReplaceTempView("_cip_issue_updates")

    if table_format == "delta":
        spark.sql(f"""
            MERGE INTO {table} AS t
            USING _cip_issue_updates AS s
              ON t.product_id = s.product_id AND t.issue_key = s.issue_key
            WHEN MATCHED THEN UPDATE SET
                t.type = s.type, t.summary = s.summary, t.severity = s.severity,
                -- an engineering-confirmed issue is never demoted by customer
                -- chatter, however many callers disagree
                t.status = CASE WHEN t.status = 'confirmed' THEN 'confirmed'
                                ELSE s.status END,
                t.mentions = s.mentions, t.customers = s.customers,
                t.regions = s.regions, t.versions = s.versions,
                t.first_seen = LEAST(t.first_seen, s.first_seen),
                t.last_seen = GREATEST(t.last_seen, s.last_seen),
                t.confidence = s.confidence, t.updated_at = s.updated_at,
                t.run_id = s.run_id
            WHEN NOT MATCHED THEN INSERT *
        """)
    else:
        # Vanilla PySpark has no MERGE, so this dev-only path does
        # read-modify-write. Acceptable only because `issues` is small
        # (thousands of rows); it would be flatly wrong for `evidence`,
        # which is append-only for exactly this reason.
        existing = spark.table(table)
        schema = existing.schema
        keep = existing.join(updates.select("product_id", "issue_key"),
                             ["product_id", "issue_key"], "left_anti")
        confirmed = existing.filter(F.col("status") == "confirmed").select(
            "product_id", "issue_key")
        merged = updates.join(confirmed, ["product_id", "issue_key"], "left_semi") \
            .withColumn("status", F.lit("confirmed")) \
            .unionByName(updates.join(confirmed, ["product_id", "issue_key"], "left_anti"))
        combined = keep.unionByName(merged.select(existing.columns))
        # Spark refuses to overwrite a table that is also a source in the
        # same plan. Materialising cuts the lineage; safe at this row count.
        rows = combined.collect()
        spark.createDataFrame(rows, schema=schema) \
             .write.mode("overwrite").format(table_format).saveAsTable(table)

    return f"published {len(candidates)} issues to {table}"


def authorize(candidate: IssueCandidate) -> IssueCandidate | None:
    """Per-candidate gate: declassify, then re-check against the same
    capability the single-node writer uses. Returns None if refused."""
    try:
        validated = declassify_candidate(candidate, [])
    except DeclassificationRefused:
        return None
    decision = ENGINE.evaluate(ToolCall(
        "publish_issue_update",
        {"product_id": validated.product_id, "issue_key": validated.issue_key},
        WRITER_SERVICE,
        purpose="cluster publication",
    ))
    return validated if decision.allowed else None


def publish_candidates(spark, candidates: Iterable[IssueCandidate], run_id: str,
                       day: str, config=SPARK) -> dict:
    approved, to_review, rejected = [], [], []
    for candidate in candidates:
        validated = authorize(candidate)
        if validated:
            approved.append(validated)
        elif candidate.decision == "reject":
            # Below the confidence floor. Evidence is retained so the
            # decision is auditable, but a human queue full of noise is a
            # queue nobody reads.
            rejected.append(candidate)
        else:
            to_review.append(candidate)

    table = config.table("issues") if config.table_format == "delta" \
        else f"{config.schema}.issues"
    try:
        commit_issue_batch(role=WRITER_SERVICE, purpose="daily publication",
                           spark=spark, table=table, candidates=approved,
                           run_id=run_id, row_count=len(approved),
                           table_format=config.table_format)
        published = len(approved)
    except PolicyViolation as exc:
        audit.write("batch_publication_blocked", reason=exc.decision.explain())
        published, to_review = 0, to_review + approved

    queue_table = config.table("review_queue") if config.table_format == "delta" \
        else f"{config.schema}.review_queue"
    if to_review:
        now = datetime.now(timezone.utc)
        pending = spark.createDataFrame([
            {"product_id": c.product_id, "issue_key": c.issue_key,
             "reason": c.decision_reason or "failed declassification",
             "payload": json.dumps(asdict(c), default=str), "status": "open",
             "created_at": now, "run_id": run_id, "day": day}
            for c in to_review
        ])
        pending.createOrReplaceTempView("_cip_review_updates")
        if config.table_format == "delta":
            # Upsert on the issue. Appending re-queued the same item on every
            # run, and preserving created_at keeps queue age meaningful.
            spark.sql(f"""
                MERGE INTO {queue_table} AS t
                USING _cip_review_updates AS s
                  ON t.product_id = s.product_id AND t.issue_key = s.issue_key
                WHEN MATCHED THEN UPDATE SET
                    t.reason = s.reason, t.payload = s.payload,
                    t.run_id = s.run_id, t.day = s.day
                WHEN NOT MATCHED THEN INSERT *
            """)
        else:
            existing = spark.table(queue_table)
            keep = existing.join(pending.select("product_id", "issue_key"),
                                 ["product_id", "issue_key"], "left_anti")
            rows = keep.unionByName(pending.select(existing.columns)).collect()
            spark.createDataFrame(rows, schema=existing.schema) \
                 .write.mode("overwrite").format(config.table_format) \
                 .saveAsTable(queue_table)

    return {"published": published, "queued_for_review": len(to_review),
            "rejected": len(rejected)}


def write_evidence(observations: DataFrame, run_id: str, day: str, config=SPARK) -> int:
    """Append-only, partitioned by day so retention is a partition drop."""
    table = config.table("evidence") if config.table_format == "delta" \
        else f"{config.schema}.evidence"
    rows = (observations
            .select(
                "observation_id", "product_id", "issue_key", "call_id", "segment_id",
                "customer_id", "region",
                F.col("evidence").alias("quote"), "confidence", "extractor",
                F.col("timestamp").alias("observed_at"))
            .withColumn("run_id", F.lit(run_id))
            .withColumn("day", F.lit(day)))
    count = rows.count()
    write_day_partition(rows, table, day, config)
    return count
