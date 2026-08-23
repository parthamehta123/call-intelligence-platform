"""Deterministic policy engine.

The load-bearing principle of the whole design:

    the model may PROPOSE an action; deterministic software decides
    whether that action EXECUTES.

Nothing below consults an LLM. Every decision is a pure function of the
tool identity, the caller's role, the argument values, and the taint
carried by those arguments -- so it is testable, reproducible and
auditable, which an LLM-based guard is not.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import CONFIG
from .audit import audit
from .dlp import scan
from .egress import check_destination
from .prompt_guard import injection_risk, scan_for_injection
from .taint import Taint, TaintedValue

ALLOW, DENY, REVIEW = "allow", "deny", "review"


@dataclass(frozen=True)
class Capability:
    """What a tool is permitted to do, declared out of band from the model."""

    name: str
    effect: str                       # read | write | network | execute
    risk: str                         # low | medium | high | critical
    roles: frozenset[str]
    accepts_untrusted_args: bool = False
    requires_human_review: bool = False
    arg_validators: dict[str, Callable[[Any], bool]] = field(default_factory=dict)


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    role: str
    taint: Taint = field(default_factory=Taint.trusted)
    purpose: str = ""

    @staticmethod
    def from_tainted(tool: str, role: str, tainted_args: dict[str, TaintedValue],
                     purpose: str = "") -> "ToolCall":
        taint = Taint.trusted()
        args: dict[str, Any] = {}
        for key, value in tainted_args.items():
            if isinstance(value, TaintedValue):
                taint = taint.merge(value.taint)
                args[key] = value.value
            else:
                args[key] = value
        return ToolCall(tool=tool, args=args, role=role, taint=taint, purpose=purpose)


@dataclass
class PolicyDecision:
    action: str
    tool: str
    role: str
    risk_score: float
    reasons: list[str]

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    def explain(self) -> str:
        return f"{self.action.upper()} {self.tool} (risk={self.risk_score:.2f}): " + "; ".join(self.reasons)


class PolicyViolation(RuntimeError):
    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.explain())
        self.decision = decision


RISK_WEIGHT = {"low": 0.1, "medium": 0.3, "high": 0.6, "critical": 0.9}

# Only SELECT reaches the database, and only single statements.
_SQL_ALLOWED = re.compile(r"(?is)^\s*select\b")
_SQL_FORBIDDEN = re.compile(
    r"(?is)\b(insert|update|delete|drop|alter|truncate|create|attach|pragma|vacuum)\b")


class PolicyEngine:
    """Registry + evaluator. Default deny: unregistered tools never run."""

    def __init__(self, config=CONFIG) -> None:
        self.config = config
        self.capabilities: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        self.capabilities[cap.name] = cap

    # -- layered evaluation ------------------------------------------------
    def evaluate(self, call: ToolCall) -> PolicyDecision:
        reasons: list[str] = []
        risk = 0.0

        cap = self.capabilities.get(call.tool)
        if cap is None:
            return self._decide(call, DENY, 1.0, ["tool not registered (default deny)"])

        risk += RISK_WEIGHT[cap.risk]

        # Layer 1 -- RBAC. Necessary, never sufficient.
        if call.role not in cap.roles:
            return self._decide(call, DENY, 1.0,
                                [f"role {call.role!r} not granted {cap.name!r} (RBAC)"])
        reasons.append(f"RBAC ok for role {call.role!r}")

        # Layer 2 -- taint. The question RBAC structurally cannot ask.
        if call.taint.untrusted:
            risk += 0.3
            if not cap.accepts_untrusted_args:
                return self._decide(
                    call, DENY, 1.0,
                    [f"{cap.effect} tool refuses untrusted-derived arguments "
                     f"({call.taint.describe()})"])
            reasons.append(f"untrusted input accepted by contract ({call.taint.describe()})")

        # Layer 3 -- argument schema. Narrow tools beat free-form strings.
        for key, validator in cap.arg_validators.items():
            if key not in call.args:
                return self._decide(call, DENY, 1.0, [f"missing required argument {key!r}"])
            if not validator(call.args[key]):
                return self._decide(call, DENY, 1.0, [f"argument {key!r} failed validation"])
        if cap.arg_validators:
            reasons.append("argument schema ok")

        blob = " ".join(str(v) for v in call.args.values())

        # Layer 4 -- injection signal on the argument payload itself.
        inj = injection_risk(blob)
        if inj > 0:
            risk += inj * 0.5
            sigs = [f.signature for f in scan_for_injection(blob)]
            if inj >= 0.6 or cap.effect in ("write", "execute", "network"):
                return self._decide(call, DENY, 1.0,
                                    [f"injection signatures in arguments: {sigs}"])
            reasons.append(f"low-grade injection signal noted: {sigs}")

        # Layer 5 -- SQL containment for query tools.
        if "sql" in call.args:
            sql = str(call.args["sql"])
            if not _SQL_ALLOWED.match(sql) or _SQL_FORBIDDEN.search(sql) or ";" in sql.strip()[:-1]:
                return self._decide(call, DENY, 1.0, ["only single read-only SELECT permitted"])
            reasons.append("SQL is read-only SELECT")

        # Layer 6 -- DLP + egress on anything leaving the boundary.
        if cap.effect == "network":
            destination = str(call.args.get("url", ""))
            decision = check_destination(destination, blob, self.config.egress_allowlist)
            if not decision.allowed:
                return self._decide(call, DENY, 1.0, [f"egress blocked: {decision.reason}"])
            reasons.append(f"egress allowed: {decision.reason}")
        else:
            secrets = sorted({f.kind for f in scan(blob) if f.is_secret})
            if secrets:
                return self._decide(call, DENY, 1.0, [f"secrets present in arguments: {secrets}"])

        # Layer 7 -- human in the loop for irreversible effects.
        if cap.requires_human_review and self.config.require_human_review:
            return self._decide(call, REVIEW, min(1.0, risk),
                                reasons + ["irreversible effect requires human approval"])

        return self._decide(call, ALLOW, min(1.0, risk), reasons)

    def _decide(self, call: ToolCall, action: str, risk: float, reasons: list[str]) -> PolicyDecision:
        decision = PolicyDecision(action, call.tool, call.role, risk, reasons)
        audit.write(
            "policy_decision",
            tool=call.tool,
            role=call.role,
            action=action,
            risk=round(risk, 3),
            taint=call.taint.describe(),
            purpose=call.purpose,
            reasons=reasons,
            args_preview={k: str(v)[:120] for k, v in call.args.items()},
        )
        return decision

    def enforce(self, call: ToolCall) -> PolicyDecision:
        decision = self.evaluate(call)
        if not decision.allowed:
            raise PolicyViolation(decision)
        return decision


ENGINE = PolicyEngine()


def guarded_tool(capability: Capability, engine: PolicyEngine = ENGINE):
    """Decorator: no registered tool can be invoked without passing the gate.

    The wrapped function is the *executor*. It never sees an argument the
    engine has not already cleared, which is what keeps a compromised
    reasoning loop from becoming a compromised system.
    """

    engine.register(capability)

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*, role: str, taint: Taint | None = None, purpose: str = "", **kwargs):
            call = ToolCall(capability.name, kwargs, role, taint or Taint.trusted(), purpose)
            engine.enforce(call)
            result = fn(**kwargs)
            audit.write("tool_executed", tool=capability.name, role=role,
                        result_preview=str(result)[:200])
            return result

        wrapper.capability = capability  # type: ignore[attr-defined]
        return wrapper

    return decorator
