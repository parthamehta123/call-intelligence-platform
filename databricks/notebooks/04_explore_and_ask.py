# Databricks notebook source
# MAGIC %md
# MAGIC # 04 · Explore, trace provenance, ask questions
# MAGIC
# MAGIC Three things worth showing an interviewer:
# MAGIC 1. every published fact traces back to the exact calls behind it;
# MAGIC 2. counting questions go to SQL, descriptive ones go to retrieval;
# MAGIC 3. the funnel's cost effect is measurable, not asserted.

# COMMAND ----------

dbutils.widgets.text("catalog", "cip_dev")
dbutils.widgets.text("schema", "call_intelligence")
catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")

# COMMAND ----------

# MAGIC %md ## Provenance: what is the evidence behind one issue?

# COMMAND ----------

display(spark.sql(f"""
    SELECT e.call_id, e.customer_id, e.region, e.confidence, e.extractor, e.quote
    FROM {catalog}.{schema}.evidence e
    WHERE e.product_id = 'PULSE7' AND e.issue_key = 'OVERHEATING'
    LIMIT 20
"""))

# COMMAND ----------

# MAGIC %md ## Corroboration: distinct customers, not raw mention count
# MAGIC One enterprise account calling forty times is one corroborating source.

# COMMAND ----------

display(spark.sql(f"""
    SELECT product_id, issue_key,
           COUNT(*)                        AS mentions,
           COUNT(DISTINCT customer_id)     AS distinct_customers,
           ROUND(COUNT(*) / COUNT(DISTINCT customer_id), 2) AS calls_per_customer
    FROM {catalog}.{schema}.evidence
    GROUP BY product_id, issue_key
    ORDER BY distinct_customers DESC
"""))

# COMMAND ----------

# MAGIC %md ## Funnel economics

# COMMAND ----------

display(spark.sql(f"""
    SELECT run_id, day,
           MAX(CASE WHEN metric = 'calls'        THEN value END) AS calls,
           MAX(CASE WHEN metric = 'segments'     THEN value END) AS segments,
           MAX(CASE WHEN metric = 'observations' THEN value END) AS observations,
           MAX(CASE WHEN metric = 'published'    THEN value END) AS published
    FROM {catalog}.{schema}.run_metrics
    GROUP BY run_id, day
    ORDER BY day DESC
"""))

# COMMAND ----------

# MAGIC %md ## The serving agent
# MAGIC Structured question -> SQL. Descriptive question -> hybrid retrieval.
# MAGIC The local SQLite/vector serving layer is single-node by design; on the
# MAGIC cluster the same questions run straight against Delta.

# COMMAND ----------

display(spark.sql(f"""
    SELECT product_id, issue_key, severity, status, customers
    FROM {catalog}.{schema}.issues
    WHERE issue_key = 'ROUTE_LOSS'
"""))

# COMMAND ----------

# MAGIC %md ## Policy decisions are queryable
# MAGIC Every allow/deny is auditable after the fact, with the provenance that
# MAGIC caused it.

# COMMAND ----------

display(spark.sql(f"""
    SELECT action, tool, role, COUNT(*) AS decisions
    FROM {catalog}.{schema}.policy_audit
    GROUP BY action, tool, role
    ORDER BY decisions DESC
"""))
