"""Stage 6 -- reconciliation, the gate before anything becomes truth.

Two separate ideas are kept apart here, and conflating them is the most
common way these systems poison their own knowledge base:

    OBSERVED CUSTOMER KNOWLEDGE   "customers report the AP overheats"
    OFFICIAL PRODUCT TRUTH        "engineering confirmed BUG-938"

Nothing in this file promotes the first into the second on its own. It
decides only whether a candidate is corroborated enough to auto-accept as
an *observation of record*, needs a human, or should be dropped.
"""

from __future__ import annotations

from typing import Iterable

from ..config import CONFIG
from ..schemas import IssueCandidate


def decide(candidate: IssueCandidate, config=CONFIG) -> IssueCandidate:
    reasons: list[str] = []

    if candidate.conflicts:
        candidate.decision = "review"
        candidate.decision_reason = "; ".join(candidate.conflicts)
        return candidate

    if candidate.mean_confidence < 0.45:
        candidate.decision = "reject"
        candidate.decision_reason = f"mean confidence {candidate.mean_confidence} below floor"
        return candidate

    if candidate.distinct_customers < config.auto_accept_customers:
        reasons.append(f"only {candidate.distinct_customers} distinct customers "
                       f"(need {config.auto_accept_customers})")
    if candidate.mentions < config.auto_accept_mentions:
        reasons.append(f"only {candidate.mentions} mentions "
                       f"(need {config.auto_accept_mentions})")
    if candidate.mean_confidence < config.auto_accept_confidence:
        reasons.append(f"mean confidence {candidate.mean_confidence} below "
                       f"{config.auto_accept_confidence}")
    # A spec correction contradicts published documentation, so it always
    # gets a human regardless of how many customers agree.
    if candidate.type == "spec_correction":
        reasons.append("spec corrections always require human sign-off")

    if reasons:
        candidate.decision = "review"
        candidate.decision_reason = "; ".join(reasons)
    else:
        candidate.decision = "auto_accept"
        candidate.decision_reason = (
            f"{candidate.distinct_customers} distinct customers across "
            f"{len(candidate.regions)} regions, mean confidence "
            f"{candidate.mean_confidence}"
        )
    return candidate


def reconcile(candidates: Iterable[IssueCandidate], config=CONFIG) -> list[IssueCandidate]:
    return [decide(c, config) for c in candidates]
