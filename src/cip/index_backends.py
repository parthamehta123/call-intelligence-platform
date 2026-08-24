"""Real index structures behind retrieval.

Both legs were linear scans over a dict: every query embedded, then
compared against every document, and BM25 scored by iterating the whole
corpus. Correct at ten documents and wrong in kind at a hundred thousand,
because the cost of a query grew with the size of the corpus rather than
with the number of matches.

  VectorIndex   FAISS inner-product index over normalised vectors
  InvertedIndex term -> postings, so BM25 touches only documents that
                contain a query term

Both keep the same interface the previous scans had, so `retrieval.py`
chooses a backend rather than being rewritten around one.
"""

from __future__ import annotations

import math
from collections import defaultdict


class VectorIndex:
    """FAISS inner-product search. Vectors are L2-normalised on the way in,
    so inner product is cosine.

    `IndexFlatIP` is exact and the right choice at this corpus size --
    approximate search trades recall for speed, and there is no speed
    problem to solve at ten documents. At scale this becomes `IndexIVFFlat`
    or HNSW; the call site does not change.
    """

    def __init__(self) -> None:
        self.doc_ids: list[str] = []
        self._index = None
        self._dim = 0

    @property
    def available(self) -> bool:
        try:
            import faiss  # noqa: F401
            return True
        except Exception:
            return False

    def build(self, doc_ids: list[str], vectors: list[list[float]]) -> None:
        import faiss
        import numpy as np

        if not vectors:
            self.doc_ids, self._index = [], None
            return
        matrix = np.asarray(vectors, dtype="float32")
        # Normalise defensively: a caller that skips it would otherwise get
        # dot products silently standing in for cosine.
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.maximum(norms, 1e-9)
        self._dim = matrix.shape[1]
        self._index = faiss.IndexFlatIP(self._dim)
        self._index.add(matrix)
        self.doc_ids = list(doc_ids)

    def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        import numpy as np

        if self._index is None or not self.doc_ids:
            return []
        query = np.asarray([vector], dtype="float32")
        query /= max(float(np.linalg.norm(query)), 1e-9)
        scores, indices = self._index.search(query, min(top_k, len(self.doc_ids)))
        return [(self.doc_ids[i], float(s))
                for s, i in zip(scores[0], indices[0]) if i >= 0]


class InvertedIndex:
    """term -> [(doc_id, term_frequency)], with BM25 over the postings.

    The previous implementation scored every document for every query. This
    visits only documents containing at least one query term, which is the
    whole point of an inverted index and the difference between a scan and
    a search.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.postings: dict[str, list[tuple[str, int]]] = defaultdict(list)
        self.lengths: dict[str, int] = {}
        self.avg_len = 1.0
        self.k1, self.b = k1, b

    def build(self, documents: dict[str, list[str]]) -> None:
        self.postings = defaultdict(list)
        self.lengths = {}
        for doc_id, tokens in documents.items():
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for token, count in counts.items():
                self.postings[token].append((doc_id, count))
            self.lengths[doc_id] = len(tokens)
        self.avg_len = (sum(self.lengths.values()) / len(self.lengths)
                        if self.lengths else 1.0)

    def search(self, tokens: list[str], top_k: int | None = None) -> list[tuple[str, float]]:
        n_docs = len(self.lengths) or 1
        scores: dict[str, float] = defaultdict(float)
        for token in tokens:
            postings = self.postings.get(token)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            for doc_id, freq in postings:
                length = self.lengths.get(doc_id, 1) or 1
                scores[doc_id] += idf * (freq * (self.k1 + 1)) / (
                    freq + self.k1 * (1 - self.b + self.b * length / self.avg_len))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        return ranked[:top_k] if top_k else ranked
