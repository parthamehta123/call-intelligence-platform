"""Global deduplication -- one of the two stages that did NOT port for free.

The single-node version keeps a `seen` set. Lifted onto Spark unchanged it
would deduplicate *within a partition* and silently miss the duplicates
that matter: the same customer's retried call landing in a different
shard. Correct dedupe is a shuffle, and pretending otherwise is how a
mention count quietly inflates.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, functions as F

from ..config import SPARK


def dedupe_within_day(segments: DataFrame) -> DataFrame:
    """Drop byte-identical segments from the same customer within the day."""
    return segments.dropDuplicates(["customer_id", "content_hash"])


def dedupe_across_days(segments: DataFrame, spark, seen_table: str | None = None,
                       day: str | None = None) -> DataFrame:
    """Anti-join against hashes seen on *earlier* days.

    A call re-transcribed or re-ingested tomorrow is still the same call.
    Within-day dedupe cannot see that, so corroboration counts drift upward
    over time unless the seen-set is persisted.

    The `day` filter is load-bearing, not a detail. Without it, a retry of
    today's run anti-joins against the hashes today's *first* attempt just
    recorded, every segment is dropped, and the job cheerfully reports
    processing zero calls. Excluding the current day makes a retry
    reprocess the day in full, which is what the partition-replacing
    writers assume.

    At 10 TB/day the seen table is itself large, so it is partitioned by
    day and pruned to a retention window; the partition filter below is
    also what lets Spark skip reading most of it.
    """
    seen_table = seen_table or SPARK.table("seen_segments")
    if not spark.catalog.tableExists(seen_table):
        return segments
    seen = spark.table(seen_table)
    if day is not None:
        seen = seen.filter(F.col("day") < day)
    seen = seen.select("customer_id", "content_hash").distinct()
    return segments.join(seen, ["customer_id", "content_hash"], "left_anti")


def record_seen(segments: DataFrame, day: str, spark, seen_table: str | None = None,
                config=SPARK) -> None:
    seen_table = seen_table or SPARK.table("seen_segments")
    from .publish import write_day_partition

    rows = (segments.select("customer_id", "content_hash")
                    .distinct()
                    .withColumn("day", F.lit(day)))
    write_day_partition(rows, seen_table, day, SPARK)
