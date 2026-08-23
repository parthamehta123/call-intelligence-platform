"""Metrics for the relevance router.

Reports precision, recall and F1, but the number that governs the design
is **recall**: a segment the router drops never reaches extraction, is
never aggregated, and never becomes evidence. There is no downstream stage
that recovers it. Precision governs cost -- every false positive is one
more paid inference call.

So the threshold sweep is framed as the trade it actually is:

    recall  -> how much product knowledge survives
    kept%   -> what fraction of 10 TB/day you pay a model to read
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..pipeline.route import score_segment
from .dataset import EvalCase


@dataclass
class Metrics:
    threshold: float
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    false_negatives: list[EvalCase] = field(default_factory=list)
    false_positives: list[EvalCase] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def kept_fraction(self) -> float:
        """Share of all segments sent to the model -- the cost driver."""
        total = self.tp + self.fp + self.tn + self.fn
        return (self.tp + self.fp) / total if total else 0.0

    def as_row(self) -> str:
        # Recall to 4 dp on purpose: 0.9499 and 0.9500 look identical at 3 dp
        # and sit on opposite sides of a 0.95 acceptance floor.
        return (f"{self.threshold:>5.2f}  {self.precision:>9.3f}  {self.recall:>8.4f}  "
                f"{self.f1:>6.3f}  {self.kept_fraction:>7.1%}  "
                f"{self.fn:>4}  {self.fp:>4}")


def evaluate(cases: Sequence[EvalCase], threshold: float,
             include_ambiguous: bool = True) -> Metrics:
    metrics = Metrics(threshold=threshold)
    for case in cases:
        if case.ambiguous and not include_ambiguous:
            continue
        predicted = score_segment(case.segment) >= threshold
        if predicted and case.label:
            metrics.tp += 1
        elif predicted and not case.label:
            metrics.fp += 1
            metrics.false_positives.append(case)
        elif not predicted and case.label:
            metrics.fn += 1
            metrics.false_negatives.append(case)
        else:
            metrics.tn += 1
    return metrics


def sweep(cases: Sequence[EvalCase],
          thresholds: Iterable[float] | None = None) -> list[Metrics]:
    thresholds = thresholds if thresholds is not None else [
        round(t / 20, 2) for t in range(0, 21)]
    return [evaluate(cases, t) for t in thresholds]


def best_threshold(cases: Sequence[EvalCase], min_recall: float = 0.95) -> Metrics | None:
    """Cheapest threshold that still clears a recall floor.

    Optimising F1 is the wrong objective here -- it trades away recall for
    precision at equal weight, and the two are not equally costly. Fix the
    recall you are willing to live with, then take the cheapest threshold
    that meets it.
    """
    viable = [m for m in sweep(cases) if m.recall >= min_recall]
    return min(viable, key=lambda m: m.kept_fraction) if viable else None


def by_category(cases: Sequence[EvalCase], threshold: float) -> dict[str, tuple[int, int]]:
    """(errors, total) per category -- shows *what kind* of thing it misses."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for case in cases:
        predicted = score_segment(case.segment) >= threshold
        counts[case.category][1] += 1
        if predicted != bool(case.label):
            counts[case.category][0] += 1
    return {k: (v[0], v[1]) for k, v in sorted(counts.items())}


def report(cases: Sequence[EvalCase], title: str, threshold: float) -> str:
    lines = [f"=== {title} ({len(cases)} cases) ===", ""]
    current = evaluate(cases, threshold)
    lines += [
        f"at the configured threshold {threshold}:",
        f"  precision {current.precision:.3f}   recall {current.recall:.4f}   "
        f"F1 {current.f1:.3f}",
        f"  kept {current.kept_fraction:.1%} of segments   "
        f"missed {current.fn}   false alarms {current.fp}",
        "",
        "threshold sweep:",
        "thresh  precision    recall      F1     kept  miss  f.pos",
    ]
    lines += ["  " + m.as_row() for m in sweep(cases)]

    floor = best_threshold(cases, min_recall=0.95)
    if floor is None:
        note = ("no threshold reaches recall >= 0.95 -- the ceiling is the "
                "scorer's features, not the threshold")
    elif floor.threshold == 0.0:
        note = ("recall >= 0.95 is reachable only at threshold 0.0, i.e. by "
                "disabling the funnel entirely -- the threshold is not the "
                "binding constraint here")
    else:
        note = (f"cheapest threshold holding recall >= 0.95: {floor.threshold} "
                f"(keeps {floor.kept_fraction:.1%}, misses {floor.fn})")
    lines += ["", note, ""]

    errors = {k: v for k, v in by_category(cases, threshold).items() if v[0]}
    if errors:
        lines.append("errors by category:")
        lines += [f"  {k:<28} {e}/{t}" for k, (e, t) in errors.items()]
    else:
        lines.append("no errors by category")

    if current.false_negatives:
        lines += ["", "missed signal (unrecoverable downstream):"]
        for case in current.false_negatives[:6]:
            text = case.segment.text.replace("\n", " ")[:88]
            lines.append(f"  [{case.category}] {text}")
    return "\n".join(lines)
