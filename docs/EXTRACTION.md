# Claude extraction on the cluster

```bash
databricks bundle deploy -t dev --var="extractor=claude" --var="extract_limit=50"
databricks bundle run daily_call_intelligence -t dev
```

The extraction path was written against `claude-opus-5` long before it ever
ran. Running it produced one architectural finding and three failures that
each reported success.

## What the comparison showed

Fifty segments, same day, both extractors. Fifteen reached the model.

| field | agreement with the rules extractor |
|---|---|
| `product_id` | **15/15** |
| `type` | **15/15** |
| `severity` | 14/15 — the reboot defect: rules `critical`, Claude `high` |
| `issue_key` | **0/15** |

Product and type agreeing completely is unsurprising: entity resolution is
shared, and the type vocabulary is a four-way enum. The `issue_key` result
is the finding.

## Free-form issue keys break aggregation silently

Claude wrote sensible, descriptive keys — `VPN_TUNNEL_DROPS`,
`ACCESS_POINT_OVERHEATING`, `STATIC_ROUTES_LOST_ON_POWER_CYCLE`. Every one
is a better name than the rules extractor's. Every one is also wrong for
this pipeline, because `issue_key` is the **aggregation key**.

Two customers reporting one defect in different words get two keys, so they
aggregate separately, so neither reaches the distinct-customer threshold,
so nothing publishes. That run published **0** issues from 15 observations
and reported success — the counts all looked plausible, and every issue sat
at one mention.

The fix is a controlled vocabulary in the schema: `issue_key` is an enum of
the keys already in use, plus `NEW_ISSUE` with a proposed label for a
defect none of them covers. New defects still surface, through review,
rather than the vocabulary drifting one call at a time.

Re-run with the enum, the same fifteen segments:

```
STABILITY_PRAISE 3 · VPN_DISCONNECT 2 · BULK_EXPORT 2 · EXPORT_TIMEOUT 2
OVERHEATING 2 · SFP_PORT_COUNT 2 · SPONTANEOUS_REBOOT 1 · ROUTE_LOSS 1
```

Identical to the rules extractor's distribution on those segments.

## Three failures that each reported success

**Executor configuration did not reach the executors.** The notebook set
`CIP_EXTRACTOR=claude` and the API key on the driver. Extraction runs in
`mapInPandas` on separate processes that re-import `cip.config` and read
their own environment, so every worker silently used the rules extractor
and the job reported SUCCESS. The only trace was `extractor='rules-v1'` in
a column nobody was looking at. Configuration is now captured in the
closure, which is serialised to the workers, and the job compares the
backend that ran against the one requested.

**The SDK was not installed.** `anthropic` is an optional extra in the
wheel, so the serverless environment never had it. Fifteen identical
`ModuleNotFoundError`s were logged per-segment and skipped, producing a
run with zero observations and a green tick.

**Failures were invisible.** Per-segment errors went to an ephemeral log on
the executor. A batch where every call fails is a configuration problem
rather than unlucky inputs, so it now raises with the first error attached,
and the job refuses to report an empty metered run as a success.

## 200 segments, effort=low

```
observations 61 · candidates 9 · published 2 · queued 7
avg confidence 0.784 · extractor claude-opus-5
```

**The controlled vocabulary held completely.** All 61 observations used
canonical keys; no free-form keys and no `NEW__` proposals. The enum is
doing the work the prose instruction alone could not.

Claude also reproduced the polarity split unprompted — `VPN_DISCONNECT`
came back as 7 `bug_report` and 4 `praise`, correctly separating callers
reporting the defect from callers saying 7.2 fixed it. That distinction is
what sends the issue to human review rather than publishing a contradiction,
and it was not asked for in the schema.

### Switching extractor changed a published severity

Two rows were rewritten by this run:

```
X100 / SPONTANEOUS_REBOOT   critical (rules)  ->  high (claude-opus-5)
X100 / VPN_DISCONNECT       high              ->  high
```

The reboot severity was the single disagreement in the 15-segment
comparison, and here it reached the knowledge base. Neither reading is
obviously wrong — a device rebooting nightly unattended is arguably either
— but it is a reminder that severity is a *judgement* the extractor makes,
not a fact it copies, and that changing extractors rewrites judgements
already published. A production cutover wants both extractors run in
parallel and their disagreements reviewed, not a switch.

Because the run was capped, the `issues` table briefly mixed
rules-derived rows with Claude-derived ones. MERGE semantics mean state
persists across runs, which is correct for a knowledge base and confusing
for a comparison; the day was reprocessed with the rules extractor
afterwards to leave it coherent.

## The full day has not completed

An uncapped run was attempted -- roughly 1,200 model calls -- and stopped
partway:

```
ExtractionUnavailable: BadRequestError: 400 invalid_request_error
  Your credit balance is too low to access the Anthropic API.
```

The 50-segment run had consumed what was left on the account. Nothing about
the pipeline failed: the fatal-error classification did exactly what it was
built for, raising on the first credit error rather than skipping 1,200
failures one at a time into an empty run that reported success. The failure
took seconds, not the length of the batch.

The day was then reprocessed with the rules extractor to leave Unity
Catalog in a consistent state (1,195 observations, 6 published, 2 queued).
**Every headline figure in this repo is still the rules extractor's.** The
comparison above stands on 15 segments, which is enough to have found the
`issue_key` problem and not enough to characterise agreement at scale.

## Reasoning effort

Extraction now requests `output_config.effort = "low"`
(`CIP_EXTRACT_EFFORT`). Opus 5 runs adaptive thinking by default, and this
task cannot use the depth: the model reads one short segment and fills a
constrained schema whose `issue_key` is an enum. Across a day's ~1,200
calls the default was paying for reasoning the task never touches.

Lowering effort rather than disabling thinking is the documented route on
Opus 5 -- with thinking off it can write a tool call into visible text or
leak reasoning tags, and low effort avoids both while still cutting cost
and latency.

The 200-segment run above used `effort=low` throughout and produced
sensible extractions at average confidence 0.784, so the setting is not
degrading quality at this size. The *saving* remains unquantified: this
repo never captured per-call token usage, so the comparison against the
default effort is reasoned rather than measured.

## Cost

`extract_limit` is applied globally, before the work fans out. It was
originally applied inside the partition function, where 50 would have meant
50 *per partition* — up to 10,000 calls across 200 of them.

The 50-segment run costs a few cents. A full day at these rates is roughly
1,200 model calls.
