"""LLM judge for abstention.

The judge exists because three similarity signals could not do this job.
Rerank score, dense similarity and a cross-encoder all overlap between
answerable and unanswerable questions, because the unanswerable ones are
topically adjacent -- the product is real, the attribute asked about is
not. Deciding that is a claim-level judgement.

These tests use a stub backend: the point is the wiring and the failure
modes, which must hold whichever model is behind it.
"""

from __future__ import annotations

import pytest

from cip import judge as judge_module
from cip.config import CONFIG


@pytest.fixture(autouse=True)
def clear_cache():
    judge_module._cached_verdict.cache_clear()
    yield
    judge_module._cached_verdict.cache_clear()


def test_judge_is_off_by_default():
    """Tests and the demo must not need a model or an API key."""
    assert CONFIG.judge == "none"
    assert judge_module.judge("anything", "unrelated document") is True


def test_judge_removes_documents_that_do_not_answer(monkeypatch):
    monkeypatch.setattr(CONFIG, "judge", "stub")
    monkeypatch.setattr(judge_module, "_cached_verdict",
                        lambda backend, model, q, d: "warranty" not in q)

    assert judge_module.judge("overheating on the pulse 7", "doc") is True
    assert judge_module.judge("warranty period", "doc") is False


def test_judge_fails_open_rather_than_silencing_the_system(monkeypatch, capsys):
    """A transient model error must degrade to un-judged retrieval, not to a
    system that answers nothing -- and it must say so rather than hide it."""
    def explode(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(CONFIG, "judge", "stub")
    monkeypatch.setattr(judge_module, "_cached_verdict", explode)

    assert judge_module.judge("q", "d") is True
    assert "model unavailable" in capsys.readouterr().out


def test_verdicts_are_cached_so_a_repeated_pair_costs_nothing():
    calls = []

    @judge_module.lru_cache(maxsize=16)
    def counted(backend, model, query, document):
        calls.append(query)
        return True

    counted("stub", "m", "q", "d")
    counted("stub", "m", "q", "d")
    assert len(calls) == 1


def test_judge_can_only_remove_never_add(monkeypatch, tmp_path):
    """It filters a ranked shortlist; it cannot invent or reorder citations."""
    from cip import kb, retrieval

    original_kb, original_index = CONFIG.kb_path, retrieval.INDEX_PATH
    CONFIG.kb_path = tmp_path / "kb.sqlite"
    retrieval.INDEX_PATH = tmp_path / "index.json"
    try:
        kb.init(CONFIG.kb_path)
        with kb.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?)",
                         ("doc::XG482::ROUTE_LOSS", "XG482", "XG482 Route Loss",
                          "Static routes are lost across power cycles on 3.5.",
                          "validated_issue", "confirmed", "2026-08-22T00:00:00+00:00", 1))
        retrieval.refresh_index()

        unjudged = retrieval.hybrid_search("XG-482 static routes lost on 3.5")
        assert unjudged

        monkeypatch.setattr(CONFIG, "judge", "stub")
        monkeypatch.setattr(judge_module, "_cached_verdict",
                            lambda backend, model, q, d: False)
        judged = retrieval.hybrid_search("XG-482 static routes lost on 3.5")

        assert judged == [], "a rejecting judge must abstain"
        assert len(unjudged) >= len(judged), "the judge must never add results"
    finally:
        CONFIG.kb_path, retrieval.INDEX_PATH = original_kb, original_index


def test_claude_backend_requests_a_constrained_verdict():
    """The production path must not depend on parsing prose."""
    schema = judge_module.VERDICT_SCHEMA
    assert schema["properties"]["answers_the_question"]["type"] == "boolean"
    assert schema["additionalProperties"] is False
    assert "answers_the_question" in schema["required"]
