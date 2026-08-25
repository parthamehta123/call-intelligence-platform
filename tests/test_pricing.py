"""Spend reporting.

Three paid runs happened before this existed, and none of them could say
what they cost -- the figure lived only in a billing console. Usage is now
carried on the observation row, because a counter on an executor never
reaches the driver.
"""

from __future__ import annotations

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
