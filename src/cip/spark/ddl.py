"""Table definitions.

Partitioning and clustering choices are the ones that matter at 10 TB:

  * `evidence` is the big table -- billions of rows a year. Partitioned by
    ingest day so retention is a partition drop, and Z-ordered on the
    lookup key so provenance queries do not scan the year.
  * `issues` is small (thousands of rows). Partitioning it would create
    thousands of tiny files; it is left unpartitioned on purpose.
"""

from __future__ import annotations

from ..config import SPARK

DDL = {
    "issues": """
        CREATE TABLE IF NOT EXISTS {t} (
            product_id STRING NOT NULL,
            issue_key STRING NOT NULL,
            type STRING,
            summary STRING,
            severity STRING,
            status STRING,
            mentions BIGINT,
            customers BIGINT,
            regions ARRAY<STRING>,
            versions ARRAY<STRING>,
            first_seen STRING,
            last_seen STRING,
            confidence DOUBLE,
            updated_at TIMESTAMP,
            run_id STRING
        ) USING {fmt}
    """,
    "evidence": """
        CREATE TABLE IF NOT EXISTS {t} (
            observation_id STRING NOT NULL,
            product_id STRING,
            issue_key STRING,
            call_id STRING,
            segment_id STRING,
            customer_id STRING,
            region STRING,
            quote STRING,
            confidence DOUBLE,
            extractor STRING,
            observed_at STRING,
            run_id STRING,
            day STRING
        ) USING {fmt}
        PARTITIONED BY (day)
    """,
    # Silver: cleaned, redacted, deduplicated segments.
    "segments": """
        CREATE TABLE IF NOT EXISTS {t} (
            segment_id STRING,
            call_id STRING,
            customer_id STRING,
            timestamp STRING,
            region STRING,
            text STRING,
            speaker_mix MAP<STRING, INT>,
            customer_turns INT,
            attribution_confidence DOUBLE,
            product_hint STRING,
            trust STRING,
            product_id STRING,
            product_confidence DOUBLE,
            relevance DOUBLE,
            pii_redactions INT,
            content_hash STRING,
            run_id STRING,
            day STRING
        ) USING {fmt}
        PARTITIONED BY (day)
    """,
    # Extracted observations, materialised so aggregation never re-runs the
    # extractor. On serverless `.cache()` is unavailable (PERSIST TABLE is
    # rejected), and re-deriving this DataFrame three times would mean
    # paying for extraction three times.
    "observations": """
        CREATE TABLE IF NOT EXISTS {t} (
            observation_id STRING,
            segment_id STRING,
            call_id STRING,
            customer_id STRING,
            product_id STRING,
            product_version STRING,
            type STRING,
            issue_key STRING,
            summary STRING,
            severity STRING,
            evidence STRING,
            confidence DOUBLE,
            region STRING,
            timestamp STRING,
            trust STRING,
            extractor STRING,
            speaker STRING,
            attribution_confidence DOUBLE,
            run_id STRING,
            day STRING
        ) USING {fmt}
        PARTITIONED BY (day)
    """,
    "review_queue": """
        CREATE TABLE IF NOT EXISTS {t} (
            product_id STRING,
            issue_key STRING,
            reason STRING,
            payload STRING,
            status STRING,
            created_at TIMESTAMP,
            run_id STRING,
            day STRING
        ) USING {fmt}
        PARTITIONED BY (day)
    """,
    "seen_segments": """
        CREATE TABLE IF NOT EXISTS {t} (
            customer_id STRING,
            content_hash STRING,
            day STRING
        ) USING {fmt}
        PARTITIONED BY (day)
    """,
    "policy_audit": """
        CREATE TABLE IF NOT EXISTS {t} (
            ts TIMESTAMP,
            event STRING,
            tool STRING,
            role STRING,
            action STRING,
            risk DOUBLE,
            taint STRING,
            purpose STRING,
            reasons ARRAY<STRING>,
            run_id STRING,
            day STRING
        ) USING {fmt}
        PARTITIONED BY (day)
    """,
    "run_metrics": """
        CREATE TABLE IF NOT EXISTS {t} (
            run_id STRING,
            day STRING,
            metric STRING,
            value DOUBLE,
            recorded_at TIMESTAMP
        ) USING {fmt}
    """,
}

# Delta-only optimisations; skipped when running on parquet locally.
OPTIMIZE = {
    "evidence": "ALTER TABLE {t} SET TBLPROPERTIES (delta.autoOptimize.optimizeWrite = true)",
    "issues": "ALTER TABLE {t} SET TBLPROPERTIES (delta.autoOptimize.optimizeWrite = true)",
}


def create_all(spark, *, config=SPARK) -> list[str]:
    created: list[str] = []
    if config.table_format == "delta":
        # Unity Catalog: the catalog itself is created by an admin, not the
        # job -- the job's service principal should not hold CREATE CATALOG.
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {config.namespace}")
    else:
        spark.sql(f"CREATE DATABASE IF NOT EXISTS {config.schema}")

    for name, ddl in DDL.items():
        table = config.table(name) if config.table_format == "delta" \
            else f"{config.schema}.{name}"
        spark.sql(ddl.format(t=table, fmt=config.table_format))
        if config.table_format == "delta" and name in OPTIMIZE:
            spark.sql(OPTIMIZE[name].format(t=table))
        created.append(table)
    return created
