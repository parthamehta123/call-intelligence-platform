"""The complete tool surface. Every privileged capability lives here.

Two design choices do most of the security work, before any detection
logic runs:

  1. There is no `shell`, no `execute_sql`, no `write_file`. The tools are
     narrow and domain-shaped, so the most an attacker can express through
     them is a legitimate product update.
  2. The extraction agent -- the only component that reads untrusted
     customer speech -- holds *no* tools at all. It returns JSON to the
     pipeline. It cannot reach this module.

`delete_product` exists deliberately: it is the action the injected
transcripts ask for, and it is here to demonstrate that the request is
refused by policy, not by the model declining to cooperate.
"""

from __future__ import annotations

import re
from typing import Any

from . import kb
from .config import CONFIG
from .security.policy import Capability, guarded_tool

# Roles. RBAC layer -- necessary, and by itself not sufficient.
EXTRACTION_AGENT = "extraction_agent"   # reads untrusted text, holds no tools
WRITER_SERVICE = "writer_service"       # writes validated state only
ANALYST_AGENT = "analyst_agent"         # answers questions, read-only
ADMIN = "admin"                         # humans

_ID = re.compile(r"^[A-Z0-9_\-]{2,40}$")
_KEY = re.compile(r"^[A-Z0-9_]{3,60}$")


@guarded_tool(Capability(
    name="publish_issue_update",
    effect="write",
    risk="high",
    roles=frozenset({WRITER_SERVICE}),
    accepts_untrusted_args=False,   # requires an explicit declassification
    arg_validators={
        "product_id": lambda v: bool(_ID.match(str(v))),
        "issue_key": lambda v: bool(_KEY.match(str(v))),
    },
))
def publish_issue_update(*, product_id: str, issue_key: str, candidate: Any, run_id: str) -> str:
    kb.upsert_issue(candidate, run_id)
    return f"published {product_id}/{issue_key}"


@guarded_tool(Capability(
    name="enqueue_human_review",
    effect="write",
    risk="low",
    roles=frozenset({WRITER_SERVICE}),
    accepts_untrusted_args=True,    # a review queue is exactly where untrusted
                                    # material is supposed to end up
    arg_validators={"product_id": lambda v: bool(_ID.match(str(v)))},
))
def enqueue_human_review(*, product_id: str, candidate: Any) -> str:
    kb.enqueue_review(candidate)
    return f"queued {product_id}/{candidate.issue_key} for review"


@guarded_tool(Capability(
    name="delete_product",
    effect="write",
    risk="critical",
    roles=frozenset({ADMIN}),
    accepts_untrusted_args=False,
    requires_human_review=True,
    arg_validators={"product_id": lambda v: bool(_ID.match(str(v)))},
))
def delete_product(*, product_id: str) -> str:  # pragma: no cover - never reached in demo
    with kb.connect() as conn:
        conn.execute("DELETE FROM issues WHERE product_id = ?", (product_id,))
    return f"deleted {product_id}"


@guarded_tool(Capability(
    name="search_knowledge",
    effect="read",
    risk="low",
    roles=frozenset({ANALYST_AGENT, ADMIN}),
    accepts_untrusted_args=True,    # a user's question is untrusted, and read-only
))
def search_knowledge(*, query: str, top_k: int = 5) -> list[dict]:
    from .retrieval import hybrid_search
    return hybrid_search(query, top_k=top_k)


@guarded_tool(Capability(
    name="query_product_state",
    effect="read",
    risk="medium",
    roles=frozenset({ANALYST_AGENT, ADMIN}),
    accepts_untrusted_args=True,
))
def query_product_state(*, sql: str) -> list[dict]:
    """Counting questions belong in SQL, not in a vector store."""
    return kb.query(sql)


@guarded_tool(Capability(
    name="notify_webhook",
    effect="network",
    risk="high",
    roles=frozenset({WRITER_SERVICE, ADMIN}),
    accepts_untrusted_args=False,
))
def notify_webhook(*, url: str, body: str) -> str:  # pragma: no cover - demo stub
    return f"POST {url} ({len(body)} bytes) -- allowlist {CONFIG.egress_allowlist}"
