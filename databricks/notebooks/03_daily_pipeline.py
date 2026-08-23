# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · Daily pipeline
# MAGIC
# MAGIC ```
# MAGIC read raw  ->  preprocess  ->  global dedupe  ->  route + extract
# MAGIC           ->  write evidence  ->  aggregate  ->  reconcile
# MAGIC           ->  declassify  ->  guarded publish
# MAGIC ```
# MAGIC
# MAGIC Distributed work stays on the executors. Reconciliation and publication
# MAGIC run on the driver **on purpose**: 10 TB of calls reduce to a few
# MAGIC thousand issue candidates, and decision logic at that size belongs in
# MAGIC ordinary testable Python, not spread across 200 executors.

# COMMAND ----------

dbutils.widgets.text("day", "")
dbutils.widgets.text("catalog", "cip_dev")
dbutils.widgets.text("schema", "call_intelligence")
dbutils.widgets.text("volume", "raw_calls")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
volume = dbutils.widgets.get("volume")

# COMMAND ----------

from datetime import datetime, timedelta, timezone

day = dbutils.widgets.get("day").strip()
if not day:
    # "Yesterday" in UTC, not in the cluster's local zone -- a job scheduled
    # at 03:00 local would otherwise reprocess or skip a day twice a year.
    day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
print("processing day:", day)

# COMMAND ----------

import os

os.environ["CIP_CATALOG"] = catalog
os.environ["CIP_SCHEMA"] = schema
os.environ["CIP_VOLUME"] = volume
os.environ["CIP_TABLE_FORMAT"] = "delta"

from cip.config import SPARK
SPARK.catalog, SPARK.schema, SPARK.volume = catalog, schema, volume
SPARK.table_format = "delta"

raw_path = f"/Volumes/{catalog}/{schema}/{volume}"

# COMMAND ----------

import json
from cip.spark.job import run

stats = run(day, raw_path=raw_path, config=SPARK, spark=spark)
print(json.dumps(stats, indent=2))

# COMMAND ----------

# MAGIC %md ## Canonical product state

# COMMAND ----------

display(spark.table(f"{catalog}.{schema}.issues")
        .select("product_id", "issue_key", "severity", "status",
                "customers", "mentions", "updated_at")
        .orderBy("customers", ascending=False))

# COMMAND ----------

# MAGIC %md ## Human review queue
# MAGIC Contradictions and spec corrections land here. Nothing auto-resolved them.

# COMMAND ----------

display(spark.table(f"{catalog}.{schema}.review_queue")
        .filter("status = 'open'")
        .select("product_id", "issue_key", "reason", "created_at"))

# COMMAND ----------

dbutils.notebook.exit(json.dumps(stats))
