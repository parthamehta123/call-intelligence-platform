"""Two labelled sets, measuring two different things.

**Generated** (thousands of segments): labels come from the synthetic
generator's sidecar, so they are exact. But the router and the generator
share an author, which makes this set circular -- it shows whether the
router catches the patterns the generator emits, not whether it
generalises. High scores here are necessary, not sufficient.

**Hard cases** (hand-written): deliberately phrased unlike anything the
generator produces -- paraphrase, anaphora, alias-only reference, products
named in calls that are about billing, injection payloads that mention a
real SKU. This is the set that can actually embarrass the router, and the
one to watch when the router changes.

Labelling rule, applied consistently to both:

    POSITIVE = the customer states something about a catalogued product
               that could update product knowledge (defect, feature
               request, spec correction, or praise about behaviour).
    NEGATIVE = everything else, including questions that request product
               state rather than assert it.

Three hard cases sit close to that line and are flagged `ambiguous`, so
metrics can be reported with and without them. Labels are mine; label
quality is the ceiling on what any of these numbers are worth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import CONFIG, ROOT
from ..pipeline.ingest import list_partitions, read_partition
from ..pipeline.preprocess import segment_call
from ..schemas import Segment

HARD_CASES = ROOT / "eval" / "router_cases.jsonl"


@dataclass
class EvalCase:
    segment: Segment
    label: int
    category: str
    why: str = ""
    ambiguous: bool = False


def load_hard_cases(path: Path | None = None) -> list[EvalCase]:
    rows = [json.loads(l) for l in
            Path(path or HARD_CASES).read_text().splitlines() if l.strip()]
    cases: list[EvalCase] = []
    for i, row in enumerate(rows):
        text = f"customer: {row['customer']}"
        if row.get("agent"):
            text += f"\nagent: {row['agent']}"
        cases.append(EvalCase(
            segment=Segment(
                segment_id=f"HARD-{i:03d}", call_id=f"HARD-{i:03d}",
                customer_id=f"U{i}", timestamp="2026-08-22T12:00:00+00:00",
                region="US", text=text, speaker_mix={"customer": 1},
                product_hint=row.get("product_hint"),
            ),
            label=int(row["label"]),
            category=row["category"],
            why=row.get("why", ""),
            ambiguous=bool(row.get("ambiguous", False)),
        ))
    return cases


def load_generated(day: str = "2026-08-22", limit: int | None = None) -> list[EvalCase]:
    """Segment-level labels derived from the generator's sidecar.

    A segment is positive iff it contains the exact sentence the generator
    injected for that call. Matching on the sentence rather than the call
    keeps the label attached to the segment that actually carries the
    signal, which matters whenever a call splits into several.
    """
    label_path = CONFIG.lake / f"date={day}" / "_LABELS.jsonl"
    if not label_path.exists():
        raise FileNotFoundError(
            f"no labels for {day}: {label_path}. Run `make generate` first.")

    signals = {
        row["call_id"]: row["signal_text"]
        for row in (json.loads(l) for l in label_path.read_text().splitlines() if l.strip())
    }

    cases: list[EvalCase] = []
    for partition in list_partitions(day):
        for call in read_partition(partition):
            signal = signals.get(call.call_id)
            for segment in segment_call(call):
                carries = bool(signal) and signal in segment.text
                cases.append(EvalCase(
                    segment=segment,
                    label=int(carries),
                    category="generated_signal" if carries else "generated_noise",
                ))
            if limit and len(cases) >= limit:
                return cases
    return cases
