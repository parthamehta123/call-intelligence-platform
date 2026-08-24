"""Does lexical matching actually earn its place?

The claim made repeatedly in this repo: hybrid retrieval is needed because
product IDs and version strings require exact matching, and dense vectors
place `7.2.13` and `7.2.1` in nearly the same position.

The main eval could not test it. With ten documents every version string
is also a rare token, so BM25 never had a chance to be *uniquely* right —
both legs scored 1.000 on `exact_identifier` and the claim went unproven.

This builds the corpus the claim describes: documents that are near
identical except for the identifier, so retrieving the right one depends
entirely on telling `7.2.1` from `7.2.13`. If lexical matching earns its
place anywhere, it earns it here.
"""

from __future__ import annotations

from dataclasses import dataclass

# Deliberately near-duplicate prose. Every document describes a comparable
# defect on the same product line; only the version differs.
CORPUS = [
    ("doc::X100::v7_2",    "X100 firmware 7.2 release notes",
     "Firmware 7.2 for the X100 Enterprise Router. Addresses VPN tunnel stability."),
    ("doc::X100::v7_2_1",  "X100 firmware 7.2.1 release notes",
     "Firmware 7.2.1 for the X100 Enterprise Router. Addresses VPN tunnel stability."),
    ("doc::X100::v7_2_13", "X100 firmware 7.2.13 release notes",
     "Firmware 7.2.13 for the X100 Enterprise Router. Addresses VPN tunnel stability."),
    ("doc::X100::v7_1",    "X100 firmware 7.1 release notes",
     "Firmware 7.1 for the X100 Enterprise Router. Addresses VPN tunnel stability."),
    ("doc::XG482::v3_4",   "XG-482 firmware 3.4 release notes",
     "Firmware 3.4 for the XG-482 Branch Gateway. Addresses static route persistence."),
    ("doc::XG482::v3_5",   "XG-482 firmware 3.5 release notes",
     "Firmware 3.5 for the XG-482 Branch Gateway. Addresses static route persistence."),
]

QUERIES = [
    ("what changed in X100 firmware 7.2.13", "doc::X100::v7_2_13"),
    ("X100 firmware 7.2.1 release notes", "doc::X100::v7_2_1"),
    ("X100 firmware 7.2 release notes", "doc::X100::v7_2"),
    ("X100 firmware 7.1 release notes", "doc::X100::v7_1"),
    ("XG-482 firmware 3.4 release notes", "doc::XG482::v3_4"),
    ("XG-482 firmware 3.5 release notes", "doc::XG482::v3_5"),
]


@dataclass
class LegScore:
    leg: str
    top1: int = 0
    total: int = 0
    misses: list[str] = None

    @property
    def accuracy(self) -> float:
        return self.top1 / self.total if self.total else 0.0


def _build_index():
    from ..retrieval import Index

    index = Index()
    index.upsert([
        {"doc_id": doc_id, "product_id": doc_id.split("::")[1], "title": title,
         "body": body, "source": "release_note", "status": "published"}
        for doc_id, title, body in CORPUS
    ])
    return index


def evaluate_identifiers() -> dict[str, LegScore]:
    """Top-1 accuracy per leg. Fusion is scored on rank, as retrieval does."""
    from ..config import CONFIG
    from ..retrieval import tokenize

    index = _build_index()
    scores = {leg: LegScore(leg=leg, misses=[]) for leg in ("bm25", "dense", "hybrid")}

    for query, expected in QUERIES:
        lexical = index.bm25(query)[:10]
        dense = index.dense(query)[:10]
        # Use the production fusion weights rather than reimplementing them.
        # This eval previously computed its own equal-weight RRF, so it
        # measured a copy of the system and would have reported no change
        # when the real weighting was fixed.
        from ..retrieval import _leg_weights

        weights = _leg_weights(query, "hybrid")
        fused: dict[str, float] = {}
        for weight, ranking in zip(weights, (lexical, dense)):
            for rank, (doc_id, _) in enumerate(ranking):
                fused[doc_id] = fused.get(doc_id, 0.0) + weight / (CONFIG.rrf_k + rank + 1)

        picks = {
            "bm25": lexical[0][0] if lexical else None,
            "dense": dense[0][0] if dense else None,
            "hybrid": max(fused, key=fused.get) if fused else None,
        }
        for leg, pick in picks.items():
            scores[leg].total += 1
            if pick == expected:
                scores[leg].top1 += 1
            else:
                scores[leg].misses.append(f"{query!r} -> {pick}")
    return scores


def report() -> str:
    from ..config import CONFIG

    scores = evaluate_identifiers()
    lines = [
        f"=== near-miss identifiers ({len(QUERIES)} queries, {len(CORPUS)} documents) ===",
        f"  embedder: {CONFIG.embedder}",
        "",
        f"  {'leg':<8} {'top-1':>7}",
    ]
    for leg in ("bm25", "dense", "hybrid"):
        lines.append(f"  {leg:<8} {scores[leg].accuracy:>7.3f}")
    for leg in ("bm25", "dense", "hybrid"):
        for miss in scores[leg].misses[:3]:
            lines.append(f"    {leg} missed {miss}")
    return "\n".join(lines)
