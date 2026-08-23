"""The one place untrusted-derived data becomes writable.

A taint system that never declassifies cannot write anything, and one
that declassifies implicitly provides no protection. So there is exactly
one narrow, audited gate, and it is a pure function of checks that already
happened:

    schema-valid  +  corroborated by N distinct customers
                  +  no unresolved conflict
                  +  no injection signature anywhere in its evidence
    ------------------------------------------------------------------
    -> re-stamped `validated`, and only then eligible for the writer.

Note what is *not* in that list: the model's own assurance. Nothing the
LLM says about its output can move data across this line.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from ..schemas import IssueCandidate, Observation, TRUST_VALIDATED
from .audit import audit
from .prompt_guard import injection_risk
from .taint import Taint


class DeclassificationRefused(RuntimeError):
    pass


def declassify_candidate(candidate: IssueCandidate,
                         evidence: Iterable[Observation]) -> IssueCandidate:
    reasons: list[str] = []

    if candidate.decision != "auto_accept":
        reasons.append(f"decision is {candidate.decision!r}, not auto_accept")
    if candidate.conflicts:
        reasons.append("unresolved conflicts present")

    tainted_evidence = [
        o.observation_id for o in evidence
        if injection_risk(o.evidence) > 0.0
    ]
    if tainted_evidence:
        reasons.append(f"injection signatures in evidence: {tainted_evidence[:5]}")

    if reasons:
        audit.write("declassification_refused", product_id=candidate.product_id,
                    issue_key=candidate.issue_key, reasons=reasons)
        raise DeclassificationRefused(
            f"{candidate.product_id}/{candidate.issue_key}: " + "; ".join(reasons))

    audit.write("declassified", product_id=candidate.product_id,
                issue_key=candidate.issue_key,
                customers=candidate.distinct_customers,
                confidence=candidate.mean_confidence)
    return replace(candidate, trust=TRUST_VALIDATED)


def taint_for(candidate: IssueCandidate) -> Taint:
    """Trusted only once the candidate carries the validated stamp."""
    return Taint.trusted() if candidate.trust == TRUST_VALIDATED else Taint.from_customer(
        f"{candidate.product_id}/{candidate.issue_key}")
