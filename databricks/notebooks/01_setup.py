# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Setup
# MAGIC
# MAGIC Creates the schema, the raw-calls volume and every Delta table.
# MAGIC
# MAGIC Idempotent — safe to re-run. The **catalog itself is not created here**:
# MAGIC in a governed workspace the job's service principal should not hold
# MAGIC `CREATE CATALOG`. An admin creates it once, then grants `USE CATALOG`.

# COMMAND ----------

dbutils.widgets.text("catalog", "cip_dev")
dbutils.widgets.text("schema", "call_intelligence")
dbutils.widgets.text("volume", "raw_calls")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
volume = dbutils.widgets.get("volume")

# COMMAND ----------

import os

os.environ["CIP_CATALOG"] = catalog
os.environ["CIP_SCHEMA"] = schema
os.environ["CIP_VOLUME"] = volume
os.environ["CIP_TABLE_FORMAT"] = "delta"

from cip.config import SPARK
from cip.spark import ddl

SPARK.catalog, SPARK.schema, SPARK.volume = catalog, schema, volume
SPARK.table_format = "delta"

# COMMAND ----------

# The catalog is NOT created here. On accounts using Default Storage a bare
# CREATE CATALOG fails ("Metastore storage root URL does not exist") because
# it needs an explicit MANAGED LOCATION -- and in a governed workspace the
# job's identity should not hold CREATE CATALOG at all. An admin creates it;
# this job only asserts it can see it.
existing = {r.catalog for r in spark.sql("SHOW CATALOGS").collect()}
if catalog not in existing:
    raise RuntimeError(
        f"catalog {catalog!r} not found. Visible: {sorted(existing)}. "
        f"Create it in the UI (Catalog > Create catalog), or grant access, "
        f"then re-run. Do not add CREATE CATALOG here.")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {catalog}.{schema}.{volume}")

created = ddl.create_all(spark, SPARK)
print("\n".join(created))

# COMMAND ----------

display(spark.sql(f"SHOW TABLES IN {catalog}.{schema}"))
