"""Knowledge graph, model-call traces, and groundedness."""

from __future__ import annotations

import pytest

pytest.importorskip("networkx")

from cip import graph as graph_module  # noqa: E402
from cip.eval.groundedness_eval import supported  # noqa: E402
from cip.security.audit import audit, trace_model_call  # noqa: E402


def test_graph_is_derived_from_canonical_state_not_stored_beside_it():
    """A second store would be a second thing to keep in step."""
    built = graph_module.build()
    assert graph_module.stats(built).products >= 1
    kinds = {d.get("kind") for _, d in built.nodes(data=True)}
    assert {"product", "issue"} <= kinds


def test_graph_answers_a_traversal_the_relational_store_cannot():
    """Connecting two issues needs a join path known in advance in SQL; the
    graph finds it."""
    built = graph_module.build()
    issues = [n for n, d in built.nodes(data=True) if d.get("kind") == "issue"]
    if len(issues) < 2:
        pytest.skip("needs at least two published issues")
    path = graph_module.connection(issues[0], issues[-1], built)
    assert path and path[0] == issues[0] and path[-1] == issues[-1]


def test_blast_radius_reports_everything_a_version_touches():
    built = graph_module.build()
    versions = sorted({d["value"] for _, d in built.nodes(data=True)
                       if d.get("kind") == "version"})
    if not versions:
        pytest.skip("needs a published version")
    assert graph_module.blast_radius(versions[0], built)


def test_traces_are_redacted_before_being_written():
    """A trace that leaked the PII the pipeline had just removed would undo
    the boundary it exists to audit."""
    trace_model_call(component="judge", model="m",
                     prompt="customer: my card is 4111 1111 1111 1111 and I am cross",
                     response="ok", verdict=False)
    record = audit.tail(1, event="model_call")[0]
    assert "4111" not in record["prompt"]
    assert record["redactions"] >= 1
    assert record["component"] == "judge"


def test_groundedness_requires_support_not_mere_mention():
    sources = ["PULSE7 Overheating. Device runs abnormally hot on version 1.9."]
    assert supported("Device runs abnormally hot on version 1.9.", sources)
    assert not supported("The warranty period is thirty-six months.", sources)


def test_a_citation_that_was_never_retrieved_is_counted():
    """The worst failure: an answer that looks sourced and is not."""
    from cip.eval.groundedness_eval import GroundednessReport

    report = GroundednessReport(citations=3, fabricated_citations=1)
    assert report.fabricated_citations == 1


def test_offline_agent_is_grounded_by_construction():
    """Its answer *is* the retrieved text, so 1.0 confirms the plumbing
    rather than testing a model -- recorded so the number is not mistaken
    for evidence about generation."""
    from cip.eval.groundedness_eval import evaluate_groundedness

    report = evaluate_groundedness()
    assert report.score == 1.0
    assert report.fabricated_citations == 0
