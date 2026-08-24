# Diarization-aware extraction

## The failure

Before this, an agent restating a known defect became a customer
observation:

```
agent: Yes, we're aware firmware 7.2 makes the VPN keep disconnecting.
  -> Observation(product=X100, issue=VPN_DISCONNECT, confidence=0.83)
     attributed to that caller
```

`_customer_lines()` filtered to `customer:` turns but fell back to
`or segment.text` when a segment had none — so agent speech was read as the
customer's.

## Why it is worse than a random error

Support agents restate the *most reported* defects most often; that is what
good support does. So misattribution is not noise sprayed evenly across the
catalog — it concentrates on the issues that already have the most reports,
which are exactly the ones sitting near the auto-accept threshold. A
handful of phantom customers there flips an issue from human review to
published product truth.

Run `make eval-attribution`.

## What was built

**Turn-level provenance.** Turns carry `speaker_confidence`; a segment
carries `customer_turns` and `attribution_confidence` (the *minimum*
confidence across its customer turns — the conservative reading, since a
flat text rendering cannot say which turn a matched phrase came from).

**Agent speech is never a source.** No fallback. A segment with no customer
turns yields no observation and is not routed at all — that is a cost win
as well as a correctness one. `Observation.validate()` rejects any
`speaker` other than `customer`, so the rule is enforced at the schema
boundary rather than trusted to the extractor.

**Uncertainty scales confidence, it does not delete.** A weakly diarized
claim is still extracted, with `confidence *= attribution_confidence`. It
needs more corroboration to clear reconciliation, which is the honest
treatment of an uncertain claim.

**Corroboration counts only well-attributed claims.** `distinct_customers`
— the signal reconciliation actually trusts — ignores observations below
`attribution_floor` (0.50). This is what defeats the inflation attack:

```
4 genuine customers + 20 mislabelled agent turns
  -> mentions 24, distinct_customers 4  -> review, not auto-accept
```

**The Claude backend is told the same contract**, and it is re-checked
locally: the model can read speaker labels but cannot know how good they
are, so its confidence is scaled by attribution too.

## Two bugs this surfaced

**Product identity was read from agent speech.** `identify_product()`
resolved against the whole segment. A caller asking about billing while the
agent said "the cloud console" resolved to MERIDIAN at 0.9, which scored
0.405 on its own and cleared the 0.35 threshold — putting 290 segments
containing no customer product-talk in front of a model. Resolution now
reads the customer channel only. The CRM `product_hint` is still exempt: it
is structured metadata, not speech.

Router precision 0.800 → **0.967**, false positives 290 → 39.

**Issue and product came from different sentences.** Patterns were matched
across concatenated customer text, so an issue found in one utterance was
paired with a product resolved from another. That minted combinations
nobody reported — `MERIDIAN/VPN_DISCONNECT`, `PULSE7/SPONTANEOUS_REBOOT`,
`XG482/OVERHEATING` — and filled the review queue with 16 items, 14 of them
fictional. Extraction now works per utterance.

Candidates 24 → **8**, review queue 16 → **2**.

Both were latent before diarization existed. A second topic in the segment
is what made them common enough to see.

## Measured

```
observations extracted          1152
agent restatements in the day    490   (each an opportunity to misattribute)
observations quoting the agent    11   (0.95%)
weakly attributed (< 0.50)        78   (kept, confidence-scaled)
```

The 0.95% that gets through are turns where diarization relabelled the
agent as the customer. Nothing downstream can tell — but because those
errors correlate with low confidence, every one of them lands at
confidence 0.37–0.43 against 0.83 for a clean customer report, and none
count toward corroboration.

## A third bug: adding fields broke the deployed job

The two new columns deployed fine and the job failed on the next read with
`UNRESOLVED_COLUMN: customer_turns`. `CREATE TABLE IF NOT EXISTS` is not
schema evolution -- it is a no-op against an existing table, so the columns
were never added to Unity Catalog.

`ddl.evolve()` now diffs declared columns against the live table and issues
`ALTER TABLE ... ADD COLUMNS` for whatever is missing, as part of setup.
Additive only: dropping or retyping a column is destructive and belongs in
a reviewed migration, not a job that runs nightly. Adding fields is the
common case -- this change alone added two -- so it should not require
someone to remember.

## What this is not

Real diarization is **not implemented** — there is no audio, and speaker
labels plus confidences come from the generator. What is built is the
*contract* a diarizer must satisfy and the handling of its errors. Swapping
in a real diarizer means populating `speaker_confidence` from it; nothing
downstream changes.

The minimum-confidence-per-segment rule is coarse. Attribution is tracked
per segment, not per matched phrase, so one badly diarized turn discounts
every observation from that segment.
