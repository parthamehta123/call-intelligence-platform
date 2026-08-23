"""Stage 2 -- clean, redact, and segment.

Three things happen here, all of them before any model is involved:

  * PII is redacted at the boundary, so customer identifiers are never
    persisted in the warehouse and never reach a model provider;
  * calls are segmented on speaker turns and topic shifts rather than at
    a fixed token count, because a chunk that straddles two complaints
    produces one confused extraction instead of two clean ones;
  * every segment is stamped as untrusted the moment it exists.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Iterator

from ..config import CONFIG
from ..schemas import CallRecord, Segment, TRUST_UNTRUSTED
from ..security.dlp import redact

# Cheap lexical markers of a topic boundary inside a support call.
TOPIC_SHIFT = (
    "another thing", "also", "separately", "one more", "by the way",
    "different issue", "unrelated", "second problem",
)


def _segment_id(call_id: str, index: int) -> str:
    return f"{call_id}-S{index:02d}"


def _is_boundary(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in TOPIC_SHIFT)


def segment_call(call: CallRecord) -> list[Segment]:
    groups: list[list[dict]] = [[]]
    for turn in call.turns:
        if turn["speaker"] == "customer" and _is_boundary(turn["text"]) and groups[-1]:
            groups.append([])
        groups[-1].append(turn)
        # Keep segments bounded so one rambling call cannot blow up a prompt.
        if sum(len(t["text"]) for t in groups[-1]) > CONFIG.max_segment_chars:
            groups.append([])

    segments: list[Segment] = []
    for index, group in enumerate(g for g in groups if g):
        raw = "\n".join(f"{t['speaker']}: {t['text']}" for t in group)
        clean, findings = redact(raw)
        mix: dict[str, int] = {}
        for turn in group:
            mix[turn["speaker"]] = mix.get(turn["speaker"], 0) + 1
        segments.append(Segment(
            segment_id=_segment_id(call.call_id, index),
            call_id=call.call_id,
            customer_id=call.customer_id,
            timestamp=call.timestamp,
            region=call.region,
            text=clean,
            speaker_mix=mix,
            product_hint=call.product_hint,
            trust=TRUST_UNTRUSTED,
            pii_redactions=len(findings),
        ))
    return segments


def dedupe(segments: Iterable[Segment], seen: set[str] | None = None) -> Iterator[Segment]:
    """Content-hash dedupe. Retries and re-transcriptions produce byte-identical
    segments; counting them twice inflates every downstream mention count."""
    seen = seen if seen is not None else set()
    for segment in segments:
        digest = hashlib.sha1(segment.text.encode()).hexdigest()
        key = f"{segment.customer_id}:{digest}"
        if key in seen:
            continue
        seen.add(key)
        yield segment


def preprocess(calls: Iterable[CallRecord], seen: set[str] | None = None) -> Iterator[Segment]:
    # One `seen` set for the whole partition: duplicates arrive as separate
    # calls (retries, re-transcriptions), not as repeats within one call.
    seen = seen if seen is not None else set()
    for call in calls:
        yield from dedupe(segment_call(call), seen)
