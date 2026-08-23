"""Stage 3 -- the funnel.

This is the single highest-leverage cost control in the system. A cheap
lexical/statistical scorer discards small talk so that only product-bearing
segments reach an LLM. On 10 TB/day it decides whether inference costs
five figures or seven.

In production this is a distilled classifier or an embedding-similarity
router; the interface -- score in [0,1] -- is identical, so the model can
be swapped without touching any other stage.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator

from ..catalog import load_catalog, resolve_product, resolve_version
from ..config import CONFIG
from ..schemas import Segment

PROBLEM_TERMS = {
    "crash", "crashes", "crashing", "reboot", "reboots", "disconnect", "disconnects",
    "disconnecting", "drop", "drops", "dropping", "fail", "fails", "failing", "error",
    "broken", "bug", "timeout", "times out", "hot", "overheat", "overheating", "slow",
    "loses", "lost", "stuck", "freeze", "frozen", "wrong", "missing",
}
REQUEST_TERMS = {"add", "support for", "could you", "feature", "wish", "would like", "request"}
SPEC_TERMS = {"spec", "spec sheet", "datasheet", "documentation", "manual", "says", "advertised"}

_SMALL_TALK = re.compile(
    r"(?i)^(hi|hello|thanks|thank you|sure|okay|ok|no problem|have a great|good morning)\b")


def identify_product(segment: Segment) -> tuple[str | None, float]:
    """Transcript first, then the case's structured product hint.

    Text evidence outranks metadata when both exist -- the caller may have
    been routed to the wrong queue -- but the hint keeps the ~40% of calls
    that never say a product name from being silently discarded.
    """
    product_id, confidence = resolve_product(segment.text)
    if product_id is not None:
        return product_id, confidence
    if segment.product_hint:
        return segment.product_hint, 0.75
    return None, 0.0


def score_segment(segment: Segment) -> float:
    customer_text = "\n".join(
        line for line in segment.text.splitlines() if line.startswith("customer:")
    ) or segment.text
    lowered = customer_text.lower()

    product_id, product_conf = identify_product(segment)
    if product_id is None:
        # No resolvable product: at most weak signal, never worth an LLM call.
        return 0.0

    score = 0.45 * product_conf
    if any(term in lowered for term in PROBLEM_TERMS):
        score += 0.35
    if any(term in lowered for term in REQUEST_TERMS):
        score += 0.20
    if any(term in lowered for term in SPEC_TERMS):
        score += 0.15
    if resolve_version(customer_text, product_id):
        score += 0.15
    if _SMALL_TALK.match(lowered.strip()):
        score -= 0.20

    return max(0.0, min(1.0, score))


def route(segments: Iterable[Segment]) -> Iterator[Segment]:
    """Annotate every segment; yield only those worth model inference."""
    for segment in segments:
        product_id, confidence = identify_product(segment)
        segment.product_id = product_id
        segment.product_confidence = confidence
        segment.relevance = score_segment(segment)
        if segment.relevance >= CONFIG.relevance_threshold:
            yield segment
