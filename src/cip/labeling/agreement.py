"""Do two annotators mean the same thing?

Percent agreement flatters a skewed set: if 90% of items are negative, two
annotators who both guess "no" agree 90% of the time while sharing no
judgement at all. Cohen's kappa corrects for the agreement chance alone
would produce, which is why it is the number reported.

Kappa is also a property of the *guidelines*, not the people. A low value
means the definition is ambiguous and needs rewriting before more labels
are collected -- collecting more against an unclear definition just buys
more noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import Label, LabelStore


# A label carrying this marker was not produced independently -- the
# annotator was told the answer. Kappa over such a pair measures whether
# one party can transcribe, not whether the guidelines are clear, so the
# reporting path refuses to interpret it.
ADVISED = "advised"


@dataclass
class Agreement:
    pairs: int = 0
    observed: float = 0.0
    kappa: float = 0.0
    conflicts: list[str] = field(default_factory=list)
    advised_pairs: int = 0

    @property
    def independent(self) -> bool:
        """Whether a meaningful kappa exists at all.

        True when at least one pair was labelled by two annotators who were
        not told each other's answers. Advised pairs are excluded from the
        statistic rather than voiding it: a store that has ever held one
        would otherwise never report a kappa again, hiding the result of
        exactly the independent annotator it is trying to encourage.
        """
        return self.pairs > 0

    @property
    def interpretation(self) -> str:
        # Landis & Koch bands, named rather than left as a bare number.
        if self.pairs == 0:
            return "no independently double-labelled items yet"
        for threshold, label in ((0.81, "almost perfect"), (0.61, "substantial"),
                                 (0.41, "moderate"), (0.21, "fair")):
            if self.kappa >= threshold:
                return label
        return "poor -- rewrite the guidelines before labelling more"

    def render(self) -> str:
        lines = ["=== inter-annotator agreement ==="]
        if self.advised_pairs:
            lines += [
                f"  advised items          {self.advised_pairs}  (EXCLUDED)",
                "    one annotator was told the answer, so agreement on these",
                "    measures transcription rather than whether the guidelines",
                "    are clear. No kappa is computed over them.",
            ]
        lines.append(f"  independent pairs      {self.pairs}")
        if not self.pairs:
            # No number at all, rather than a number with a caveat beside
            # it: a figure on the page is quoted whatever sits next to it.
            lines.append(
                "\n  kappa NOT REPORTED -- no item has been labelled by two "
                "independent\n  annotators. That is the only thing that "
                "produces a meaningful number here.")
            return "\n".join(lines)
        lines += [
            f"  observed agreement     {self.observed:.3f}",
            f"  Cohen's kappa          {self.kappa:.3f} ({self.interpretation})",
        ]
        if self.conflicts:
            lines.append(f"  conflicts: {', '.join(self.conflicts[:6])}")
        return "\n".join(lines)


def agreement(store: LabelStore | None = None) -> Agreement:
    store = store or LabelStore()
    result = Agreement()

    first_votes, second_votes = [], []
    for item_id, labels in store.by_item().items():
        by_annotator: dict[str, int] = {}
        for label in labels:
            by_annotator.setdefault(label.annotator, label.value)
        if len(by_annotator) < 2:
            continue
        if any(ADVISED in (label.note or "") for label in labels):
            # Counted, then skipped. It contributes to neither the kappa nor
            # the conflict list, so an old advised batch cannot distort or
            # suppress a later independent annotator's result.
            result.advised_pairs += 1
            continue
        annotators = sorted(by_annotator)[:2]
        a, b = by_annotator[annotators[0]], by_annotator[annotators[1]]
        first_votes.append(a)
        second_votes.append(b)
        if a != b:
            result.conflicts.append(item_id)

    result.pairs = len(first_votes)
    if not result.pairs:
        return result

    agreed = sum(1 for a, b in zip(first_votes, second_votes) if a == b)
    result.observed = agreed / result.pairs

    # Chance agreement from each annotator's own marginal rate.
    p_a = sum(first_votes) / result.pairs
    p_b = sum(second_votes) / result.pairs
    expected = p_a * p_b + (1 - p_a) * (1 - p_b)
    result.kappa = ((result.observed - expected) / (1 - expected)
                    if expected < 1 else 1.0)
    return result


def adjudicate(store: LabelStore | None = None) -> list[dict]:
    """Items where annotators disagree, for a third pass.

    Returned rather than resolved by majority: two people reading one
    sentence differently usually means the sentence is genuinely ambiguous,
    and a vote hides that instead of fixing the definition.
    """
    store = store or LabelStore()
    out = []
    for item_id, labels in store.by_item().items():
        votes = {l.annotator: l.value for l in labels}
        if len(set(votes.values())) > 1:
            out.append({"item_id": item_id, "votes": votes,
                        "kind": labels[0].kind, "payload": labels[0].payload})
    return out
