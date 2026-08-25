"""Regression gate for the relevance router.

The router prices every stage after it, so a change that quietly drops
recall is the most expensive kind of regression this repo can have: the
segments it discards never reach extraction and cannot be recovered later.
These floors are set just under measured behaviour, so a real degradation
fails rather than merely showing up in a report nobody runs.
"""

from __future__ import annotations

import pytest

from cip.config import CONFIG
from cip.eval.dataset import load_hard_cases
from cip.eval.router_eval import best_threshold, evaluate, sweep

THRESHOLD = CONFIG.relevance_threshold


@pytest.fixture(scope="module")
def hard():
    return load_hard_cases()


def test_hard_cases_are_balanced_enough_to_mean_something(hard):
    positives = sum(c.label for c in hard)
    assert len(hard) >= 30
    assert 0.25 < positives / len(hard) < 0.75, "degenerate label balance"


def test_router_recall_floor_on_hard_cases(hard):
    metrics = evaluate(hard, THRESHOLD)
    assert metrics.recall >= 0.90, (
        f"recall {metrics.recall:.3f} below floor; missed: "
        f"{[c.category for c in metrics.false_negatives]}")


def test_router_precision_floor_on_hard_cases(hard):
    # Lower than recall by design: a false positive costs one inference call,
    # a false negative loses a customer report permanently.
    metrics = evaluate(hard, THRESHOLD)
    assert metrics.precision >= 0.70, f"precision {metrics.precision:.3f} below floor"


def test_funnel_actually_discards_most_traffic(hard):
    """The cost claim in the README is only true if this holds."""
    assert evaluate(hard, THRESHOLD).kept_fraction < 0.60


def test_injection_payloads_never_dominate_the_signal(hard):
    """Injection text may pass the router -- extraction is toolless, so that
    is survivable -- but it must not be scored above genuine product signal."""
    injections = [c for c in hard if c.category == "negative_injection"]
    positives = [c for c in hard if c.label == 1]
    assert injections and positives

    from cip.pipeline.route import score_segment
    worst_injection = max(score_segment(c.segment) for c in injections)
    best_signal = max(score_segment(c.segment) for c in positives)
    assert worst_injection <= best_signal


def test_threshold_sits_in_a_defensible_place(hard):
    """Guards against a threshold nudged until the numbers looked good."""
    metrics = {m.threshold: m for m in sweep(hard)}
    assert metrics[THRESHOLD].f1 >= max(
        m.f1 for t, m in metrics.items() if t > THRESHOLD), (
        "a higher threshold scores better on every axis -- retune or justify")


def test_recall_ceiling_is_reported_not_hidden(hard):
    """If no threshold can reach the recall floor, that must surface as None
    rather than silently returning the least-bad option."""
    impossible = best_threshold(hard, min_recall=1.01)
    assert impossible is None


def test_generic_aliases_resolve_only_when_unambiguous():
    """A category noun is evidence exactly while one product owns it."""
    from cip.catalog import GENERIC_CONFIDENCE, _generic_index, resolve_product

    index = _generic_index()
    assert index["router"] == "X100"
    assert len(set(index.values())) == len({v for v in index.values()})
    pid, conf = resolve_product("the router reboots on its own")
    assert (pid, conf) == ("X100", GENERIC_CONFIDENCE)


def test_generic_alias_alone_cannot_admit_a_segment():
    """0.45 * 0.60 = 0.27, below the threshold by construction.

    'my home router from another vendor' names a router and reports nothing
    about ours; it must take actual problem language to get through.
    """
    from cip.pipeline.route import score_segment
    from cip.schemas import Segment

    def seg(text):
        return Segment(segment_id="S", call_id="C", customer_id="U",
                       timestamp="2026-08-22T10:00:00+00:00", region="US",
                       text=f"customer: {text}", speaker_mix={"customer": 1})

    assert score_segment(seg("my home router from another vendor, not your kit")) < THRESHOLD
    assert score_segment(seg("the router reboots on its own every night")) >= THRESHOLD


def test_unambiguous_version_identifies_the_product():
    """Callers routinely give a version and no product name at all."""
    from cip.catalog import VERSION_CONFIDENCE, resolve_product

    pid, conf = resolve_product("after installing firmware 7.2 the VPN keeps dropping")
    assert (pid, conf) == ("X100", VERSION_CONFIDENCE)
    # A number that is not a catalog version must not resolve anything.
    assert resolve_product("that cost about 7.99 dollars")[0] is None


def test_customer_states_requires_a_customer_line():
    """A claim on an agent line is not the customer having made it.

    The generator injects diarization errors that move a claim onto an
    `agent:` line. Labelling those positive charges the router with a miss
    for correctly dropping them, which is what understated recall before.
    """
    from cip.eval.dataset import _customer_states

    signal = "The branch gateway loses its static routes on power cycle."
    customer = f"agent: One moment.\ncustomer: {signal}"
    flipped = f"customer: One moment.\nagent: {signal}"

    assert _customer_states(customer, signal) is True
    assert _customer_states(flipped, signal) is False
    assert _customer_states(customer, None) is False
    # Said by both: the customer still said it.
    assert _customer_states(f"{flipped}\ncustomer: {signal}", signal) is True
