"""Canonical data contracts.

Every stage of the pipeline speaks one of these. Extraction is
schema-constrained on purpose: the LLM emits `Observation` records and
nothing else, so free-form model output can never become a tool call.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


ISSUE_TYPES = (
    "bug_report",
    "feature_request",
    "spec_correction",
    "praise",
    "usage_question",
)

SEVERITIES = ("low", "medium", "high", "critical")

TRUST_UNTRUSTED = "untrusted"   # customer speech, anything off the wire
TRUST_DERIVED = "derived"       # produced by an LLM from untrusted input
TRUST_VALIDATED = "validated"   # passed the policy boundary


@dataclass
class CallRecord:
    """One raw call as it lands in the data lake."""

    call_id: str
    customer_id: str
    timestamp: str
    region: str
    centre: str = ""
    channel: str = ""
    # SKU the support case was opened against. Structured, trusted metadata --
    # customers routinely never say the product name out loud.
    product_hint: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)

    def transcript_text(self, speaker: str | None = None) -> str:
        return "\n".join(
            f"{t['speaker']}: {t['text']}"
            for t in self.turns
            if speaker is None or t["speaker"] == speaker
        )


@dataclass
class Segment:
    """A topic-coherent slice of a call. The unit the LLM actually sees."""

    segment_id: str
    call_id: str
    customer_id: str
    timestamp: str
    region: str
    text: str
    speaker_mix: dict[str, int]
    # Diarization provenance. `attribution_confidence` is the MINIMUM speaker
    # confidence across the segment's customer turns -- deliberately the
    # conservative reading: if any customer turn is poorly diarized, the whole
    # segment's attribution is suspect, because a flat text rendering cannot
    # say which turn a matched phrase came from.
    customer_turns: int = 0
    attribution_confidence: float = 1.0
    product_hint: str | None = None
    trust: str = TRUST_UNTRUSTED
    product_id: str | None = None
    product_confidence: float = 0.0
    relevance: float = 0.0
    pii_redactions: int = 0


@dataclass
class Observation:
    """A single structured claim extracted from one segment.

    This is a *customer observation*, not product truth. It only becomes
    truth after aggregation, reconciliation and policy approval.
    """

    observation_id: str
    segment_id: str
    call_id: str
    customer_id: str
    product_id: str
    product_version: str | None
    type: str
    issue_key: str
    summary: str
    severity: str
    evidence: str
    confidence: float
    region: str
    timestamp: str
    trust: str = TRUST_DERIVED
    extractor: str = "unknown"
    # Who actually said it. Only ever "customer": an agent restating a known
    # defect is not a customer report, and counting it as one inflates the
    # very issues agents talk about most.
    speaker: str = "customer"
    attribution_confidence: float = 1.0
    # Tokens this observation's model call consumed. Zero for the rules
    # extractor, which makes no call. Carried on the row because executors
    # write to ephemeral local logs -- a counter there never reaches the
    # driver, and the row is already travelling to a table that survives.
    input_tokens: int = 0
    output_tokens: int = 0

    def validate(self) -> list[str]:
        """Schema validation. Anything that fails here never reaches the KB."""
        errors: list[str] = []
        if self.type not in ISSUE_TYPES:
            errors.append(f"type {self.type!r} not in {ISSUE_TYPES}")
        if self.severity not in SEVERITIES:
            errors.append(f"severity {self.severity!r} not in {SEVERITIES}")
        if not 0.0 <= self.confidence <= 1.0:
            errors.append(f"confidence {self.confidence} out of range")
        if not re.fullmatch(r"[A-Z0-9_\-]{2,40}", self.product_id or ""):
            errors.append(f"product_id {self.product_id!r} malformed")
        if not re.fullmatch(r"[A-Z0-9_]{3,60}", self.issue_key or ""):
            errors.append(f"issue_key {self.issue_key!r} malformed")
        if len(self.evidence) > 400:
            errors.append("evidence too long (possible prompt smuggling)")
        if self.speaker != "customer":
            errors.append(f"speaker {self.speaker!r}: only customer claims are observations")
        if not 0.0 <= self.attribution_confidence <= 1.0:
            errors.append(f"attribution_confidence {self.attribution_confidence} out of range")
        return errors


@dataclass
class IssueCandidate:
    """Aggregate of many observations about the same (product, issue)."""

    product_id: str
    issue_key: str
    type: str
    summary: str
    severity: str
    mentions: int
    distinct_customers: int
    regions: list[str]
    versions: list[str]
    first_seen: str
    last_seen: str
    mean_confidence: float
    evidence_ids: list[str]
    conflicts: list[str] = field(default_factory=list)
    decision: str = "pending"      # auto_accept | review | reject
    decision_reason: str = ""
    trust: str = TRUST_DERIVED


def to_json(obj: Any) -> str:
    return json.dumps(asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj, default=str)
