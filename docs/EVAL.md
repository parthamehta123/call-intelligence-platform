# Router evaluation

The relevance router is the single highest-leverage component in this
pipeline and, until now, the only major one with no number attached to it.
It decides what an LLM is paid to read and what is thrown away forever.

Run it:

```bash
make eval-router
```

## Why recall, not F1

The two error types are not symmetric, and optimising F1 pretends they
are:

| Error | Cost |
|---|---|
| False positive | one extra inference call |
| False negative | the customer's report is gone — no later stage recovers a discarded segment |

So recall is the constraint and precision is the budget. `best_threshold()`
takes a recall floor and returns the *cheapest* threshold meeting it,
rather than maximising F1.

## Two sets, measuring different things

**Generated** — 4,000 segments, labels taken from the generator's sidecar
(`_LABELS.jsonl`), which the pipeline never reads. Labels are exact. But
the generator and the router share an author, so this set is circular: it
shows whether the router catches the patterns the generator emits, not
whether it generalises. A good score here is necessary, not sufficient.

**Hard cases** — 32 hand-written segments in `eval/router_cases.jsonl`,
deliberately phrased unlike anything the generator produces: paraphrase,
anaphora (*"that thing where it disconnects"*), alias-only reference,
products named in calls that are actually about billing, injection
payloads that name a real SKU. Three sit close to the line and are flagged
`ambiguous` so metrics can be reported with and without them. **The labels
are mine, and label quality is the ceiling on what these numbers mean.**

## Results at the configured threshold (0.35)

| Set | Precision | Recall | Kept | Missed |
|---|---|---|---|---|
| Generated (4,000) | 0.976 | **0.9499** | 29.1% | 60 |
| Hard cases (32) | 0.733 | **0.9167** | 46.9% | 1 |

The 29.1% kept is the funnel claim made concrete: ~71% of segments never
reach a model.

## Three findings that change what to work on

**1 · Recall is capped at 0.9499, and the threshold cannot lift it.**
Every threshold above 0 yields the same ceiling. Reaching 0.95 requires
threshold 0.0 — disabling the funnel entirely. So the threshold is not the
binding constraint.

**2 · All 60 missed segments have one identical cause: no product
resolved.** Not weak problem-term matching, not scoring — entity
resolution simply returns nothing, so the segment scores 0.0 and is
dropped before any other feature is considered. The clearest example:

> *"The router reboots on its own at night after firmware 7.2."*

The catalog knows `the enterprise router` and `big router` as aliases, but
not bare `router`, and that call carried no CRM product hint. **Router
recall is gated by catalog coverage, not by the scorer.** That is where
the next work goes — broadening aliases, and back-filling the product hint
from the account's owned products rather than the case record alone.

**3 · The threshold is in a dead zone.** On the generated set every value
from 0.05 to 0.40 produces identical metrics, because the rule-based
scorer emits a small number of discrete scores. The configured 0.35 was
never tuned; it just happens to sit in a flat region. On the generated set
0.45–0.55 is strictly better (precision 1.000 at the same recall), but on
the hard cases 0.45 collapses recall from 0.917 to 0.750. **The circular
set would have told us to raise the threshold and the realistic set says
don't** — which is the entire argument for keeping both.

## Known weaknesses this surfaces

* **Paraphrase.** *"Exporting the audit report just spins forever"* is
  missed: the product resolves only via CRM hint (0.75 confidence → 0.3375)
  and none of *spins forever* / *gives up* appear in the problem-term list.
  A lexical scorer cannot fix this; an embedding router or a distilled
  classifier can.
* **Product named without a claim.** *"I bought the X100 two years ago.
  Anyway, I'm calling about my billing address"* passes. Expected for a
  bag-of-terms scorer.
* **Injection payloads can pass.** One of two does. This is survivable
  rather than alarming — extraction is toolless and schema-constrained, so
  an injected segment becomes a low-confidence observation that
  reconciliation rejects. `test_injection_payloads_never_dominate_the_signal`
  asserts injection never outscores genuine signal.

## Regression gate

`tests/test_router_eval.py` fixes floors just under measured behaviour, so
a degradation fails CI rather than appearing in a report nobody reads:

* recall ≥ 0.90 and precision ≥ 0.70 on hard cases
* funnel keeps < 60% of hard-case traffic
* injection never scores above genuine signal
* no higher threshold beats the configured one on F1 — guards against a
  threshold nudged until the numbers looked good
* an unreachable recall floor returns `None` instead of the least-bad option

## What this still does not measure

Retrieval quality — Recall@K, nDCG, groundedness, citation correctness —
remains unmeasured. This work covers the funnel only. And 32 hand-written
cases is a smoke test, not a benchmark: the honest next step is a few
hundred segments labelled by someone who did not write the router.
