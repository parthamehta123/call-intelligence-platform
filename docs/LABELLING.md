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
