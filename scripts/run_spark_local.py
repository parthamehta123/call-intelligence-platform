"""Run the full Spark job locally, against the synthetic lake, on parquet.

    make spark-run

Uses the isolated .venv-spark interpreter. Do NOT run this with the base
environment's python if databricks-connect is installed there -- see
docs/DATABRICKS.md.
"""

import json, os, sys, pathlib
root = pathlib.Path("/Users/parthamehta/call-intelligence-platform")
sys.path.insert(0, str(root / "src"))
os.environ["PYTHONPATH"] = str(root / "src")
os.environ["PYSPARK_PYTHON"] = sys.executable

from pyspark.sql import SparkSession
from cip.config import SPARK
SPARK.table_format = "parquet"
SPARK.schema = "cip_local"
SPARK.shuffle_partitions = 8
SPARK.warehouse_dir = str(root / "data" / "spark-warehouse")

spark = (SparkSession.builder.appName("cip-local")
         .master("local[4]")
         .config("spark.sql.shuffle.partitions", 8)
         .config("spark.sql.session.timeZone", "UTC")
         .config("spark.sql.warehouse.dir", SPARK.warehouse_dir)
         .getOrCreate())
spark.sparkContext.setLogLevel("ERROR")

from cip.spark.job import run
stats = run("2026-08-22", raw_path=str(root / "data" / "lake"), config=SPARK, spark=spark)
print("STATS:" + json.dumps(stats, indent=2))
print("--- issues ---")
spark.table("cip_local.issues").select(
    "product_id","issue_key","severity","status","customers","mentions").orderBy("customers", ascending=False).show(20, False)
print("--- review queue ---")
spark.table("cip_local.review_queue").select("product_id","issue_key","reason").show(10, False)
spark.stop()
