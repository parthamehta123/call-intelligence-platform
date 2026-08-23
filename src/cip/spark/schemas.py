"""Explicit Spark schemas.

Schema inference reads a sample of the data to guess types. On 10 TB that
is an expensive extra pass, and worse, it is non-deterministic: a day
where every `product_hint` happens to be null infers a different schema
than the day before and the job fails on write. Every read here is
explicitly typed.
"""

from __future__ import annotations

from pyspark.sql.types import (
    ArrayType, DoubleType, IntegerType, LongType, MapType, StringType,
    StructField, StructType, TimestampType,
)

TURN = StructType([
    StructField("speaker", StringType(), False),
    StructField("text", StringType(), False),
    StructField("start_time", DoubleType(), True),
])

CALL = StructType([
    StructField("call_id", StringType(), False),
    StructField("customer_id", StringType(), False),
    StructField("timestamp", StringType(), False),
    StructField("region", StringType(), False),
    StructField("channel", StringType(), False),
    StructField("product_hint", StringType(), True),
    StructField("turns", ArrayType(TURN), False),
])

SEGMENT = StructType([
    StructField("segment_id", StringType(), False),
    StructField("call_id", StringType(), False),
    StructField("customer_id", StringType(), False),
    StructField("timestamp", StringType(), False),
    StructField("region", StringType(), False),
    StructField("text", StringType(), False),
    StructField("speaker_mix", MapType(StringType(), IntegerType()), True),
    StructField("product_hint", StringType(), True),
    StructField("trust", StringType(), False),
    StructField("product_id", StringType(), True),
    StructField("product_confidence", DoubleType(), False),
    StructField("relevance", DoubleType(), False),
    StructField("pii_redactions", IntegerType(), False),
    StructField("content_hash", StringType(), False),
])

OBSERVATION = StructType([
    StructField("observation_id", StringType(), False),
    StructField("segment_id", StringType(), False),
    StructField("call_id", StringType(), False),
    StructField("customer_id", StringType(), False),
    StructField("product_id", StringType(), False),
    StructField("product_version", StringType(), True),
    StructField("type", StringType(), False),
    StructField("issue_key", StringType(), False),
    StructField("summary", StringType(), False),
    StructField("severity", StringType(), False),
    StructField("evidence", StringType(), False),
    StructField("confidence", DoubleType(), False),
    StructField("region", StringType(), False),
    StructField("timestamp", StringType(), False),
    StructField("trust", StringType(), False),
    StructField("extractor", StringType(), False),
])

# Stage metrics emitted per partition, unioned and summed on the driver.
# Accumulators would be cheaper but are unreliable under speculative
# execution and task retries -- a retried task double-counts. A tiny
# DataFrame that is deduped by partition id is not clever, and is correct.
PARTITION_METRICS = StructType([
    StructField("partition_id", LongType(), False),
    StructField("segments", LongType(), False),
    StructField("segments_to_llm", LongType(), False),
    StructField("pii_redactions", LongType(), False),
    StructField("observations", LongType(), False),
    StructField("extraction_rejected", LongType(), False),
])
