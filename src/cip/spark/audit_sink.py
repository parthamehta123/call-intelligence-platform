"""Flush the driver's audit log into a queryable Delta table.

Honest scope note. Policy decisions that matter -- declassification,
publication, batch commits -- all happen on the **driver**, so they land
here in full. Audit events raised on *executors* (a schema-invalid
extraction, for instance) are written to that executor's local log and are
visible in the Spark driver/executor logs, but they are not collected into
this table: shipping a row per rejected observation would be a shuffle of
its own. Their aggregate count is captured as a run metric instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import SPARK
from ..security.audit import audit

AUDITED_EVENTS = {
    "policy_decision",
    "tool_executed",
    "declassified",
    "declassification_refused",
    "batch_publication_blocked",
    "spark_run_started",
    "spark_run_finished",
}


def line_count() -> int:
    """Marker taken before a run so only that run's events are flushed."""
    path = audit.path
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def flush(spark, *, run_id: str, day: str, since: int = 0, config=SPARK) -> int:
    path = audit.path
    if not path.exists():
        return 0

    records = []
    for line in path.read_text().splitlines()[since:]:
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") not in AUDITED_EVENTS:
            continue
        records.append({
            "ts": datetime.fromisoformat(record["ts"]),
            "event": record["event"],
            "tool": record.get("tool"),
            "role": record.get("role"),
            "action": record.get("action"),
            "risk": float(record["risk"]) if record.get("risk") is not None else None,
            "taint": record.get("taint"),
            "purpose": record.get("purpose"),
            "reasons": [str(r) for r in (record.get("reasons") or [])],
            "run_id": run_id,
            "day": day,
        })

    if not records:
        return 0

    table = config.table("policy_audit") if config.table_format == "delta" \
        else f"{config.schema}.policy_audit"
    schema = spark.table(table).schema
    (spark.createDataFrame(records, schema=schema)
          .write.mode("append").format(config.table_format)
          .partitionBy("day").saveAsTable(table))
    return len(records)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
