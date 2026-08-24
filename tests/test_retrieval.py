"""Retrieval: hybrid ranking, exact identifiers, and corpus provenance."""

import pytest

from cip import kb, retrieval
from cip.agent import ask
from cip.config import CONFIG


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "kb_path", tmp_path / "kb.sqlite")
    monkeypatch.setattr(retrieval, "INDEX_PATH", tmp_path / "index.json")
    kb.init(CONFIG.kb_path)
    with kb.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?)",
            ("doc::XG482::ROUTE_LOSS", "XG482", "XG482 Route Loss",
             "Static routes are lost across power cycles on firmware 3.5. "
             "Reported by 115 customers. Status confirmed.",
             "validated_issue", "confirmed", "2026-08-22T00:00:00+00:00", 1))
    retrieval.refresh_index()
    return tmp_path


def test_embedding_slots_are_stable_across_processes():
    # A salted hash would make the persisted index silently useless.
    from cip.embedding import _slot

    assert _slot("firmware") == _slot("firmware")
    assert retrieval.embed("vpn drops") == retrieval.embed("vpn drops")


def test_index_built_by_a_different_encoder_is_not_reused(tmp_path, monkeypatch):
    """Vectors from one encoder scored against queries from another give
    plausible, meaningless rankings and raise nothing. The signature makes
    that failure structural instead of silent."""
    from cip.config import CONFIG

    index = retrieval.Index()
    index.upsert([{"doc_id": "d1", "product_id": "X100", "title": "t",
                   "body": "static routes lost", "source": "validated_issue",
                   "status": "observed"}])
    path = tmp_path / "index.json"
    index.save(path)
    assert retrieval.Index.load(path).docs, "same encoder should reload"

    monkeypatch.setattr(CONFIG, "embedder", "sentence-transformers")
    assert not retrieval.Index.load(path).docs, "different encoder must invalidate"


def test_exact_identifier_query_finds_the_right_document(seeded):
    hits = retrieval.hybrid_search("XG-482 firmware 3.5 static routes")
    assert hits and hits[0]["doc_id"] == "doc::XG482::ROUTE_LOSS"


def test_only_validated_documents_are_indexed(seeded):
    index = retrieval.Index.load(retrieval.INDEX_PATH)
    assert all(doc.source in ("product_doc", "validated_issue")
               for doc in index.docs.values())


def test_incremental_reindex_touches_only_changed_documents(seeded):
    assert retrieval.refresh_index() == 0        # nothing dirty
    with kb.connect() as conn:
        conn.execute("UPDATE documents SET dirty=1 WHERE doc_id = 'doc::XG482::ROUTE_LOSS'")
    assert retrieval.refresh_index() == 1


def test_counting_questions_route_to_sql_not_vectors(seeded):
    assert ask("How many customers reported route loss?").route == "sql"
    assert ask("Describe the XG-482 route loss problem").route == "rag"


def test_sql_route_narrows_to_the_issue_named_in_the_question(seeded):
    from cip import tools
    from cip.security.declassify import declassify_candidate
    from cip.schemas import IssueCandidate

    for key in ("ROUTE_LOSS", "OVERHEATING"):
        candidate = IssueCandidate(
            product_id="XG482" if key == "ROUTE_LOSS" else "PULSE7", issue_key=key,
            type="bug_report", summary=key, severity="high", mentions=100,
            distinct_customers=90, regions=["US"], versions=["3.5"],
            first_seen="2026-08-22T00:00:00+00:00", last_seen="2026-08-22T23:00:00+00:00",
            mean_confidence=0.9, evidence_ids=[], decision="auto_accept")
        tools.publish_issue_update(role=tools.WRITER_SERVICE,
                                   product_id=candidate.product_id, issue_key=key,
                                   candidate=declassify_candidate(candidate, []),
                                   run_id="R1")

    answer = ask("How many customers reported route loss?")
    assert answer.route == "sql"
    assert "ROUTE_LOSS" in answer.answer and "OVERHEATING" not in answer.answer
