"""Spend reporting.

Three paid runs happened before this existed, and none of them could say
what they cost -- the figure lived only in a billing console. Usage is now
carried on the observation row, because a counter on an executor never
reaches the driver.
"""

from __future__ import annotations

import pytest

from cip.pricing import RATES, estimate
from cip.schemas import Observation


def test_cost_is_computed_from_both_rates():
    spend = estimate("claude-opus-5", calls=1000,
                     input_tokens=1_000_000, output_tokens=1_000_000)
    rate_in, rate_out = RATES["claude-opus-5"]
    assert spend.usd == rate_in + rate_out


def test_an_unknown_model_reports_unknown_rather_than_guessing():
    """Rates go stale. A wrong number presented confidently is worse than
    an absent one."""
    spend = estimate("claude-model-from-the-future", 10, 1000, 1000)
    assert spend.usd is None
    assert "unknown" in spend.render()


def test_the_model_is_named_alongside_the_estimate():
    """So a figure can be checked against an invoice instead of trusted."""
    assert "claude-opus-5" in estimate("claude-opus-5", 1, 10, 10).render()


def test_the_rules_extractor_reports_no_spend():
    """It makes no call, so a zero must not be reported as a measured cost."""
    spend = estimate("claude-opus-5", calls=0, input_tokens=0, output_tokens=0)
    assert spend.calls == 0
    assert spend.usd == 0.0


def test_observations_carry_their_own_usage():
    observation = Observation(
        observation_id="O1", segment_id="S1", call_id="C1", customer_id="U1",
        product_id="X100", product_version="7.2", type="bug_report",
        issue_key="VPN_DISCONNECT", summary="x", severity="high", evidence="e",
        confidence=0.9, region="US", timestamp="2026-08-22T10:00:00+00:00")
    assert observation.input_tokens == 0 and observation.output_tokens == 0
    assert observation.validate() == []


# --- the cost report must count calls, not rows ----------------------------
#
# Spend was summed from observation rows, so a metered call that returned no
# signal left no trace and cost $0 in the report. On the first full Claude
# day that hid 34 of 1225 calls. The pathological case -- every call
# abstaining -- would have reported $0 against a full bill.

class _FakeUsage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o
        self.cache_read_input_tokens = 0


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    stop_reason = "end_turn"

    def __init__(self, payload, i=1000, o=100):
        self.content = [_FakeBlock(payload)]
        self.usage = _FakeUsage(i, o)


class _FakeClient:
    """Returns a real observation, then an abstention."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.messages = self

    def create(self, **_):
        return _FakeResponse(self._payloads.pop(0))


NO_SIGNAL = '{"is_product_signal": false}'
SIGNAL = ('{"is_product_signal": true, "product_id": "X100", "issue_key": '
          '"SPONTANEOUS_REBOOT", "type": "bug_report", "summary": "reboots", '
          '"severity": "high", "evidence": "The X100 reboots nightly.", '
          '"confidence": 0.9}')


def _segment(text, sid):
    from cip.schemas import Segment

    return Segment(segment_id=sid, call_id="C1", customer_id="U1",
                   timestamp="2026-08-22T12:00:00+00:00", region="US",
                   text=text, speaker_mix={"customer": 1}, customer_turns=1)


def test_a_call_that_returns_no_observation_is_still_counted(monkeypatch):
    from cip.pipeline import extract as ex

    ex.LEDGER.drain()
    client = _FakeClient([SIGNAL, NO_SIGNAL])
    monkeypatch.setattr(ex.CONFIG, "claude_model", "claude-opus-5", raising=False)

    ex.extract_claude(_segment("customer: The X100 reboots nightly.", "S1"), client=client)
    ex.extract_claude(_segment("customer: Hello there.", "S2"), client=client)

    calls = ex.LEDGER.drain()
    totals = ex.totals(calls)
    assert totals["model_calls"] == 2, "both calls were billed"
    assert totals["calls_without_observation"] == 1
    # The abstaining call's tokens must be in the total, not lost with its
    # missing row -- that is the entire bug.
    assert totals["input_tokens"] == 2000
    assert totals["output_tokens"] == 200


def test_drain_empties_so_a_caller_only_sees_its_own_calls():
    from cip.pipeline.extract import LEDGER, ModelCall

    LEDGER.drain()
    LEDGER.record(ModelCall(segment_id="S", input_tokens=1, output_tokens=1))
    assert len(LEDGER.drain()) == 1
    assert LEDGER.drain() == []


def test_totals_of_no_calls_is_zero_not_missing():
    from cip.pipeline.extract import totals

    assert totals([]) == {"model_calls": 0, "input_tokens": 0,
                          "output_tokens": 0, "cache_read_input_tokens": 0,
                          "calls_without_observation": 0, "calls_failed": 0}


def test_a_call_that_errored_is_recorded_as_an_attempt(monkeypatch):
    """A failed call left no trace at all, so the counts disagreed.

    `extract_claude` writes the ledger only after a successful response, so
    a request that raised was invisible: the router said 1225 segments went
    to the model and the extractor recorded 1223.
    """
    from cip.pipeline import extract as ex

    ex.LEDGER.drain()

    def boom(_segment, client=None):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(ex, "extract_claude", boom)
    with pytest.raises(RuntimeError):
        ex._extract_one(_segment("customer: The X100 reboots.", "S9"))

    calls = ex.LEDGER.drain()
    assert len(calls) == 1
    assert calls[0].failed is True
    assert calls[0].produced_observation is False

    t = ex.totals(calls)
    assert t["model_calls"] == 1, "an attempt is a call"
    assert t["calls_failed"] == 1
    # A failure is not an abstention: one was billed and returned nothing,
    # the other never got a response at all.
    assert t["calls_without_observation"] == 0
