"""Cross-encoder reranking.

The previous reranker was token overlap plus a status prior -- a heuristic
standing in for a model. A cross-encoder reads the query and document
together, which is what lets it rank on relevance rather than similarity.

Off by default: it loads a model, and the offline demo and test suite must
run without one. `CIP_RERANKER=cross-encoder` turns it on.

An earlier measurement found this same model unusable as an *abstention*
signal -- genuine answers scored below unanswerable ones -- because
MS MARCO is web passages and these documents are terse generated
summaries. Ordering a shortlist is a weaker demand than deciding
membership, so it is measured here separately rather than assumed to fail
or assumed to work.
"""

from __future__ import annotations

import glob
import os
from functools import lru_cache

from .config import CONFIG


@lru_cache(maxsize=2)
def _load(model_name: str):
    from sentence_transformers import CrossEncoder

    cache = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    hub = os.path.join(cache, "hub") if not cache.endswith("hub") else cache
    pattern = os.path.join(hub, "models--" + model_name.replace("/", "--"),
                           "snapshots", "*")
    # Load from the snapshot directory: resolving by repo id goes through
    # the hub, which 401s on a stale token even for a public model.
    snapshots = [d for d in sorted(glob.glob(pattern)) if os.path.isdir(d)]
    return CrossEncoder(snapshots[-1] if snapshots else model_name)


def rerank(query: str, candidates: list[dict]) -> list[dict]:
    """Reorder in place by cross-encoder score. Never adds or removes.

    Fails open: a model that will not load leaves the existing order
    untouched, which is the previous behaviour rather than an empty result.
    """
    if CONFIG.reranker != "cross-encoder" or len(candidates) < 2:
        return candidates
    try:
        model = _load(CONFIG.reranker_model)
        pairs = [(query, f"{c['title']}. {c['body']}") for c in candidates]
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as exc:  # pragma: no cover - depends on model availability
        print(f"[cip.rerank] {type(exc).__name__}: {exc}; keeping existing order")
        return candidates

    for candidate, score in zip(candidates, scores):
        candidate["rerank_score"] = float(score)
    return sorted(candidates, key=lambda c: -c["rerank_score"])
