"""PySpark / Databricks execution layer.

The pure-Python stages in ``cip.pipeline`` carry all the domain logic. This
package supplies only what the cluster needs: explicit schemas, partition
functions, real shuffles, and Delta writes.

What ports unchanged and what did not is documented honestly in
``docs/DATABRICKS.md`` -- stages 2-4 are a straight ``mapInPandas``/
``mapPartitions`` lift; stage 5 (aggregation) and global dedupe genuinely
required Spark-native rewrites, because the single-node versions hold a
dict and a set over the whole day.
"""
