"""Canonical product knowledge store + immutable evidence store.

SQLite here; Postgres or a property graph in production. The shape is
what matters:

  * `issues` holds *state* -- one row per (product, issue), never one row
    per call. Status is explicit: `observed` (customers say so) versus
    `confirmed` (engineering agrees). Only humans move a row to confirmed.
  * `evidence` is append-only. Every issue row can be traced back to the
    exact segments, calls, extractor and run that produced it.
  * `documents` is what the RAG index is built from, and it is derived
    from validated state -- never from raw transcripts.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .catalog import load_catalog
from .config import CONFIG
from .schemas import IssueCandidate, Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id     TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    family         TEXT,
    versions       TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    product_id    TEXT NOT NULL,
    issue_key     TEXT NOT NULL,
    type          TEXT NOT NULL,
    summary       TEXT NOT NULL,
    severity      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'observed',   -- observed | confirmed | refuted
    mentions      INTEGER NOT NULL,
    customers     INTEGER NOT NULL,
    regions       TEXT NOT NULL,
    versions      TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    confidence    REAL NOT NULL,
    updated_at    TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    PRIMARY KEY (product_id, issue_key)
);

CREATE TABLE IF NOT EXISTS evidence (
    observation_id TEXT PRIMARY KEY,
    product_id     TEXT NOT NULL,
    issue_key      TEXT NOT NULL,
    call_id        TEXT NOT NULL,
    segment_id     TEXT NOT NULL,
    customer_id    TEXT NOT NULL,
    region         TEXT NOT NULL,
    quote          TEXT NOT NULL,
    confidence     REAL NOT NULL,
    extractor      TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    run_id         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_issue ON evidence(product_id, issue_key);

CREATE TABLE IF NOT EXISTS review_queue (
    product_id  TEXT NOT NULL,
    issue_key   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (product_id, issue_key, created_at)
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id     TEXT PRIMARY KEY,
    product_id TEXT,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    source     TEXT NOT NULL,      -- product_doc | validated_issue | release_note
    status     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    dirty      INTEGER NOT NULL DEFAULT 1   -- CDC flag for incremental re-embedding
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    day         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT
);
"""


@contextmanager
def connect(path: Path | None = None):
    db_path = Path(path or CONFIG.kb_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        for product in load_catalog():
            conn.execute(
                "INSERT OR REPLACE INTO products VALUES (?,?,?,?)",
                (product["product_id"], product["canonical_name"],
                 product["family"], json.dumps(product["versions"])),
            )
        # Seed the documentation corpus RAG will retrieve over.
        for product in load_catalog():
            doc_id = f"doc::{product['product_id']}::overview"
            conn.execute(
                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?)",
                (doc_id, product["product_id"],
                 f"{product['canonical_name']} - product overview",
                 f"{product['canonical_name']} is part of the {product['family']} line. "
                 f"Supported firmware versions: {', '.join(product['versions'])}. "
                 f"Known aliases used by customers: {', '.join(product['aliases'])}.",
                 "product_doc", "published", _now(), 1),
            )


def start_run(run_id: str, day: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                     (run_id, day, _now(), None, None))


def finish_run(run_id: str, stats: dict) -> None:
    with connect() as conn:
        conn.execute("UPDATE runs SET finished_at=?, stats=? WHERE run_id=?",
                     (_now(), json.dumps(stats), run_id))


def write_evidence(observations: Iterable[Observation], run_id: str) -> int:
    """Append-only. Evidence is written even for candidates that get rejected,
    because 'why did we NOT act on this' is an audit question too."""
    rows = [
        (o.observation_id, o.product_id, o.issue_key, o.call_id, o.segment_id,
         o.customer_id, o.region, o.evidence, o.confidence, o.extractor,
         o.timestamp, run_id)
        for o in observations
    ]
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def upsert_issue(candidate: IssueCandidate, run_id: str) -> None:
    """Write canonical state. Called only by the guarded writer service."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT status FROM issues WHERE product_id=? AND issue_key=?",
            (candidate.product_id, candidate.issue_key)).fetchone()
        # An engineering-confirmed issue is never demoted by customer chatter.
        status = existing["status"] if existing and existing["status"] == "confirmed" else "observed"
        conn.execute(
            "INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate.product_id, candidate.issue_key, candidate.type, candidate.summary,
             candidate.severity, status, candidate.mentions, candidate.distinct_customers,
             json.dumps(candidate.regions), json.dumps(candidate.versions),
             candidate.first_seen, candidate.last_seen, candidate.mean_confidence,
             _now(), run_id),
        )
        # CDC: only the changed issue's document is marked for re-embedding.
        doc_id = f"doc::{candidate.product_id}::{candidate.issue_key}"
        conn.execute(
            "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?)",
            (doc_id, candidate.product_id,
             f"{candidate.product_id} {candidate.issue_key.replace('_', ' ').title()}",
             f"{candidate.summary}. Reported by {candidate.distinct_customers} distinct "
             f"customers across {', '.join(candidate.regions)} on versions "
             f"{', '.join(candidate.versions) or 'unspecified'}. Severity {candidate.severity}. "
             f"Status {status}. First seen {candidate.first_seen}, last seen {candidate.last_seen}.",
             "validated_issue", status, _now(), 1),
        )


def enqueue_review(candidate: IssueCandidate) -> None:
    from dataclasses import asdict
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO review_queue VALUES (?,?,?,?,?,?)",
                     (candidate.product_id, candidate.issue_key, candidate.decision_reason,
                      json.dumps(asdict(candidate)), "open", _now()))


def query(sql: str, params: tuple = ()) -> list[dict]:
    """Read-only helper. The policy engine independently enforces SELECT-only."""
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def dirty_documents() -> list[dict]:
    return query("SELECT * FROM documents WHERE dirty = 1")


def mark_clean(doc_ids: list[str]) -> None:
    if not doc_ids:
        return
    with connect() as conn:
        conn.executemany("UPDATE documents SET dirty = 0 WHERE doc_id = ?",
                         [(d,) for d in doc_ids])
