"""Pipeline behaviour: the funnel, entity resolution, aggregation, conflicts."""

import pytest

from cip.catalog import resolve_product, resolve_version
from cip.pipeline.aggregate import aggregate
from cip.pipeline.extract import extract_rules
from cip.pipeline.preprocess import preprocess, segment_call
from cip.pipeline.reconcile import reconcile
from cip.pipeline.route import route, score_segment
from cip.schemas import CallRecord, Observation, Segment


def _call(*texts: str, product_hint=None, call_id="C1", customer="U1") -> CallRecord:
    return CallRecord(
        call_id=call_id, customer_id=customer, timestamp="2026-08-22T10:00:00+00:00",
        region="US", channel="voice", product_hint=product_hint,
        turns=[{"speaker": "customer", "text": t, "start_time": 0.0} for t in texts])


def test_catalog_resolves_aliases_and_rejects_unknown_versions():
    assert resolve_product("my wifi box is dead")[0] == "PULSE7"
    assert resolve_product("lovely weather today")[0] is None
    assert resolve_version("firmware 7.2", "X100") == "7.2"
    assert resolve_version("firmware 99.9", "X100") is None


def test_funnel_drops_small_talk_and_keeps_product_signal():
    chatter = segment_call(_call("Hi, how are you today?"))[0]
    signal = segment_call(_call("The X100 VPN keeps disconnecting on firmware 7.2"))[0]
    assert score_segment(chatter) == 0.0
    assert score_segment(signal) > 0.5


def test_product_hint_rescues_calls_that_never_name_the_product():
    segment = segment_call(_call("The VPN keeps disconnecting every ten minutes",
                                 product_hint="X100"))[0]
    routed = list(route([segment]))
    assert routed and routed[0].product_id == "X100"


def test_pii_is_redacted_before_persistence():
    segments = list(preprocess([_call("You can reach me at dana@example.com")]))
    assert "dana@example.com" not in segments[0].text
    assert segments[0].pii_redactions == 1


def test_duplicate_segments_from_one_customer_are_counted_once():
    text = "The X100 VPN keeps disconnecting"
    segments = list(preprocess([_call(text, call_id="C1"), _call(text, call_id="C2")]))
    assert len(segments) == 1


def test_extraction_emits_schema_valid_observations():
    segment = list(route(segment_call(
        _call("The X100 VPN keeps disconnecting on firmware 7.2"))))[0]
    obs = extract_rules(segment)
    assert obs is not None
    assert obs.validate() == []
    assert obs.issue_key == "VPN_DISCONNECT"
    assert obs.product_version == "7.2"


def _obs(customer: str, summary="VPN disconnects", **kw) -> Observation:
    base = dict(observation_id=f"O{customer}", segment_id="S", call_id="C",
                customer_id=customer, product_id="X100", product_version="7.2",
                type="bug_report", issue_key="VPN_DISCONNECT", summary=summary,
                severity="high", evidence="vpn drops", confidence=0.9, region="US",
                timestamp="2026-08-22T10:00:00+00:00")
    base.update(kw)
    return Observation(**base)


def test_repeat_callers_do_not_inflate_corroboration():
    candidate = aggregate([_obs("U1") for _ in range(40)])[0]
    assert candidate.mentions == 40
    assert candidate.distinct_customers == 1
    assert reconcile([candidate])[0].decision == "review"


def test_thousands_of_reports_collapse_into_one_issue():
    candidates = aggregate([_obs(f"U{i}") for i in range(5000)])
    assert len(candidates) == 1
    assert candidates[0].distinct_customers == 5000
    assert reconcile(candidates)[0].decision == "auto_accept"


def test_contradictory_claims_go_to_a_human():
    observations = [_obs(f"U{i}") for i in range(20)]
    observations += [_obs(f"V{i}", summary="RESOLVED CLAIM: VPN disconnects")
                     for i in range(6)]
    candidate = reconcile(aggregate(observations))[0]
    assert candidate.decision == "review"
    assert "resolved" in candidate.decision_reason


def test_spec_corrections_always_require_sign_off():
    observations = [_obs(f"U{i}", type="spec_correction", issue_key="SFP_PORT_COUNT")
                    for i in range(500)]
    assert reconcile(aggregate(observations))[0].decision == "review"


def test_review_queue_does_not_duplicate_across_runs(tmp_path, monkeypatch):
    """Re-running a day must refresh the queue item, not append a copy.

    With created_at in the primary key, three runs left three identical open
    items -- a queue that grows with run count rather than with distinct
    problems.
    """
    from cip import kb
    from cip.config import CONFIG
    from cip.schemas import IssueCandidate

    monkeypatch.setattr(CONFIG, "kb_path", tmp_path / "kb.sqlite")
    kb.init(CONFIG.kb_path)

    candidate = IssueCandidate(
        product_id="X100", issue_key="VPN_DISCONNECT", type="bug_report",
        summary="VPN disconnects", severity="high", mentions=50,
        distinct_customers=40, regions=["US"], versions=["7.2"],
        first_seen="2026-08-22T00:00:00+00:00", last_seen="2026-08-22T23:00:00+00:00",
        mean_confidence=0.9, evidence_ids=[], decision="review",
        decision_reason="contradictory reports")

    kb.enqueue_review(candidate)
    first = kb.query("SELECT created_at FROM review_queue")[0]["created_at"]

    candidate.decision_reason = "contradictory reports (updated)"
    kb.enqueue_review(candidate)
    kb.enqueue_review(candidate)

    rows = kb.query("SELECT product_id, issue_key, reason, created_at FROM review_queue")
    assert len(rows) == 1
    assert rows[0]["reason"].endswith("(updated)"), "evidence should refresh"
    assert rows[0]["created_at"] == first, "queue age must survive a re-run"
