"""Spark-native aggregation -- the other stage that did NOT port for free.

`cip.pipeline.aggregate` builds a `defaultdict` over every observation of
the day. That is a driver-side collect: correct for 4,000 calls, fatal for
10 TB. The logic below is the same contract expressed as a shuffle, and
`tests/test_spark.py` asserts the two produce identical candidates on the
same input -- which is the only reason it is safe to have two
implementations at all.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, functions as F

RESOLVED_PREFIX = "RESOLVED CLAIM:"

SEVERITY_RANK = F.create_map(
    F.lit("low"), F.lit(0), F.lit("medium"), F.lit(1),
    F.lit("high"), F.lit(2), F.lit("critical"), F.lit(3),
)


def aggregate_observations(observations: DataFrame) -> DataFrame:
    """observations -> one row per (product_id, issue_key)."""
    obs = observations.withColumn(
        "polarity",
        F.when(F.col("summary").startswith(RESOLVED_PREFIX), F.lit("denying"))
         .otherwise(F.lit("affirming")),
    ).withColumn("severity_rank", SEVERITY_RANK[F.col("severity")])

    totals = obs.groupBy("product_id", "issue_key").agg(
        F.count("*").alias("mentions"),
        # Exact, not approx_count_distinct. The auto-accept threshold is a
        # small integer (5 customers), and HLL's error band straddles it --
        # an approximate count here would make promotion nondeterministic.
        F.countDistinct("customer_id").alias("distinct_customers"),
        F.sort_array(F.collect_set("region")).alias("regions"),
        F.sort_array(F.collect_set("product_version")).alias("versions"),
        F.min("timestamp").alias("first_seen"),
        F.max("timestamp").alias("last_seen"),
        F.sum(F.when(F.col("polarity") == "affirming", 1).otherwise(0)).alias("affirming"),
        F.sum(F.when(F.col("polarity") == "denying", 1).otherwise(0)).alias("denying"),
        F.sort_array(F.collect_set("observation_id")).alias("evidence_ids"),
    ).withColumn(
        "majority",
        F.when(F.col("affirming") >= F.col("denying"), F.lit("affirming"))
         .otherwise(F.lit("denying")),
    )

    # Summary, type and severity come from the majority side only.
    majority_rows = obs.join(
        totals.select("product_id", "issue_key", "majority"),
        ["product_id", "issue_key"],
    ).filter(F.col("polarity") == F.col("majority"))

    majority_agg = majority_rows.groupBy("product_id", "issue_key").agg(
        F.round(F.avg("confidence"), 3).alias("mean_confidence"),
        # max() over a struct picks the highest severity, tie-broken
        # lexicographically -- deterministic, unlike first() which depends
        # on partition order and would make reruns disagree.
        F.max(F.struct("severity_rank", "severity", "summary", "type")).alias("top"),
    )

    return (
        totals.join(majority_agg, ["product_id", "issue_key"])
        .withColumn("severity", F.col("top.severity"))
        .withColumn("summary", F.col("top.summary"))
        .withColumn("type", F.col("top.type"))
        .withColumn(
            "conflicts",
            F.when(
                (F.col("affirming") > 0) & (F.col("denying") > 0),
                F.array(F.concat(
                    F.least(F.col("affirming"), F.col("denying")).cast("string"),
                    F.lit("/"), F.col("mentions").cast("string"),
                    F.lit(" reports claim the issue is resolved"))),
            ).otherwise(F.array().cast("array<string>")),
        )
        .drop("top", "majority", "affirming", "denying")
        .orderBy(F.col("distinct_customers").desc(), "product_id", "issue_key")
    )
