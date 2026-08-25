"""Metrics for the relevance router.

Reports precision, recall and F1, but the number that governs the design
is **recall**: a segment the router drops never reaches extraction, is
never aggregated, and never becomes evidence. There is no downstream stage
that recovers it. Precision governs cost -- every false positive is one
more paid inference call.

So the threshold sweep is framed as the trade it actually is:

    recall  -> how much product knowledge survives
    kept%   -> what fraction of 10 TB/day you pay a model to read

Injections are scored on a third channel rather than as negatives. The
router forwarding one is correct -- a segment it drops never reaches taint
tracking and is never recorded as an attack -- so charging it against
precision would tune the funnel directly against the security design. See
`attack_recall`, which is the number that falls silently when the funnel
is tuned for cost alone.
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
    # The attack channel, scored separately -- see `attack_recall`. Counts
    # every segment carrying a payload, including those that also carry a
    # real claim.
    attack_kept: int = 0
    attack_dropped: int = 0
    attacks_dropped: list[EvalCase] = field(default_factory=list)
    # Attack-*only* segments. Kept apart from the counters above because
    # those overlap the signal classes, and mixing them into precision or
    # kept% would count the same segment twice.
    attack_only_kept: int = 0
    attack_only_dropped: int = 0

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
    def attack_recall(self) -> float:
        """Share of injection payloads the router forwards.

        Wanted high, for the opposite reason to signal recall: a dropped
        injection is not a saved inference call, it is an attack that never
        reached taint tracking and was never recorded. This is the number
        that goes down silently when the funnel is tuned for cost.
        """
        total = self.attack_kept + self.attack_dropped
        return self.attack_kept / total if total else 1.0

    @property
    def precision_charging_attacks(self) -> float:
        """Precision if forwarded attacks were counted as false alarms.

        Reported alongside `precision` so the gain from making attacks a
        third class stays visible rather than being absorbed silently.
        """
        fp = self.fp + self.attack_only_kept
        return self.tp / (self.tp + fp) if (self.tp + fp) else 1.0

    @property
    def kept_fraction(self) -> float:
        """Share of all segments sent to the model -- the cost driver."""
        total = (self.tp + self.fp + self.tn + self.fn
                 + self.attack_only_kept + self.attack_only_dropped)
        return ((self.tp + self.fp + self.attack_only_kept) / total
                if total else 0.0)

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

        # The attack channel is scored on every segment carrying a payload,
        # including the 19 that also carry a real claim -- the security
        # stage must see those too, so they count here as well as below.
        if case.carries_attack:
            if predicted:
                metrics.attack_kept += 1
            else:
                metrics.attack_dropped += 1
                metrics.attacks_dropped.append(case)

        # Attack-only segments are not scored for product signal: keeping
        # one is correct routing, not a false alarm.
        if case.expected == "attack":
            if predicted:
                metrics.attack_only_kept += 1
            else:
                metrics.attack_only_dropped += 1
            continue

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
        # For the attack class the error is the opposite one: an injection
        # the router *drops* is the failure, not one it forwards.
        wrong = (not predicted) if case.expected == "attack" else (
            predicted != bool(case.label))
        if wrong:
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
    ]
    if current.attack_kept or current.attack_dropped:
        total_attacks = current.attack_kept + current.attack_dropped
        lines += [
            "",
            "attack channel (injection payloads -- forwarding is correct):",
            f"  routed to security {current.attack_kept}/{total_attacks}"
            f"   ({current.attack_recall:.1%})"
            f"   dropped silently {current.attack_dropped}",
            f"  precision if these were charged as false alarms: "
            f"{current.precision_charging_attacks:.3f}",
        ]
    lines += [
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

    if current.attacks_dropped:
        lines += ["", "injections dropped before the security stage:"]
        for case in current.attacks_dropped[:6]:
            text = case.segment.text.replace("\n", " ")[:88]
            lines.append(f"  [{case.category}] {text}")

    if current.false_negatives:
        lines += ["", "missed signal (unrecoverable downstream):"]
        for case in current.false_negatives[:6]:
            text = case.segment.text.replace("\n", " ")[:88]
            lines.append(f"  [{case.category}] {text}")
    return "\n".join(lines)
