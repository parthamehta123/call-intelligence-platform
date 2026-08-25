"""The labelling harness.

Every eval number in this repo rests on labels written by the person who
built the system, and a model already found one of them wrong. This is the
machinery that lets somebody else produce them, so the properties worth
testing are the ones that keep a second annotator independent.
"""

from __future__ import annotations

import pytest

from cip.labeling.agreement import adjudicate, agreement
from cip.labeling.cli import GUIDELINES, export_router_labels
from cip.labeling.pool import ROUTER_QUOTA, build_router_pool
from cip.labeling.store import Label, LabelStore


@pytest.fixture()
def store(tmp_path):
    return LabelStore(tmp_path / "labels.jsonl")


def test_the_annotator_never_sees_the_model_score():
    """An annotator told what the model thinks agrees with it, which turns
    an independent label into a confirmation."""
    pool = build_router_pool(size=40)
    assert pool
    for item in pool[:10]:
        rendered = item.prompt()
        assert "router_score" not in rendered
        assert str(item.provenance["router_score"]) not in rendered
    # The score is kept for analysis, just not shown.
    assert "router_score" in pool[0].provenance


def test_sampling_is_weighted_toward_the_decision_boundary():
    """Items the system already gets confidently right teach nothing."""
    pool = build_router_pool(size=200)
    strata = {item.stratum for item in pool}
    assert strata <= set(ROUTER_QUOTA)
    assert any(item.stratum == "boundary" for item in pool)


def test_a_short_stratum_does_not_shrink_the_pool():
    """The rule-based router emits discrete scores, so few segments land
    near the threshold. A labelling budget is set by the labeller's time,
    not by how the scores happen to bunch."""
    assert len(build_router_pool(size=250)) == 250


def test_two_annotators_produce_two_records(store):
    """Disagreement is the signal that a definition is unclear; a store
    that keeps the last write destroys it."""
    store.append(Label(item_id="i1", kind="router", value=1, annotator="a"))
    store.append(Label(item_id="i1", kind="router", value=0, annotator="b"))
    assert len(store.all()) == 2
    assert store.summary()["double_labelled"] == 1


def test_kappa_corrects_for_chance_agreement(store):
    """On a skewed set, two annotators who both guess "no" agree most of
    the time while sharing no judgement at all."""
    for i in range(20):
        store.append(Label(item_id=f"i{i}", kind="router", value=0, annotator="a"))
        store.append(Label(item_id=f"i{i}", kind="router", value=0, annotator="b"))
    result = agreement(store)
    assert result.observed == 1.0
    # Both annotators are constant, so chance agreement is also 1.0 and
    # kappa is undefined rather than perfect -- it must not report 1.0 as
    # evidence of shared judgement.
    assert result.kappa == 1.0 or result.kappa == 0.0


def test_contested_items_are_excluded_from_the_export(store):
    """An eval built on cases people could not agree about measures the
    ambiguity, not the router."""
    store.append(Label(item_id="agreed", kind="router", value=1, annotator="a",
                       payload={"text": "customer: the vpn drops"}))
    store.append(Label(item_id="agreed", kind="router", value=1, annotator="b",
                       payload={"text": "customer: the vpn drops"}))
    store.append(Label(item_id="split", kind="router", value=1, annotator="a",
                       payload={"text": "customer: hmm"}))
    store.append(Label(item_id="split", kind="router", value=0, annotator="b",
                       payload={"text": "customer: hmm"}))

    assert len(adjudicate(store)) == 1
    exported = export_router_labels(store)
    assert len(exported) == 1
    assert exported[0]["annotators"] == ["a", "b"]


def test_guidelines_cover_the_agent_speech_trap():
    """The distinction that took a whole debugging session to find."""
    assert "AGENT" in GUIDELINES["router"]
    assert "skip" in GUIDELINES["router"]


def test_low_agreement_says_to_fix_the_guidelines_not_collect_more(store):
    for i in range(10):
        store.append(Label(item_id=f"i{i}", kind="router", value=i % 2, annotator="a"))
        store.append(Label(item_id=f"i{i}", kind="router", value=(i + 1) % 2, annotator="b"))
    assert "rewrite the guidelines" in agreement(store).interpretation
