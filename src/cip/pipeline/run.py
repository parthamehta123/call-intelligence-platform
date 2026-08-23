"""Daily orchestration.

The local runner walks partitions; on a cluster the same three functions
become `mapPartitions` stages and the loop disappears. Keeping the stage
signatures as `Iterable -> Iterator` is what makes that swap mechanical
rather than a rewrite.

Nothing in this file writes to the knowledge base directly. Publication
goes through declassification and then the guarded writer, so the trust
boundary holds even for code we wrote ourselves.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .. import kb, tools
from ..config import CONFIG
from ..retrieval import refresh_index
from ..schemas import Observation
from ..security.audit import audit
from ..security.declassify import DeclassificationRefused, declassify_candidate
from ..security.policy import PolicyViolation
from .aggregate import aggregate
from .extract import extract
from .ingest import list_partitions, manifest, read_partition
from .preprocess import preprocess
from .reconcile import reconcile
from .route import route


def process_partition(path: Path) -> tuple[list[Observation], dict]:
    """One partition, start to finish. This is the `mapPartitions` body."""
    stats = Counter()
    segments = []
    for segment in preprocess(read_partition(path)):
        stats["segments"] += 1
        stats["pii_redactions"] += segment.pii_redactions
        segments.append(segment)

    routed = list(route(segments))
    stats["segments_to_llm"] = len(routed)

    observations = list(extract(routed))
    stats["observations"] = len(observations)
    return observations, dict(stats)


def run_day(day: str, workers: int = 1) -> dict:
    run_id = "R" + hashlib.sha1(
        f"{day}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:10]
    kb.init()
    kb.start_run(run_id, day)
    audit.write("run_started", run_id=run_id, day=day, extractor=CONFIG.extractor)

    partitions = list_partitions(day)
    totals = Counter()
    observations: list[Observation] = []

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(process_partition, partitions))
    else:
        results = [process_partition(p) for p in partitions]

    for partition_obs, stats in results:
        observations.extend(partition_obs)
        totals.update(stats)

    kb.write_evidence(observations, run_id)
    candidates = reconcile(aggregate(observations))

    published, queued, refused = 0, 0, 0
    by_key = {}
    for obs in observations:
        by_key.setdefault((obs.product_id, obs.issue_key), []).append(obs)

    for candidate in candidates:
        evidence = by_key.get((candidate.product_id, candidate.issue_key), [])
        try:
            validated = declassify_candidate(candidate, evidence)
        except DeclassificationRefused:
            if candidate.decision in ("review", "auto_accept"):
                tools.enqueue_human_review(
                    role=tools.WRITER_SERVICE,
                    purpose="candidate failed declassification",
                    product_id=candidate.product_id,
                    candidate=candidate,
                )
                queued += 1
            else:
                refused += 1
            continue

        try:
            tools.publish_issue_update(
                role=tools.WRITER_SERVICE,
                purpose="validated daily product knowledge update",
                product_id=validated.product_id,
                issue_key=validated.issue_key,
                candidate=validated,
                run_id=run_id,
            )
            published += 1
        except PolicyViolation:
            queued += 1

    reindexed = refresh_index()

    day_manifest = manifest(day)
    stats = {
        "run_id": run_id,
        "day": day,
        "calls": day_manifest["counts"]["total"],
        "segments": totals["segments"],
        "segments_to_llm": totals["segments_to_llm"],
        "funnel_reduction": round(
            1 - totals["segments_to_llm"] / max(totals["segments"], 1), 4),
        "pii_redactions": totals["pii_redactions"],
        "observations": totals["observations"],
        "candidates": len(candidates),
        "published": published,
        "queued_for_review": queued,
        "rejected": refused,
        "documents_reindexed": reindexed,
    }
    kb.finish_run(run_id, stats)
    audit.write("run_finished", **stats)
    return stats
