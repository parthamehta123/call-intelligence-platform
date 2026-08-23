"""Pipeline stages, in dependency order.

ingest -> preprocess -> route -> extract -> aggregate -> reconcile -> publish

Each stage is a pure function over an iterable of records, which is what
makes the local implementation and the Spark implementation the same code
shape: `mapPartitions(stage)` instead of `map(stage, partition)`.
"""
