"""Session bootstrap that works both on Databricks and on a laptop."""

from __future__ import annotations

from ..config import SPARK


def get_spark(app_name: str = "call-intelligence-platform"):
    from pyspark.sql import SparkSession

    active = SparkSession.getActiveSession()
    if active is not None:
        # On Databricks the session already exists and is configured by the
        # runtime; creating another one would silently ignore cluster config.
        return active

    builder = (
        SparkSession.builder.appName(app_name)
        .config("spark.sql.shuffle.partitions", SPARK.shuffle_partitions)
        .config("spark.sql.session.timeZone", "UTC")
        # Skew is the default state of call data: one product, one region and
        # one call centre always dominate. AQE splits those partitions instead
        # of leaving a single 4-hour task holding up the job.
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.sql.adaptive.skewJoin.enabled", "true")
        .config("spark.sql.warehouse.dir", SPARK.warehouse_dir)
    )
    if SPARK.table_format == "delta":
        builder = (
            builder
            .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
            .config("spark.sql.catalog.spark_catalog",
                    "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        )
    return builder.getOrCreate()
