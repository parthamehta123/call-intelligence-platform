"""Append-only audit log.

Every policy decision is recorded with enough provenance to answer, after
the fact: what did the agent try to do, which call transcript influenced
it, and why did the engine allow or deny it.

Two rules this module learned the hard way on a cluster:

* **No filesystem work at import.** `audit = AuditLog()` runs on every
  Spark executor the moment `cip.security` is imported inside a UDF, and
  an executor's package directory is read-only. Creating a directory in
  `__init__` failed the whole stage with
  `OSError: [Errno 30] Read-only file system`.
* **A failed write must not fail the task.** Policy decisions that matter
  -- declassification, publication -- are made on the driver, where the log
  is writable. Executor-side events are per-task and local either way, so
  losing them must degrade rather than abort. Losses are counted and
  surfaced, never silent.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import CONFIG

_LOCK = threading.Lock()


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        # Path only. Nothing is created until something is actually written.
        self.path = Path(path or CONFIG.audit_path)
        self.dropped = 0
        self._warned = False

    def write(self, event: str, **fields: Any) -> dict:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "pid": os.getpid(),
            **{k: (asdict(v) if is_dataclass(v) and not isinstance(v, type) else v)
               for k, v in fields.items()},
        }
        line = json.dumps(record, default=str)
        try:
            with _LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            self.dropped += 1
            if not self._warned:
                self._warned = True
                print(f"[cip.audit] log unwritable ({exc.strerror}); "
                      f"events on this process will be counted, not stored: "
                      f"{self.path}", file=sys.stderr)
        return record

    def tail(self, n: int = 20, event: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        records = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        if event:
            records = [r for r in records if r["event"] == event]
        return records[-n:]

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


audit = AuditLog()
