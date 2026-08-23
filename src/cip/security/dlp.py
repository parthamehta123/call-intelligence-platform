"""Data-loss prevention on tool inputs and outputs.

Two jobs. Inbound: strip customer PII before transcripts are persisted or
sent to a model. Outbound: refuse to let credentials leave the boundary,
even when the caller is an authorized identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ordered most-specific first so overlapping matches resolve sensibly.
PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("aws_access_key", "critical", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key", "critical", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("anthropic_key", "critical", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{16,}\b")),
    ("bearer_token", "high", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9\-_\.=]{20,}\b")),
    ("password_assignment", "high", re.compile(r"(?i)\b(?:password|passwd|secret)\s*[:=]\s*\S{6,}")),
    ("credit_card", "high", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("ssn", "high", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("email", "medium", re.compile(r"\b[\w\.\-\+]+@[\w\-]+\.[A-Za-z]{2,}\b")),
    ("phone", "medium", re.compile(r"\b(?:\+?1[ -])?\(?\d{3}\)?[ -]\d{3}[ -]\d{4}\b")),
]

SECRET_KINDS = {"aws_access_key", "private_key", "anthropic_key", "bearer_token",
                "password_assignment"}


@dataclass(frozen=True)
class DLPFinding:
    kind: str
    severity: str
    span: tuple[int, int]
    sample: str

    @property
    def is_secret(self) -> bool:
        return self.kind in SECRET_KINDS


def _luhn(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13:
        return False
    checksum, parity = 0, len(nums) % 2
    for idx, num in enumerate(nums):
        if idx % 2 == parity:
            num *= 2
            if num > 9:
                num -= 9
        checksum += num
    return checksum % 10 == 0


def scan(text: str) -> list[DLPFinding]:
    findings: list[DLPFinding] = []
    for kind, severity, pattern in PATTERNS:
        for match in pattern.finditer(text):
            # Card numbers are the one pattern noisy enough to need a checksum;
            # without it every order number and phone extension trips DLP.
            if kind == "credit_card" and not _luhn(match.group()):
                continue
            sample = match.group()
            findings.append(DLPFinding(
                kind=kind,
                severity=severity,
                span=match.span(),
                sample=sample[:4] + "***" if len(sample) > 4 else "***",
            ))
    return findings


def redact(text: str) -> tuple[str, list[DLPFinding]]:
    """Replace every hit with a typed placeholder, right to left."""
    findings = scan(text)
    out = text
    for finding in sorted(findings, key=lambda f: f.span[0], reverse=True):
        start, end = finding.span
        out = f"{out[:start]}[REDACTED:{finding.kind.upper()}]{out[end:]}"
    return out, findings


def contains_secret(text: str) -> bool:
    return any(f.is_secret for f in scan(text))
