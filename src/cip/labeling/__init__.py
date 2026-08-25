"""Labelling harness.

Every eval figure in this repo rests on labels written by the person who
built the system, and a model has already found one of them wrong. That is
the single largest threat to the numbers, and it cannot be fixed by writing
more labels the same way.

So this is the part that makes the gap closable by somebody else:

  pool.py       sample items worth labelling, stratified by uncertainty
  store.py      labels with annotator, timestamp and provenance
  agreement.py  inter-annotator agreement, and adjudication of conflicts

The design decisions that matter are about bias, not plumbing. The pool is
sampled where the router is *uncertain* rather than uniformly, because a
labeller's time is the scarce resource and items the system already gets
confidently right teach nothing. And the harness never shows the current
prediction: an annotator told what the model thinks agrees with it, which
turns an independent label into a confirmation.
"""

from .agreement import Agreement, agreement, adjudicate
from .pool import PoolItem, build_router_pool, build_retrieval_pool
from .store import Label, LabelStore

__all__ = ["Agreement", "agreement", "adjudicate", "PoolItem",
           "build_router_pool", "build_retrieval_pool", "Label", "LabelStore"]
