"""Retrieval quality: Recall@K, MRR, nDCG, routing, abstention.

Two claims in this repo's documentation were unmeasured until now:

  1. "hybrid retrieval beats either leg, because product IDs and versions
     need exact lexical matching"
  2. "counting questions go to SQL, because vector similarity cannot count"

Both are testable, and the first is the reason `bm25` / `dense` /
`hybrid` run through an identical filter and rerank path -- an ablation is
the only evidence that fusing helps rather than merely sounding right.

Relevance here is binary and the corpus is small (10 documents, 16
queries), so these are diagnostics rather than benchmark figures. They are
strong enough to catch a regression and to settle the ablation; they are
not strong enough to publish.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ROOT
from ..retrieval import hybrid_search

CASES = ROOT / "eval" / "retrieval_cases.jsonl"
MODES = ("hybrid", "bm25", "dense")


@dataclass
class RetrievalCase:
    query: str
    relevant: list[str]
    category: str
    route: str
    why: str = ""


def load_cases(path: Path | None = None) -> list[RetrievalCase]:
    rows = [json.loads(l) for l in Path(path or CASES).read_text().splitlines() if l.strip()]
    return [RetrievalCase(**row) for row in rows]


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    if not relevant:
        return 1.0
    return len(set(retrieved[:k]) & set(relevant)) / len(relevant)


def reciprocal_rank(retrieved: list[str], relevant: list[str]) -> float:
    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Binary relevance, so gain is 1 or 0 and DCG reduces to a log discount."""
    if not relevant:
        return 1.0
    dcg = sum(1.0 / math.log2(rank + 1)
              for rank, doc_id in enumerate(retrieved[:k], start=1) if doc_id in relevant)
    ideal = sum(1.0 / math.log2(rank + 1)
                for rank in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


@dataclass
class ModeScore:
    mode: str
    recall: float = 0.0
    mrr: float = 0.0
    ndcg: float = 0.0
    n: int = 0
    by_category: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))


def score_mode(cases: list[RetrievalCase], mode: str, k: int = 5) -> ModeScore:
    """Scored over answerable queries only -- an unanswerable query has no
    correct ranking, and folding it in as recall 1.0 flatters every mode."""
    answerable = [c for c in cases if c.relevant]
    score = ModeScore(mode=mode, n=len(answerable))
    for case in answerable:
        retrieved = [h["doc_id"] for h in hybrid_search(case.query, top_k=k, mode=mode)]
        r = recall_at_k(retrieved, case.relevant, k)
        score.recall += r
        score.mrr += reciprocal_rank(retrieved, case.relevant)
        score.ndcg += ndcg_at_k(retrieved, case.relevant, k)
        score.by_category[case.category].append(r)
    if score.n:
        score.recall /= score.n
        score.mrr /= score.n
        score.ndcg /= score.n
    return score


def routing_accuracy(cases: list[RetrievalCase]) -> tuple[int, int, list[str]]:
    from ..agent import ask

    correct, wrong = 0, []
    for case in cases:
        route = ask(case.query).route
        if route == case.route:
            correct += 1
        else:
            wrong.append(f"{case.query!r} -> {route}, expected {case.route}")
    return correct, len(cases), wrong


def abstention(cases: list[RetrievalCase], k: int = 5) -> tuple[int, int, list[str]]:
    """An unanswerable question must not draw a confident near-miss.

    The corpus contains adjacent documents for every one of these, so the
    failure mode is real: ask about IPv6 multicast and the X100 overview is
    lexically close enough to look like an answer.
    """
    from ..agent import ask

    unanswerable = [c for c in cases if c.category == "unanswerable"]
    held, leaked = 0, []
    for case in unanswerable:
        answer = ask(case.query)
        if not answer.citations:
            held += 1
        else:
            leaked.append(f"{case.query!r} cited {[c.get('doc_id') for c in answer.citations]}")
    return held, len(unanswerable), leaked


def report(k: int = 5) -> str:
    cases = load_cases()
    lines = [f"=== retrieval quality ({len(cases)} labelled queries, "
             f"{len([c for c in cases if c.relevant])} answerable) ===", ""]

    lines.append(f"{'mode':<8} {'Recall@'+str(k):>9} {'MRR':>7} {'nDCG@'+str(k):>8}")
    scores = {mode: score_mode(cases, mode, k) for mode in MODES}
    for mode in MODES:
        s = scores[mode]
        lines.append(f"{mode:<8} {s.recall:>9.3f} {s.mrr:>7.3f} {s.ndcg:>8.3f}")

    lines += ["", f"Recall@{k} by query type:",
              f"  {'category':<18} " + "  ".join(f"{m:>7}" for m in MODES)]
    for category in sorted({c.category for c in cases if c.relevant}):
        row = [f"  {category:<18} "]
        for mode in MODES:
            values = scores[mode].by_category.get(category, [])
            row.append(f"{sum(values)/len(values):>7.3f}" if values else f"{'-':>7}")
        lines.append("  ".join(row))

    correct, total, wrong = routing_accuracy(cases)
    lines += ["", f"query routing (SQL vs RAG): {correct}/{total}"]
    lines += [f"    {w}" for w in wrong]

    held, total_unanswerable, leaked = abstention(cases, k)
    lines += ["", f"abstention on unanswerable queries: {held}/{total_unanswerable}"]
    lines += [f"    leaked: {l}" for l in leaked]
    return "\n".join(lines)
