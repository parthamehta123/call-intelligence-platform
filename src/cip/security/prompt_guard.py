"""Prompt-injection detection.

Deliberately positioned as a *signal*, not a gate. Attackers obfuscate,
translate and encode, so any regex list is bypassable -- this raises the
risk score of a proposed action, while taint tracking, capability
narrowing and egress control do the actual containment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SIGNATURES: list[tuple[str, str, re.Pattern[str]]] = [
    ("instruction_override", "high",
     re.compile(r"(?i)\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|earlier|above|all)\b.{0,20}\b(instruction|prompt|rule|direction)")),
    ("role_hijack", "high",
     re.compile(r"(?i)\b(you are now|act as|new (?:directive|persona|role)|maintenance mode|developer mode)\b")),
    ("transcript_escape", "critical",
     re.compile(r"(?i)</?(transcript|system|assistant|user|instructions)>|\b(assistant|system)\s*:\s*i will\b")),
    ("destructive_command", "critical",
     re.compile(r"(?i)\b(rm\s+-rf|drop\s+(?:table|database)|truncate\s+table|delete\s+from|shutdown|mkfs)\b")),
    ("exfiltration", "critical",
     re.compile(r"(?i)\b(curl|wget|upload|post|send|exfil\w*)\b.{0,40}(https?://|/etc/|~/\.ssh|credentials|secrets)")),
    ("tool_invocation", "high",
     re.compile(r"(?i)\b(delete_product|drop_table|execute_sql|run_shell|shell)\s*\(")),
    ("authority_claim", "medium",
     re.compile(r"(?i)\b(admin team|security team|anthropic|system administrator)\b.{0,30}\b(says|directive|authorized|approved|instructs)")),
]


@dataclass(frozen=True)
class InjectionFinding:
    signature: str
    severity: str
    excerpt: str


SEVERITY_WEIGHT = {"medium": 0.3, "high": 0.6, "critical": 1.0}


def scan_for_injection(text: str) -> list[InjectionFinding]:
    findings: list[InjectionFinding] = []
    for name, severity, pattern in SIGNATURES:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 20)
            findings.append(InjectionFinding(
                signature=name,
                severity=severity,
                excerpt=text[start:match.end() + 20].strip().replace("\n", " "),
            ))
    return findings


def injection_risk(text: str) -> float:
    """0.0 clean .. 1.0 near-certain injection attempt."""
    findings = scan_for_injection(text)
    if not findings:
        return 0.0
    return min(1.0, sum(SEVERITY_WEIGHT[f.severity] for f in findings))
