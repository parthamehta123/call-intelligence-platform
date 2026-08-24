"""Hybrid retrieval over validated knowledge.

Two things matter more than the choice of vector store:

1. **Only validated documents are indexed.** The corpus is built from the
   knowledge base, never from raw transcripts. An injected instruction
   sitting in a call therefore has no path into an answer -- it was never
   indexed in the first place.

2. **Lexical retrieval is not optional.** "Does XG-482 firmware 7.2.13
   have the route-loss bug?" turns on exact identifiers, and dense vectors
   place `7.2.13` and `7.2.1` at almost the same point. BM25 finds the
   token; embeddings find the paraphrase; RRF fuses the two rankings and a
   reranker orders the survivors.

The embedding here is a hashed bag-of-words, chosen so the demo runs with
no dependencies. Swap `embed()` for a real embedding model and nothing
else in this module changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import kb
from .config import CONFIG, DATA

INDEX_PATH = DATA / "index.json"
DIM = 256

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\.\-_]*")
_STOP = {"the", "a", "an", "is", "are", "of", "and", "to", "in", "for", "on", "with",
         "it", "this", "that", "was", "were", "be", "by", "from", "as", "at", "or"}


def tokenize(text: str) -> list[str]:
    """Tokens keep internal dots and hyphens, and shed trailing ones.

    The pattern allows dots so version strings survive as single tokens --
    `7.2.13` must not become `7`, `2`, `13`. Left greedy, it also swallowed
    sentence-final punctuation: "runs abnormally hot." indexed as `hot.`,
    which never matched a query's `hot`. Every sentence-final word in the
    corpus was silently unmatchable, quietly depressing BM25.
    """
    return [t for t in (m.strip(".-_") for m in _TOKEN.findall(text.lower()))
            if t not in _STOP and len(t) > 1]


def embed(text: str) -> list[float]:
    """Single-text convenience. Prefer `embed_many` -- a real encoder is far
    faster per item in a batch, and indexing embeds whole document sets."""
    return embed_many([text])[0]


def embed_many(texts: list[str]) -> list[list[float]]:
    from .embedding import embed_batch

    return embed_batch(texts, tokenize)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class Doc:
    doc_id: str
    product_id: str | None
    title: str
    body: str
    source: str
    status: str
    tokens: list[str]
    vector: list[float]


class Index:
    """BM25 statistics + vectors, persisted as JSON."""

    def __init__(self) -> None:
        self.docs: dict[str, Doc] = {}
        self.df: Counter[str] = Counter()
        self.avg_len: float = 1.0

    # -- incremental maintenance (CDC) ------------------------------------
    def upsert(self, rows: list[dict]) -> list[str]:
        """Re-embed only what changed. Re-indexing 10 TB nightly is the
        mistake that makes these systems unaffordable; the KB flags the
        handful of documents that actually moved."""
        # Encoded as one batch. Per-document calls are fine for a hashed
        # bag-of-words and badly wasteful for a real model.
        texts = [f"{row['title']}\n{row['body']}" for row in rows]
        vectors = embed_many(texts) if texts else []

        for row, text, vector in zip(rows, texts, vectors):
            if row["doc_id"] in self.docs:
                self._remove_stats(self.docs[row["doc_id"]])
            doc = Doc(
                doc_id=row["doc_id"], product_id=row["product_id"], title=row["title"],
                body=row["body"], source=row["source"], status=row["status"],
                tokens=tokenize(text), vector=vector,
            )
            self.docs[doc.doc_id] = doc
            self.df.update(set(doc.tokens))
        self._recompute_avg()
        return [row["doc_id"] for row in rows]

    def _remove_stats(self, doc: Doc) -> None:
        for token in set(doc.tokens):
            self.df[token] -= 1
            if self.df[token] <= 0:
                del self.df[token]

    def _recompute_avg(self) -> None:
        if self.docs:
            self.avg_len = sum(len(d.tokens) for d in self.docs.values()) / len(self.docs)

    # -- scoring -----------------------------------------------------------
    def bm25(self, query: str, k1: float = 1.5, b: float = 0.75) -> list[tuple[str, float]]:
        n_docs = len(self.docs) or 1
        query_tokens = tokenize(query)
        scores: dict[str, float] = {}
        for doc in self.docs.values():
            counts = Counter(doc.tokens)
            length = len(doc.tokens) or 1
            score = 0.0
            for token in query_tokens:
                freq = counts.get(token, 0)
                if not freq:
                    continue
                idf = math.log(1 + (n_docs - self.df.get(token, 0) + 0.5) /
                               (self.df.get(token, 0) + 0.5))
                score += idf * (freq * (k1 + 1)) / (
                    freq + k1 * (1 - b + b * length / self.avg_len))
            if score:
                scores[doc.doc_id] = score
        return sorted(scores.items(), key=lambda kv: -kv[1])

    def dense(self, query: str) -> list[tuple[str, float]]:
        query_vector = embed(query)
        scored = [(doc_id, cosine(query_vector, doc.vector))
                  for doc_id, doc in self.docs.items()]
        return sorted([s for s in scored if s[1] > 0], key=lambda kv: -kv[1])

    # -- persistence -------------------------------------------------------
    def save(self, path: Path | None = None) -> None:
        # Resolved at call time, not bound as a default. A default argument
        # is evaluated once at import, so pointing INDEX_PATH at a temporary
        # directory had no effect and writes went to the real index.
        path = Path(path or INDEX_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        from .embedding import signature

        path.write_text(json.dumps({
            "embedding": signature(),
            "docs": {k: v.__dict__ for k, v in self.docs.items()},
            "df": dict(self.df),
            "avg_len": self.avg_len,
        }))

    @staticmethod
    def load(path: Path | None = None) -> "Index":
        path = Path(path or INDEX_PATH)
        index = Index()
        if not path.exists():
            return index
        from .embedding import signature

        raw = json.loads(path.read_text())
        # Vectors from one encoder compared against queries from another
        # produce plausible, meaningless rankings and no error at all. An
        # index built in a different vector space is treated as absent, so
        # the next refresh rebuilds it.
        if raw.get("embedding") != signature():
            return index
        index.docs = {k: Doc(**v) for k, v in raw["docs"].items()}
        index.df = Counter(raw["df"])
        index.avg_len = raw["avg_len"]
        return index


def refresh_index() -> int:
    """Pull CDC-flagged documents out of the KB and re-embed just those.

    Changing the embedding backend invalidates the whole index, so the
    incremental path is skipped and everything is re-encoded once.
    """
    index = Index.load()
    dirty = kb.dirty_documents()
    if not index.docs:
        # Either a first build or a vector-space change; either way the
        # dirty flags are not enough on their own.
        dirty = kb.query("SELECT * FROM documents")
    updated = index.upsert(dirty)
    index.save()
    kb.mark_clean(updated)
    return len(updated)


def _extract_filters(query: str) -> dict[str, str]:
    """Metadata pre-filtering. A product named in the question shrinks the
    candidate set before any scoring happens."""
    from .catalog import resolve_product
    product_id, confidence = resolve_product(query)
    return {"product_id": product_id} if product_id and confidence >= 0.85 else {}


def hybrid_search(query: str, top_k: int | None = None,
                  mode: str = "hybrid") -> list[dict]:
    """`mode` exists so the legs can be measured separately.

    "hybrid retrieval beats either leg" is the load-bearing claim behind
    this module, and an ablation is the only thing that can support it.
    `bm25` and `dense` run the identical filter and rerank path so the
    comparison isolates the retrieval leg and nothing else.
    """
    top_k = top_k or CONFIG.top_k
    index = Index.load()
    if not index.docs:
        return []

    filters = _extract_filters(query)
    allowed = {
        doc_id for doc_id, doc in index.docs.items()
        if not filters or doc.product_id == filters["product_id"]
    }

    lexical = [(d, s) for d, s in index.bm25(query) if d in allowed][:20]
    dense = [(d, s) for d, s in index.dense(query) if d in allowed][:20]

    rankings = {"hybrid": (lexical, dense),
                "bm25": (lexical,), "dense": (dense,)}[mode]
    dense_scores = dict(dense) if mode in ("hybrid", "dense") else {}

    # Reciprocal rank fusion: rank-based, so the two score scales never
    # need calibrating against each other.
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, (doc_id, _) in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (CONFIG.rrf_k + rank + 1)

    ranked = sorted(fused.items(), key=lambda kv: -kv[1])[: top_k * 3]
    results = []
    for doc_id, score in ranked:
        doc = index.docs[doc_id]
        coverage = topical_coverage(query, doc, index)
        similarity = dense_scores.get(doc_id, 0.0)

        # Admit on lexical coverage OR semantic similarity.
        #
        # Coverage alone was a lexical gate bolted onto semantic retrieval,
        # and it cancelled the encoder: asked for "a way to download
        # everything at once", the real model ranked the bulk-export
        # document first and the gate dropped it, because it shares no
        # literal term with "Bulk CSV export requested". Every document
        # failed, and the ablation reported all three legs identical.
        #
        # Coverage still carries the hashed backend, where similarity is
        # only lexical overlap wearing a different metric.
        if coverage <= 0.0 and similarity < CONFIG.dense_floor:
            continue
        entry = _rerank_entry(doc, query, score)
        entry["coverage"] = round(coverage, 3)
        entry["similarity"] = round(similarity, 3)
        results.append(entry)
    results.sort(key=lambda r: -r["score"])
    results = results[:top_k]

    # The judge runs last, on the ranked shortlist only. Asking it about
    # every candidate would multiply cost by the corpus; asking it about the
    # documents actually about to be cited bounds the work at top_k per
    # query. It can only remove -- it never reorders and never adds.
    if CONFIG.judge != "none":
        from .judge import claim_view, judge as judge_relevance

        results = [r for r in results
                   if judge_relevance(query, claim_view(r["title"], r["body"]))]
    return results


# A term in more than this share of the corpus tells you nothing about
# which document to pick. "customers" appears in every validated issue
# document ("Reported by N distinct customers"), so counting it as topical
# overlap would make every query look covered.
_MAX_DF_RATIO = 0.5


def _stem(token: str) -> str:
    """Crude suffix stripping, used ONLY for the coverage check.

    Coverage needs "reboot" to match "reboots" and "export" to match
    "exporting"; requiring exact tokens dropped genuine answers and cut
    Recall@5 from 0.818 to 0.576. BM25 keeps working on unstemmed tokens,
    so ranking behaviour is unchanged -- this affects the abstain decision
    only.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def discriminating_terms(query: str, index: "Index") -> set[str]:
    """Query terms that actually narrow the corpus.

    Product names are removed deliberately. "Do you support IPv6 multicast
    routing on the X100" matches the X100 overview strongly on the product
    alone, and that similarity says nothing about whether multicast is
    covered -- which is exactly how an unanswerable question draws a
    confident citation.
    """
    from .catalog import load_catalog

    product_tokens: set[str] = set()
    for product in load_catalog():
        for phrase in [product["product_id"], product["canonical_name"],
                       *product["aliases"], *product.get("generic_aliases", [])]:
            product_tokens.update(tokenize(phrase))

    n_docs = len(index.docs) or 1
    return {
        token for token in tokenize(query)
        if token not in product_tokens
        and index.df.get(token, 0) / n_docs <= _MAX_DF_RATIO
    }


def topical_coverage(query: str, doc: Doc, index: "Index") -> float:
    """Share of the query's discriminating terms the document actually contains."""
    terms = discriminating_terms(query, index)
    if not terms:
        # Nothing but product names and boilerplate: a browse query, where
        # returning the product's documents is the right behaviour.
        return 1.0
    doc_stems = {_stem(token) for token in doc.tokens}
    present = sum(1 for term in terms if _stem(term) in doc_stems)
    return present / len(terms)


def _rerank_entry(doc: Doc, query: str, fused_score: float) -> dict:
    """Cheap stand-in for a cross-encoder: exact-token overlap plus a
    freshness/status prior. Confirmed engineering facts outrank raw
    customer observations when both match."""
    query_tokens = set(tokenize(query))
    overlap = len(query_tokens & set(doc.tokens)) / (len(query_tokens) or 1)
    status_prior = {"confirmed": 0.15, "published": 0.10, "observed": 0.05}.get(doc.status, 0.0)
    return {
        "doc_id": doc.doc_id,
        "product_id": doc.product_id,
        "title": doc.title,
        "body": doc.body,
        "source": doc.source,
        "status": doc.status,
        "score": round(fused_score * 10 + overlap + status_prior, 4),
    }
