"""Token pricing, so a run can report what it cost.

Rates are per million tokens and go stale: they are a cached figure, not an
authority, and the model id is recorded alongside every estimate so a
number can be checked against the invoice rather than trusted.
"""

from __future__ import annotations

from dataclasses import dataclass

# USD per million tokens, first-party API rates.
RATES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
}


@dataclass
class Spend:
    model: str
    calls: int
    input_tokens: int
    output_tokens: int

    @property
    def known_rate(self) -> bool:
        return self.model in RATES

    @property
    def usd(self) -> float | None:
        if not self.known_rate:
            return None
        rate_in, rate_out = RATES[self.model]
        return (self.input_tokens * rate_in + self.output_tokens * rate_out) / 1e6

    def render(self) -> str:
        lines = [
            f"  model            {self.model}",
            f"  model calls      {self.calls}",
            f"  input tokens     {self.input_tokens:,}",
            f"  output tokens    {self.output_tokens:,}",
        ]
        if self.usd is None:
            lines.append("  estimated cost   unknown (no cached rate for this model)")
        else:
            lines += [
                f"  estimated cost   ${self.usd:.2f}",
                f"  per 1k calls     ${self.usd / max(self.calls, 1) * 1000:.2f}",
            ]
        return "\n".join(lines)


def estimate(model: str, calls: int, input_tokens: int, output_tokens: int) -> Spend:
    return Spend(model=model, calls=calls,
                 input_tokens=input_tokens, output_tokens=output_tokens)
