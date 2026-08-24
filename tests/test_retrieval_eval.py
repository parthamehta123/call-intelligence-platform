"""Retrieval quality gates.

These lock in measured behaviour so a change to tokenization, ranking or
the abstain rule fails here rather than silently degrading answers.
"""

from __future__ import annotations

import pytest

from cip import kb, retrieval
from cip.config import CONFIG
from cip.eval.retrieval_eval import (load_cases, ndcg_at_k, recall_at_k,
                                     reciprocal_rank, score_mode)


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("retrieval")
    original_kb, original_index = CONFIG.kb_path, retrieval.INDEX_PATH
    CONFIG.kb_path = tmp / "kb.sqlite"
    retrieval.INDEX_PATH = tmp / "index.json"
    kb.init(CONFIG.kb_path)
    with kb.connect() as conn:
        for product, issue, title, body, status in [
            ("XG482", "ROUTE_LOSS", "XG482 Route Loss",
             "Static routes are lost across power cycles on firmware 3.5.", "confirmed"),
            ("X100", "SPONTANEOUS_REBOOT", "X100 Spontaneous Reboot",
             "Device reboots without operator action on 7.2.", "observed"),
            ("PULSE7", "OVERHEATING", "PULSE7 Overheating",
             "Device runs abnormally hot.", "observed"),
            ("MERIDIAN", "EXPORT_TIMEOUT", "MERIDIAN Export Timeout",
             "Report export times out.", "observed"),
            ("MERIDIAN", "BULK_EXPORT", "MERIDIAN Bulk Export",
             "Bulk CSV export requested.", "observed"),
            ("PULSE7", "STABILITY_PRAISE", "PULSE7 Stability Praise",
             "Customers report the release is stable.", "observed"),
        ]:
            conn.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?)",
                         (f"doc::{product}::{issue}", product, title, body,
                          "validated_issue", status, "2026-08-22T00:00:00+00:00", 1))
    retrieval.refresh_index()
    yield
    CONFIG.kb_path, retrieval.INDEX_PATH = original_kb, original_index


def test_tokenizer_keeps_versions_and_drops_trailing_punctuation():
    """The bug: `hot.` indexed with its full stop never matched a query's
    `hot`, so every sentence-final word in the corpus was unmatchable."""
    tokens = retrieval.tokenize("Device runs abnormally hot. Firmware 7.2.13 on the XG-482.")
    assert "hot" in tokens and "hot." not in tokens
    assert "7.2.13" in tokens, "version strings must survive as one token"
    assert "xg-482" in tokens


def test_metrics_are_computed_correctly():
    assert recall_at_k(["a", "b"], ["a", "c"], 5) == 0.5
    assert reciprocal_rank(["x", "a"], ["a"]) == 0.5
    assert reciprocal_rank(["x"], ["a"]) == 0.0
    assert ndcg_at_k(["a"], ["a"], 5) == 1.0
    assert ndcg_at_k(["x", "a"], ["a"], 5) < 1.0


def test_exact_identifier_queries_retrieve_the_right_document(indexed):
    """The claim that identifiers need exact matching -- 3.5 must not be
    answered with the 7.2 document."""
    hits = [h["doc_id"] for h in retrieval.hybrid_search("XG-482 firmware 3.5 static routes")]
    assert hits and hits[0] == "doc::XG482::ROUTE_LOSS"


def test_unanswerable_query_returns_nothing(indexed):
    """A near-miss is worse than an empty result: it looks like an answer."""
    assert retrieval.hybrid_search("what is the warranty period for the Pulse 7") == []


def test_product_name_alone_does_not_make_a_document_relevant(indexed):
    """Topical coverage exists because product similarity is not topicality."""
    index = retrieval.Index.load(retrieval.INDEX_PATH)
    doc = index.docs["doc::PULSE7::OVERHEATING"]
    assert retrieval.topical_coverage("pulse 7 overheating", doc, index) > 0
    assert retrieval.topical_coverage("pulse 7 warranty period", doc, index) == 0


def test_retrieval_quality_floor():
    """Measured on the labelled set against the real corpus.

    Pins INDEX_PATH explicitly. The module-scoped `indexed` fixture
    repoints it at a six-document temporary corpus, so without this the
    result depended on test ordering -- passing alone and failing in the
    suite, which is worse than either outcome consistently.
    """
    from cip.config import DATA

    real_index = DATA / "index.json"
    if not real_index.exists():
        pytest.skip("no live index; run `make run` to build one")

    original = retrieval.INDEX_PATH
    retrieval.INDEX_PATH = real_index
    try:
        score = score_mode(load_cases(), "hybrid", k=5)
    finally:
        retrieval.INDEX_PATH = original

    assert score.recall >= 0.70, f"Recall@5 {score.recall:.3f} below floor"
    assert score.mrr >= 0.65, f"MRR {score.mrr:.3f} below floor"


def test_counting_questions_never_reach_the_vector_index():
    from cip.agent import ask
    for case in load_cases():
        if case.category == "routing_sql":
            assert ask(case.query).route == "sql", case.query
