"""Does extracted evidence actually come from the customer?

The failure this measures is specific and was live in this repo: an agent
restating a known defect became a customer observation. Because agents
restate the *most reported* issues most often, the resulting inflation is
concentrated exactly where it does most damage -- on counts sitting near
the auto-accept threshold, where a few extra "customers" flip an issue
from human review to published product truth.

So the metric is not overall accuracy. It is contamination: what fraction
of observations quote something the customer never said.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from ..config import CONFIG
from ..pipeline.extract import extract
from ..pipeline.ingest import list_partitions, read_partition
from ..pipeline.preprocess import preprocess
from ..pipeline.route import for_extraction, route


@dataclass
class AttributionReport:
    observations: int = 0
    contaminated: int = 0
    agent_claims_present: int = 0
    weakly_attributed: int = 0
    contaminated_by_issue: Counter = field(default_factory=Counter)
    examples: list[str] = field(default_factory=list)

    @property
    def contamination_rate(self) -> float:
        return self.contaminated / self.observations if self.observations else 0.0

    def render(self) -> str:
        lines = [
            "=== attribution ===",
            f"  observations extracted          {self.observations}",
            f"  agent restatements in the day   {self.agent_claims_present} "
            f"(each an opportunity to misattribute)",
            f"  observations quoting the agent  {self.contaminated} "
            f"({self.contamination_rate:.2%})",
            f"  weakly attributed (< {CONFIG.attribution_floor})       "
            f"{self.weakly_attributed} "
            f"(kept, but confidence-scaled so they need more corroboration)",
        ]
        if self.contaminated_by_issue:
            lines.append("  contamination by issue:")
            lines += [f"    {k:<22} {v}" for k, v in self.contaminated_by_issue.most_common()]
        if self.examples:
            lines.append("  examples:")
            lines += [f"    {e[:100]}" for e in self.examples[:5]]
        return "\n".join(lines)


def _agent_claim_reached_the_customer_channel(segment_text: str, claim: str) -> bool:
    """Exact test, not a similarity heuristic.

    Extraction reads only `customer:` lines, so agent speech can reach an
    observation by exactly one route: diarization relabelled the agent turn
    as the customer. Ask that directly -- does the agent's sentence appear
    on a customer-labelled line?

    A fuzzy text-overlap check was wrong here and overcounted: the agent's
    restatement and the customer's own complaint describe the same defect in
    nearly the same words, so word-overlap flagged genuine customer reports
    as contamination.
    """
    claim_core = " ".join(claim.lower().split())
    for line in segment_text.splitlines():
        if not line.startswith("customer: "):
            continue
        spoken = " ".join(line[len("customer: "):].lower().split())
        if spoken and (spoken in claim_core or claim_core in spoken):
            return True
    return False


def evaluate_attribution(day: str = "2026-08-22") -> AttributionReport:
    label_path = CONFIG.lake / f"date={day}" / "_LABELS.jsonl"
    labels = {
        row["call_id"]: row
        for row in (json.loads(l) for l in label_path.read_text().splitlines() if l.strip())
    }
    report = AttributionReport(
        agent_claims_present=sum(1 for r in labels.values() if r.get("agent_claim_text")))

    for partition in list_partitions(day):
        segments = list(preprocess(read_partition(partition)))
        by_id = {segment.segment_id: segment for segment in segments}
        for observation in extract(for_extraction(route(segments))):
            report.observations += 1
            if observation.attribution_confidence < CONFIG.attribution_floor:
                report.weakly_attributed += 1
            claim = labels.get(observation.call_id, {}).get("agent_claim_text")
            segment = by_id.get(observation.segment_id)
            if claim and segment and _agent_claim_reached_the_customer_channel(
                    segment.text, claim):
                report.contaminated += 1
                report.contaminated_by_issue[observation.issue_key] += 1
                report.examples.append(
                    f"[{observation.issue_key}] conf={observation.confidence} "
                    f"attr={observation.attribution_confidence} :: {claim}")
    return report
