"""Embedding backends.

Two, deliberately:

* `hashed` -- a hashed bag-of-words, no dependencies, deterministic. It
  keeps the demo and the test suite runnable with nothing installed, and it
  is the default for exactly that reason.
* `sentence-transformers` -- a real encoder, run locally.

The distinction matters more than it looks. With the hashed backend the
"dense" retrieval leg is lexical matching wearing a different metric: it
cannot disagree with BM25 about `7.2.13` because it represents that string
as the same token BM25 does. That is why the retrieval ablation reported
all three legs identical, and why the repeatedly-stated benefit of hybrid
search had no supporting evidence. Swapping the backend is what turns that
claim into something measurable.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from functools import lru_cache

from .config import CONFIG

HASHED_DIM = 256


def _slot(token: str) -> int:
    # Never `hash()`: Python salts string hashing per process, so a vector
    # written today would land in different slots tomorrow and the index
    # would rot silently rather than fail loudly.
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(),
                          "big") % HASHED_DIM


def embed_hashed(texts: list[str], tokenize) -> list[list[float]]:
    vectors = []
    for text in texts:
        vector = [0.0] * HASHED_DIM
        for token, count in Counter(tokenize(text)).items():
            vector[_slot(token)] += 1.0 + math.log(count)
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        vectors.append([v / norm for v in vector])
    return vectors


@lru_cache(maxsize=2)
def _load_sentence_transformer(model_name: str):
    """Loaded once, lazily. Importing torch costs seconds, so a CLI command
    that never embeds anything should never pay for it."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


def embed_sentence_transformers(texts: list[str], model_name: str) -> list[list[float]]:
    model = _load_sentence_transformer(model_name)
    # normalize_embeddings so cosine similarity is a plain dot product,
    # matching what the hashed backend already guarantees.
    vectors = model.encode(texts, normalize_embeddings=True,
                           show_progress_bar=False, convert_to_numpy=True)
    return [v.tolist() for v in vectors]


def embed_batch(texts: list[str], tokenize) -> list[list[float]]:
    backend = CONFIG.embedder
    if backend == "hashed":
        return embed_hashed(texts, tokenize)
    if backend == "sentence-transformers":
        return embed_sentence_transformers(texts, CONFIG.embed_model)
    raise ValueError(f"unknown embedder {backend!r}")


def signature() -> str:
    """Identifies the vector space an index was built in.

    Stored with the index and checked on load. Comparing query vectors from
    one encoder against document vectors from another produces plausible,
    meaningless rankings -- a failure with no error message, so it has to be
    caught structurally.
    """
    if CONFIG.embedder == "hashed":
        return f"hashed:{HASHED_DIM}"
    return f"{CONFIG.embedder}:{CONFIG.embed_model}"
