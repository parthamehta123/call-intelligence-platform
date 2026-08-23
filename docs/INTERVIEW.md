# Interview notes

## The 45-second answer

> I wouldn't make RAG the primary architecture, because 10 TB of
> previous-day calls is fundamentally an ingestion, extraction and
> state-management problem — RAG is a read pattern and this is a write
> pattern. I'd land raw calls in a partitioned lake, process them with
> Spark, transcribe and redact PII at the boundary, and put a cheap
> relevance router in front of the LLM so only product-bearing segments
> reach inference — that's roughly a 70% cut before any model runs. The
> LLM does schema-constrained extraction, not updating: it emits
> observations and holds no tools. I aggregate on distinct customers
> rather than mentions, resolve conflicts explicitly, keep "customers
> report X" separate from "engineering confirmed X", and only then write
> canonical state through a guarded writer, preserving evidence and
> provenance. RAG sits on top of that validated knowledge, hybrid because
> product IDs and versions need exact lexical matching, and counting
> questions go to SQL instead of vectors.

## The follow-up: "what if the agent has root?"

> Then RBAC alone is insufficient, because the authorized identity is
> exactly what gets manipulated through indirect prompt injection. I'd
> treat transcripts and retrieved content as tainted, separate reasoning
> from execution, and put a deterministic policy enforcement point in
> front of every privileged tool: the agent proposes, software decides.
> I'd remove arbitrary shell in favour of narrow domain tools, add DLP,
> egress allowlisting, taint tracking and human approval for irreversible
> actions. If root is unavoidable, it's root inside an ephemeral sandbox,
> not on the production host. IAM controls who may reach a capability; the
> agent-security layer controls whether this particular AI-generated
> action is safe.

## Numbers to quote from a real run

```
calls                4,000
segments             3,999
segments to LLM      1,166      -> 71% discarded before inference
PII redactions         304      -> removed before persistence
observations         1,138
issue candidates        12      -> 1,138 observations collapse to 12 facts
published                6
queued for review        2      -> conflicts + spec corrections
rejected                 4      -> includes injection-bearing segments
docs re-embedded        10      -> CDC, not a full re-index
red team            10 / 10 blocked
```

## Questions they will probe, and the short answers

**"Why not just fine-tune?"** Product facts change daily; weights are the
wrong storage medium for state that must be corrected, audited and rolled
back. Fine-tune for *format* and *domain language*, retrieve for *facts*.

**"How do you keep the index fresh without re-embedding 10 TB?"** The KB
sets a dirty flag on the documents an update actually changed; the indexer
re-embeds only those. Nightly cost tracks *changed* knowledge, not total
volume.

**"Two calls disagree — which wins?"** Neither, automatically. Majority
polarity forms the candidate, the minority is recorded as a conflict, and
any conflict routes to a human. Recency alone is a bad tiebreaker: the
newest caller is not the best-informed one.

**"What stops one angry customer changing the product record?"**
Corroboration is measured in distinct customers, not calls, and spec
corrections always require sign-off regardless of volume.

**"Where does an LLM-as-judge fit?"** As a *proposer* for ambiguous
conflicts, feeding the human queue. Never as the enforcement point — a
judge that can be argued with is not a control.

**"How would you make this near-real-time?"** Stages 2–6 are pure
`Iterable -> Iterator` functions. Swap the lake reader for Kafka +
Structured Streaming and micro-batch them; aggregation moves to windowed
state. Reconciliation thresholds need re-tuning, since corroboration
accrues over time.

**"What do you monitor?"** Data plane: TB processed, failed partitions,
lag, skew, duplicate rate. Model plane: extraction precision/recall
against a labelled sample, schema-failure rate, confidence distribution,
cost per 1,000 calls. Retrieval: Recall@K, nDCG, groundedness, citation
correctness, p95 latency. Security: policy denials by layer, declassif-
ication refusals, DLP hits, review-queue depth and age.

**"What breaks first at 10 TB?"** The router's precision. Everything
downstream is priced off it — a five-point drop in precision doubles the
inference bill, and a drop in *recall* silently loses product signal. It
gets a labelled eval set and a regression gate before anything else does.

## What this repo deliberately does not claim

- The rules extractor is a stand-in so the demo runs offline and
  deterministically; the Claude backend in `pipeline/extract.py` is the
  real path.
- The embedding is a hashed bag-of-words. Swap `embed()` for a real
  encoder — nothing else changes.
- Injection signatures catch demonstration attacks. They are a signal
  layer; capability narrowing and taint tracking do the containment.
