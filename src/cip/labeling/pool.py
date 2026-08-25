"""Choosing what is worth labelling.

Uniform sampling spends most of a labeller's attention on items the system
already handles confidently, which teaches nothing. These pools are
stratified by uncertainty: heaviest around the decision boundary, thinner
in the regions where the answer is not in doubt.

The pool carries the item and nothing else. No score, no prediction, no
current label -- see the note in `__init__` about anchoring.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field


@dataclass
class PoolItem:
    item_id: str
    kind: str                      # "router" | "retrieval"
    payload: dict
    stratum: str = ""
    # Recorded so a later reader can tell how the pool was assembled, and
    # reproduce or challenge it. Never shown to the annotator.
    provenance: dict = field(default_factory=dict)

    def prompt(self) -> str:
        if self.kind == "router":
            return self.payload["text"]
        return (f"Query:    {self.payload['query']}\n"
                f"Document: {self.payload['title']}\n"
                f"          {self.payload['body'][:300]}")


def _item_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:14]


def _stratum(score: float, threshold: float) -> str:
    """Distance from the decision boundary, in bands."""
    margin = abs(score - threshold)
    if margin <= 0.05:
        return "boundary"
    if margin <= 0.20:
        return "near"
    return "confident_keep" if score >= threshold else "confident_drop"


# Where a fixed labelling budget goes. Weighted toward the boundary because
# that is where a wrong threshold actually costs something; the confident
# bands are sampled thinly, as a check that they really are confident.
ROUTER_QUOTA = {"boundary": 0.45, "near": 0.30,
                "confident_keep": 0.15, "confident_drop": 0.10}


def build_router_pool(day: str = "2026-08-22", size: int = 300,
                      seed: int = 11) -> list[PoolItem]:
    """Segments for "is this product signal?", stratified by router score."""
    from ..config import CONFIG
    from ..pipeline.ingest import list_partitions, read_partition
    from ..pipeline.preprocess import preprocess
    from ..pipeline.route import score_segment

    scored: dict[str, list] = {k: [] for k in ROUTER_QUOTA}
    for partition in list_partitions(day):
        for segment in preprocess(read_partition(partition)):
            score = score_segment(segment)
            scored[_stratum(score, CONFIG.relevance_threshold)].append((segment, score))

    rng = random.Random(seed)
    for candidates in scored.values():
        rng.shuffle(candidates)

    def make(stratum: str, segment, score) -> PoolItem:
        return PoolItem(
            item_id=_item_id("router", segment.segment_id),
            kind="router",
            payload={"text": segment.text, "segment_id": segment.segment_id},
            stratum=stratum,
            # Held for analysis, withheld from the annotator.
            provenance={"router_score": round(score, 3), "day": day})

    pool: list[PoolItem] = []
    taken = {stratum: 0 for stratum in ROUTER_QUOTA}
    for stratum, share in ROUTER_QUOTA.items():
        wanted = int(size * share)
        chosen = scored[stratum][:wanted]
        taken[stratum] = len(chosen)
        pool += [make(stratum, segment, score) for segment, score in chosen]

    # A stratum can be short -- the rule-based router emits discrete scores,
    # so few segments land within 0.05 of the threshold. Redistribute the
    # shortfall instead of returning a smaller pool: a labelling budget is
    # fixed by the labeller's time, not by how the scores happen to bunch.
    shortfall = size - len(pool)
    if shortfall > 0:
        leftovers = [(stratum, segment, score)
                     for stratum in ROUTER_QUOTA
                     for segment, score in scored[stratum][taken[stratum]:]]
        rng.shuffle(leftovers)
        pool += [make(stratum, segment, score)
                 for stratum, segment, score in leftovers[:shortfall]]

    rng.shuffle(pool)
    return pool


def build_retrieval_pool(queries: list[str], top_k: int = 5,
                         seed: int = 11) -> list[PoolItem]:
    """Query-document pairs for "does this document belong in the answer?".

    Pairs come from what retrieval actually returns, so the labels bind to
    the system's real candidates rather than to an idealised corpus.
    """
    from ..retrieval import hybrid_search

    rng = random.Random(seed)
    pool: list[PoolItem] = []
    for query in queries:
        for rank, hit in enumerate(hybrid_search(query, top_k=top_k)):
            pool.append(PoolItem(
                item_id=_item_id("retrieval", query, hit["doc_id"]),
                kind="retrieval",
                payload={"query": query, "doc_id": hit["doc_id"],
                         "title": hit["title"], "body": hit["body"]},
                stratum=f"rank_{rank + 1}",
                provenance={"rank": rank + 1, "score": hit.get("score")},
            ))
    rng.shuffle(pool)
    return pool
