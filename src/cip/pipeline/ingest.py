"""Stage 1 -- land raw calls and read them back partition by partition.

At 10 TB/day nothing is loaded into a single process. The lake is
partitioned by date (and in production also by region and call centre),
and every downstream stage consumes one partition at a time so memory
stays flat regardless of the day's volume.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..config import CONFIG
from ..schemas import CallRecord


def list_partitions(day: str, region: str | None = None,
                    centre: str | None = None) -> list[Path]:
    """Partition files for a day, optionally pruned to a region or centre.

    Pruning happens by path, not by filtering rows after loading them. At
    10 TB a day that is the difference between reading one centre's traffic
    and reading everyone's in order to discard most of it.
    """
    day_dir = CONFIG.lake / f"date={day}"
    if not day_dir.exists():
        raise FileNotFoundError(f"no data landed for {day}: {day_dir}")
    pattern = (f"region={region}" if region else "region=*")
    pattern += "/" + (f"centre={centre}" if centre else "centre=*")
    found = sorted(day_dir.glob(f"{pattern}/part-*.jsonl"))
    # Days written before the layout changed keep working.
    return found or sorted(day_dir.glob("part-*.jsonl"))


def read_partition(path: Path) -> Iterator[CallRecord]:
    with path.open() as handle:
        for line in handle:
            if line.strip():
                yield CallRecord(**json.loads(line))


def manifest(day: str) -> dict:
    return json.loads((CONFIG.lake / f"date={day}" / "_MANIFEST.json").read_text())
