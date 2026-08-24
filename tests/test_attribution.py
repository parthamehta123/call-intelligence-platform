"""Diarization-aware extraction.

An agent restating a known defect is not a customer report. This mattered
in practice: before these rules, an agent-only segment produced a customer
observation at confidence 0.83, attributed to that caller.

The damage is not random. Agents restate the *most reported* defects most
often, so misattribution concentrates on the issues already closest to the
auto-accept threshold -- where a handful of phantom customers flips an
issue from human review to published product truth.
"""

from __future__ import annotations

import pytest

from cip.config import CONFIG
from cip.pipeline.aggregate import aggregate
from cip.pipeline.extract import extract_rules
from cip.pipeline.preprocess import segment_call
from cip.pipeline.reconcile import reconcile
from cip.pipeline.route import route, score_segment
from cip.schemas import CallRecord, Observation, Segment

AGENT_CLAIM = "Yes, we're aware firmware 7.2 makes the VPN keep disconnecting."
CUSTOMER_CLAIM = "Firmware 7.2 makes the VPN keep disconnecting for us."


def _segment(text: str, *, attribution: float = 1.0, customer_turns: int = 1) -> Segment:
    return Segment(
        segment_id="S1", call_id="C1", customer_id="U1",
        timestamp="2026-08-22T10:00:00+00:00", region="US", text=text,
        speaker_mix={"customer": customer_turns}, product_hint="X100",
        customer_turns=customer_turns, attribution_confidence=attribution)


def _routed(segment: Segment) -> Segment | None:
    routed = list(route([segment]))
    return routed[0] if routed else None


def test_agent_restating_a_defect_never_becomes_an_observation():
    segment = _segment(f"agent: {AGENT_CLAIM}", customer_turns=0, attribution=0.0)
    assert _routed(segment) is None, "agent-only speech must not even reach a model"


def test_agent_claim_beside_customer_smalltalk_is_still_not_a_report():
    segment = _segment(f"agent: {AGENT_CLAIM}\ncustomer: okay, thanks for letting me know.")
    routed = _routed(segment)
    assert routed is None or extract_rules(routed) is None


def test_the_same_words_from_the_customer_are_extracted():
    routed = _routed(_segment(f"customer: {CUSTOMER_CLAIM}"))
    assert routed is not None
    observation = extract_rules(routed)
    assert observation is not None
    assert observation.issue_key == "VPN_DISCONNECT"
    assert observation.speaker == "customer"


def test_router_does_not_pay_to_read_agent_only_segments():
    """Cost, not just correctness: nothing extractable can come from them."""
    assert score_segment(_segment(f"agent: {AGENT_CLAIM}", customer_turns=0)) == 0.0


def test_weak_diarization_scales_confidence_rather_than_dropping():
    """Uncertain attribution should demand more corroboration, not vanish."""
    strong = extract_rules(_routed(_segment(f"customer: {CUSTOMER_CLAIM}", attribution=1.0)))
    weak = extract_rules(_routed(_segment(f"customer: {CUSTOMER_CLAIM}", attribution=0.4)))
    assert strong and weak
    assert weak.confidence < strong.confidence
    assert weak.attribution_confidence == 0.4


def test_observation_from_a_non_customer_speaker_fails_validation():
    observation = Observation(
        observation_id="O1", segment_id="S1", call_id="C1", customer_id="U1",
        product_id="X100", product_version="7.2", type="bug_report",
        issue_key="VPN_DISCONNECT", summary="x", severity="high", evidence="e",
        confidence=0.9, region="US", timestamp="2026-08-22T10:00:00+00:00",
        speaker="agent")
    assert any("only customer claims" in e for e in observation.validate())


def _obs(i: int, attribution: float) -> Observation:
    return Observation(
        observation_id=f"O{i}", segment_id=f"S{i}", call_id=f"C{i}",
        customer_id=f"U{i}", product_id="X100", product_version="7.2",
        type="bug_report", issue_key="VPN_DISCONNECT", summary="VPN disconnects",
        severity="high", evidence="vpn drops", confidence=0.9, region="US",
        timestamp="2026-08-22T10:00:00+00:00", attribution_confidence=attribution)


def test_mislabelled_turns_cannot_manufacture_corroboration():
    """The attack this defends: twenty mislabelled agent turns plus four real
    customers must not auto-publish as twenty-four corroborating reports."""
    items = [_obs(i, 0.95) for i in range(4)] + [_obs(100 + i, 0.40) for i in range(20)]
    candidate = aggregate(items)[0]

    assert candidate.mentions == 24
    assert candidate.distinct_customers == 4, "weak attribution must not corroborate"
    assert reconcile([candidate])[0].decision == "review"


def test_well_attributed_reports_still_publish():
    """The guard must not block legitimate corroboration."""
    candidate = aggregate([_obs(i, 0.95) for i in range(12)])[0]
    assert candidate.distinct_customers == 12
    assert reconcile([candidate])[0].decision == "auto_accept"


def test_missing_speaker_confidence_is_treated_as_trusted():
    """Historic partitions predate the field; absent must not mean zero."""
    call = CallRecord(
        call_id="C1", customer_id="U1", timestamp="2026-08-22T10:00:00+00:00",
        region="US", channel="voice", product_hint="X100",
        turns=[{"speaker": "customer", "text": CUSTOMER_CLAIM, "start_time": 0.0}])
    segment = segment_call(call)[0]
    assert segment.attribution_confidence == 1.0
    assert segment.customer_turns == 1


def test_attribution_floor_is_actually_wired_up():
    assert 0.0 < CONFIG.attribution_floor < 1.0


def test_issue_and_product_come_from_the_same_utterance():
    """A segment carrying two topics must not mint a hybrid observation.

    A MERIDIAN caller plus a relabelled agent turn about the X100 VPN defect
    produced MERIDIAN/VPN_DISCONNECT -- a pairing nobody reported. Matching
    patterns across concatenated customer text paired an issue from one
    sentence with a product resolved from another. It surfaced as a review
    queue full of cross-products: PULSE7/SPONTANEOUS_REBOOT,
    XG482/OVERHEATING, MERIDIAN/VPN_DISCONNECT.
    """
    segment = _segment(
        "customer: The cloud console times out when I export an audit report.\n"
        "customer: Yes, we're aware firmware 7.2 makes the VPN keep disconnecting.")
    routed = _routed(segment)
    assert routed is not None
    observation = extract_rules(routed)
    assert observation is not None

    pairings = {
        ("MERIDIAN", "EXPORT_TIMEOUT"), ("X100", "VPN_DISCONNECT"),
    }
    assert (observation.product_id, observation.issue_key) in pairings, (
        f"invented pairing {observation.product_id}/{observation.issue_key}")


def test_evidence_is_quoted_from_the_utterance_that_matched():
    segment = _segment(
        "customer: my invoice is wrong.\n"
        f"customer: {CUSTOMER_CLAIM}")
    observation = extract_rules(_routed(segment))
    assert observation is not None
    assert "VPN" in observation.evidence
    assert "invoice" not in observation.evidence
