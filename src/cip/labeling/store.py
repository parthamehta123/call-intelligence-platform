"""Where labels live.

Append-only JSONL, one record per judgement, carrying who made it and when.
Two annotators labelling the same item produce two records rather than one
overwriting the other -- disagreement is the signal that a definition is
unclear, and a store that silently keeps the last write destroys it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import DATA


@dataclass
class Label:
    item_id: str
    kind: str
    value: int                      # 1 relevant / product signal, 0 not
    annotator: str
    created_at: str = ""
    note: str = ""
    # Seconds spent. A label given in under a second is worth less than one
    # that took ten, and knowing which is which matters when a number is
    # challenged.
    seconds: float = 0.0
    payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.created_at = self.created_at or datetime.now(timezone.utc).isoformat()


class LabelStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DATA / "labels.jsonl")

    def append(self, label: Label) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(asdict(label)) + "\n")

    def all(self) -> list[Label]:
        if not self.path.exists():
            return []
        return [Label(**json.loads(line))
                for line in self.path.read_text().splitlines() if line.strip()]

    def by_item(self) -> dict[str, list[Label]]:
        grouped: dict[str, list[Label]] = {}
        for label in self.all():
            grouped.setdefault(label.item_id, []).append(label)
        return grouped

    def labelled_by(self, annotator: str) -> set[str]:
        return {l.item_id for l in self.all() if l.annotator == annotator}

    def summary(self) -> dict:
        grouped = self.by_item()
        annotators = {l.annotator for l in self.all()}
        return {
            "labels": len(self.all()),
            "items": len(grouped),
            "annotators": sorted(annotators),
            "double_labelled": sum(1 for v in grouped.values()
                                   if len({l.annotator for l in v}) > 1),
        }
