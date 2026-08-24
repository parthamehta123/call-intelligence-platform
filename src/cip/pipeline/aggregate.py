"""Stage 5 -- aggregate observations into issue candidates.

Fifty thousand customers reporting one firmware bug is one fact with
fifty thousand pieces of evidence, not fifty thousand facts. Everything
downstream operates on the aggregate; the individual observations are
retained only as provenance.

The counter that matters is *distinct customers*, not mentions. One angry
enterprise account calling forty times is a single corroborating source,
and treating it as forty is how an auto-update pipeline gets gamed.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..config import CONFIG
from ..schemas import IssueCandidate, Observation

RESOLVED_PREFIX = "RESOLVED CLAIM:"


def aggregate(observations: Iterable[Observation]) -> list[IssueCandidate]:
    groups: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for obs in observations:
        groups[(obs.product_id, obs.issue_key)].append(obs)

    candidates: list[IssueCandidate] = []
    for (product_id, issue_key), items in groups.items():
        # Majority polarity wins the summary; the minority becomes a conflict.
        affirming = [o for o in items if not o.summary.startswith(RESOLVED_PREFIX)]
        denying = [o for o in items if o.summary.startswith(RESOLVED_PREFIX)]
        majority = affirming if len(affirming) >= len(denying) else denying

        severities = [o.severity for o in majority]
        rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        timestamps = sorted(o.timestamp for o in items)

        candidates.append(IssueCandidate(
            product_id=product_id,
            issue_key=issue_key,
            type=majority[0].type,
            summary=majority[0].summary,
            severity=max(severities, key=lambda s: rank[s]),
            mentions=len(items),
            # Corroboration counts only well-attributed claims. A weakly
            # diarized turn might be the agent restating a known defect, and
            # letting it count as a distinct customer is precisely how
            # attribution error inflates the issues agents discuss most --
            # the ones already nearest the auto-accept threshold.
            distinct_customers=len({
                o.customer_id for o in items
                if o.attribution_confidence >= CONFIG.attribution_floor}),
            regions=sorted({o.region for o in items}),
            versions=sorted({o.product_version for o in items if o.product_version}),
            first_seen=timestamps[0],
            last_seen=timestamps[-1],
            mean_confidence=round(sum(o.confidence for o in majority) / len(majority), 3),
            evidence_ids=[o.observation_id for o in items],
            conflicts=(
                [f"{len(denying)}/{len(items)} reports claim the issue is resolved"]
                if denying and affirming else []
            ),
        ))

    candidates.sort(key=lambda c: (-c.distinct_customers, c.product_id, c.issue_key))
    return candidates
