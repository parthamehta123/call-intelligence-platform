# Building a labelled eval set

```bash
make label-router ANNOTATOR=your-name     # ~50 items, a few minutes
make label-status                         # coverage and agreement
```

## Why this exists

Every eval figure in this repo rests on labels written by the person who
built the system. That is the single largest threat to them, and it is not
fixable by writing more labels the same way. A model has already found one
of mine wrong: the PULSE7 overview was marked relevant to a query about
thermal behaviour, and the document contains no thermal content at all
(`label_revised` in `eval/retrieval_cases.jsonl`).

So this is the machinery for somebody else to produce them.

## The two decisions that matter

**The annotator never sees the model's score or prediction.** Someone told
what the system thinks agrees with it, and an independent label becomes a
confirmation. The score is kept in `provenance` for analysis afterwards and
withheld from the prompt; a test asserts it never appears.

**Sampling is weighted toward the decision boundary.** A labelling budget
is somebody's attention, and items the system already gets confidently
right teach nothing:

| stratum | share | why |
|---|---|---|
| boundary (within 0.05 of the threshold) | 45% | where a wrong threshold actually costs something |
| near (within 0.20) | 30% | the shape of the slope |
| confident keep | 15% | a check that confidence is warranted |
| confident drop | 10% | the same, for the discards |

The rule-based router emits discrete scores, so few segments land near the
threshold — a real consequence of the "dead zone" measured in
`docs/EVAL.md`. When a stratum runs short the shortfall is redistributed
rather than shrinking the pool, because the budget is set by the labeller's
time and not by how the scores happen to bunch.

## Disagreement is data

Two annotators labelling one item produce two records. A store that keeps
the last write destroys the only signal that says a definition is unclear.

`make label-status` reports **Cohen's kappa**, not percent agreement.
On a skewed set two annotators who both guess "no" agree 90% of the time
while sharing no judgement at all; kappa corrects for that. It is a
property of the *guidelines* rather than the people, so a low value means
stop and rewrite the definition — collecting more labels against an
ambiguous one just buys more noise.

Contested items are **excluded** from the export rather than resolved by
majority vote. An eval built on the cases people could not agree about
measures the ambiguity, not the router.

## What it does not do

It does not make the labels good. It makes them *somebody else's*, records
who produced each one and how long they took, and refuses to hide the
disagreements. The numbers only improve when a second person actually sits
down with it.

## First pass, and what it found

A 50-item router pass exists in the store under annotator
`claude-opus-5`. **It is a model label set, not a human one**, and it is
named that way so no reader mistakes it for independent validation: the
generator, the guidelines, and these labels share an author, so agreement
between them is consistency, not correctness. The human pass is still
outstanding, and remains the only thing that can break that circle.

With that caveat, the pass was still worth running, because it disagreed
with the eval in two places that turned out to be the eval's fault.

**1. The generated set mislabels diarization flips.** `load_generated`
marks a segment positive iff `signal_text in segment.text`, with no check
on which speaker said it. 25 of 4000 segments carry the claim on an
`agent:` line — the generator's own injected diarization errors. The
router correctly drops them; the eval counts each as a miss.

| truth definition | precision | recall | f1 |
|---|---|---|---|
| `signal in text` (was) | 0.9780 | 0.9860 | 0.9820 |
| speaker-aware (now) | 0.9714 | **1.0000** | 0.9855 |

The reported recall of 0.986 was a floor imposed by the label, not by the
router. There were no true misses. **Fixed** — `_customer_states` now
requires the sentence to appear on a `customer:` line, with a regression
test in `tests/test_router_eval.py`.

**2. The precision figure is mostly injections.** Of the 35 false
positives under the speaker-aware truth, **24 were prompt-injection
segments** — and the router was forwarding only 43 of 57. That is
arguably correct behaviour being penalised: a segment dropped at the
router never reaches the security stage and is never recorded as an
attack. Scored with injections excluded from the penalty, precision is
0.9908.

So "precision 0.978 / recall 0.986" resolves into: no real misses, and a
precision cost that is a deliberate routing decision rather than an error.
Neither is visible while the label ignores the speaker.

**Fix the label, not the router.** Both effects came from the truth
function; neither was a router defect. Both are now fixed: the speaker
check landed first, and injections became a third class rather than
negatives (`docs/EVAL.md`, "Injections are a third class").

Making that change also turned the second finding into a security metric
rather than a precision footnote — and the metric immediately showed the
router dropping **14 of 57** injections before the security stage ever saw
them. The number that looked like a scoring quibble was a gap.

It is now closed: the injection detector was given its own path to the
router's keep decision, and both channels read 57/57 and 2/2 with the
funnel's cost unchanged (`docs/SECURITY.md`). Worth stating plainly, since
it is the argument for doing any of this — **a labelling pass found a
security hole**. Not by looking for one; by disagreeing with an eval about
five segments and following the disagreement instead of settling it.
