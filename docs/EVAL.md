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
| Generated (4,000) | 0.971 | **1.0000** | 30.6% | 0 |
| Hard cases (32) | 0.733 | **0.9167** | 46.9% | 1 |
| Hard cases, excluding the 3 ambiguous | 0.846 | 0.9167 | — | 1 |

The 30.6% kept is the funnel claim made concrete: ~69% of segments never
reach a model.

### What the first measurement changed

The initial run scored 0.9499 recall on the generated set with 60 misses,
**all sharing one cause**: entity resolution returned nothing, so the
segment scored 0.0 before any other feature was read. Two fixes followed,
both applying the same rule — *a term is evidence exactly while one product
owns it*:

| Fix | Confidence | Rationale |
|---|---|---|
| Generic category nouns (`router`, `console`, `gateway`, `wifi`) | 0.60 | `0.45 × 0.60 = 0.27` sits **below** the threshold on purpose. A category noun alone never admits a segment; it only carries one through alongside real problem language. *"My home router from another vendor"* still scores 0.27 and is correctly dropped. |
| Unambiguous version strings (`7.2` → X100) | 0.70 | *"After installing firmware 7.2 the VPN keeps disconnecting"* names no product at all. A version is a sharper signal than a category noun, so it ranks above it. `7.99` resolves to nothing — it is not a catalog version. |

Both indexes are built by checking uniqueness across the whole catalog. Ship
a second router and `router` stops being evidence automatically, rather than
silently resolving to whichever product happened to be listed first.

Result: recall 0.9499 → **1.0000**, precision 0.976 → 0.971, cost 29.1% →
30.6% kept. In the pipeline that recovered real signal — observations rose
1,138 → 1,198, and the X100 reboot issue went from 85 to 107 corroborating
customers.

The precision figure moved after this was first written, because the
*label* was corrected rather than the router — see "The label was wrong
before the router was" below. The recall figure is unchanged.

### Recall 1.0 means the set is exhausted, not that the router is good

Every signal the generator emits now contains a product noun, a curated
alias, or a catalog version, so the router resolves all of them. That is
the circularity in this set reaching its natural end: it has no remaining
power to discriminate, and a perfect score on it should be read as
"stop tuning against this", not as evidence of quality.

**The hard cases are now the only informative measure**, and they sit at
0.917 recall / 0.733 precision — or 0.846 precision excluding the three
ambiguous judgement calls.

### The label was wrong before the router was

Recall later slipped to 0.9860 with 17 apparent misses, and none of them
were misses. The generated set marked a segment positive if the injected
sentence appeared anywhere in it, ignoring **who said it** — so the
generator's own injected diarization errors, which move a claim onto an
`agent:` line, were labelled as customer speech. The router drops those,
correctly, and was charged for it. Making the truth function speaker-aware
restored recall to 1.0000 with 0 misses.

Two things are worth taking from that. The regression arrived when the
*generator* gained a feature, not when the router changed, so nothing in
the router's history explained it. And it was only visible because a
labelling pass disagreed with the eval and the disagreement was
investigated rather than reconciled — the labels were right and the eval
was wrong, which is not the direction anyone checks by default.

Of the 35 remaining false alarms, **24 are prompt-injection segments**
(the router keeps 43 of 57). Dropping those at the router would mean an
attack never reaches the security stage and is never recorded, so they are
counted against precision while arguably being correct behaviour.
Excluding them, precision is 0.9908.

## Three findings that change what to work on

**1 · Recall was capped by entity resolution, not by the threshold.**
Before the fix, no threshold above 0 could beat 0.9499. The lever was in a
different component entirely — which is the argument for measuring before
tuning, since the threshold is the obvious knob and it was the wrong one.

**2 · The fix was catalog coverage, and it cost almost nothing.**
+5 points of recall for +1.5 points of traffic kept. Cheap because the new
evidence is admitted at low confidence: it lifts segments that also carry
problem language, without lifting mentions-in-passing.

**3 · The threshold is in a dead zone.** On the generated set every value
from 0.05 to 0.40 produces identical metrics, because the rule-based
scorer emits a small number of discrete scores. The configured 0.35 was
never tuned; it just happens to sit in a flat region. On the generated set
0.45–0.55 looks better (precision 0.990–0.995 for two misses), but on
the hard cases 0.45 collapses recall from 0.917 to 0.750. **The circular
set would have told us to raise the threshold and the realistic set says
don't** — which is the entire argument for keeping both.

## Known weaknesses this surfaces

* **Paraphrase — still the one miss, and deliberately not fixed.**
  *"Exporting the audit report just spins forever and eventually gives
  up"* is dropped: the product resolves only via CRM hint (0.3375) and
  none of *spins forever* / *gives up* appear in the problem-term list.
  Adding those phrases would close it, and would be overfitting to a test
  case I wrote myself — with 32 self-authored cases, tuning the lexicon
  against them measures nothing. The real fix is an embedding router or a
  distilled classifier, evaluated on cases somebody else labelled.
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
