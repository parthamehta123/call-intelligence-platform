"""Taint tracking.

The core rule of this system: data that entered from a customer is marked
untrusted at the boundary, and every value derived from it inherits that
mark. When a tool call is proposed later, the policy engine can ask a
question RBAC cannot -- "was this influenced by untrusted input?" -- and
answer it from provenance rather than from pattern-matching the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Taint:
    """Provenance label attached to a value."""

    untrusted: bool
    sources: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def trusted() -> "Taint":
        return Taint(untrusted=False)

    @staticmethod
    def from_customer(source_id: str) -> "Taint":
        return Taint(untrusted=True, sources=frozenset({source_id}))

    def merge(self, other: "Taint") -> "Taint":
        # Taint is monotone: mixing trusted and untrusted yields untrusted.
        return Taint(
            untrusted=self.untrusted or other.untrusted,
            sources=self.sources | other.sources,
        )

    def describe(self) -> str:
        if not self.untrusted:
            return "trusted"
        preview = ", ".join(sorted(self.sources)[:3])
        extra = "" if len(self.sources) <= 3 else f" (+{len(self.sources) - 3} more)"
        return f"untrusted[{preview}{extra}]"


@dataclass
class TaintedValue:
    """A value carrying its provenance. Unwrapping is explicit and audited."""

    value: Any
    taint: Taint

    @staticmethod
    def customer(value: Any, source_id: str) -> "TaintedValue":
        return TaintedValue(value, Taint.from_customer(source_id))

    @staticmethod
    def trusted(value: Any) -> "TaintedValue":
        return TaintedValue(value, Taint.trusted())

    def derive(self, new_value: Any) -> "TaintedValue":
        """Anything computed from this value keeps the same taint."""
        return TaintedValue(new_value, self.taint)

    def combine(self, others: Iterable["TaintedValue"], new_value: Any) -> "TaintedValue":
        taint = self.taint
        for other in others:
            taint = taint.merge(other.taint)
        return TaintedValue(new_value, taint)

    @property
    def untrusted(self) -> bool:
        return self.taint.untrusted

    def __repr__(self) -> str:  # keeps audit logs readable
        preview = str(self.value)
        if len(preview) > 60:
            preview = preview[:57] + "..."
        return f"TaintedValue({preview!r}, {self.taint.describe()})"
