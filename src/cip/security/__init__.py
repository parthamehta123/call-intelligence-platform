"""Agent security boundary.

RBAC answers "is this identity allowed to call this tool?". It cannot answer
"did untrusted customer speech cause this call, and is the payload safe?".
This package supplies the second answer: taint tracking, DLP, prompt-injection
detection, egress control and a deterministic policy engine that sits between
the model's *proposal* and any privileged *execution*.
"""

from .audit import AuditLog, audit
from .dlp import DLPFinding, scan, redact
from .egress import EgressDecision, check_destination
from .policy import PolicyDecision, PolicyEngine, ToolCall, guarded_tool
from .prompt_guard import InjectionFinding, scan_for_injection
from .taint import Taint, TaintedValue

__all__ = [
    "AuditLog", "audit", "DLPFinding", "scan", "redact",
    "EgressDecision", "check_destination", "PolicyDecision", "PolicyEngine",
    "ToolCall", "guarded_tool", "InjectionFinding", "scan_for_injection",
    "Taint", "TaintedValue",
]
