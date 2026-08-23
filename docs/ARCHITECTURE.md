# Architecture

## The framing

The request is "an agent reads yesterday's customer calls, and product
details get updated." The instinct is to reach for RAG. RAG is the wrong
primary abstraction here for one structural reason:

> RAG is a **read** pattern. This requirement is a **write** pattern.

Retrieval fetches context to answer a question. Here, nobody has asked a
question yet — 10 TB of calls must be turned into durable changes to
product state. That is an ingestion, extraction, reconciliation and
state-management problem. RAG belongs at the far end, over the knowledge
that this pipeline has already validated.

Three failure modes follow from getting that backwards:

| If you treat it as RAG | What actually happens |
|---|---|
| Embed all 10 TB nightly | Cost scales with total volume instead of *changed* volume; the index is stale by construction |
| Let retrieval answer "how many customers reported X?" | Vector similarity cannot count. You get a confident wrong number |
| Let the model write what it retrieves | One customer's claim overwrites engineering's; contradictions resolve by recency |

## Stage map

```
                    10 TB CUSTOMER CALLS / DAY
                              |
   [1] INGEST                 v
   partitioned lake    s3://calls/raw/date=/region=/centre=
   (durable, replayable)      |
                              v
   [2] PREPROCESS      transcribe -> diarize -> PII redact -> dedupe
   trust boundary      -> topic-aware segmentation
   set here                   |
                              |  every segment stamped UNTRUSTED
                              v
   [3] ROUTE           cheap classifier: is this product signal?
   the funnel                 |
                    ~70% discarded before any model runs
                              |
                              v
   [4] EXTRACT         LLM, schema-constrained, NO TOOLS
   information               |   -> Observation{product, issue_key,
   extraction                |      severity, evidence, confidence}
                              v
   [5] AGGREGATE       group by (product, issue); count DISTINCT
   many->one                  |   CUSTOMERS, not mentions
                              v
   [6] RECONCILE       corroboration? conflict? confidence?
   the gate                   |
                    +---------+---------+
                    |                   |
              auto_accept             review
                    |                   |
        [7] DECLASSIFY            human queue
        (audited, one place)           |
                    |                   |
                    v                   v
   [8] PUBLISH   guarded writer service ---> KB (state) + evidence (provenance)
                              |
                              v CDC: only changed docs re-embedded
                    +---------+---------+
                    |                   |
                 BM25 index        vector index
                    +---------+---------+
                              v
                          RRF + rerank
                              |
   [9] SERVE          agent with two read-only tools:
                      SQL (counts, rankings) | search (descriptions)
```

## Why each stage exists

**[1] Lake first.** Never process straight off the wire. A partitioned,
immutable landing zone gives replay when the extractor changes, and
partitioning is what lets every later stage run one shard at a time so
memory is flat regardless of the day's volume.

**[2] Redact at the boundary.** PII is removed before persistence and
before any model provider sees it. Segmentation follows speaker turns and
topic markers, not a token count — a chunk straddling two complaints
yields one confused extraction instead of two clean ones.

**[3] The funnel is the cost architecture.** Support calls are mostly
greetings, holds and thanks. A cheap scorer drops those. In the demo it
discards ~70% of segments; the same threshold at 10 TB/day is the
difference between a viable and an absurd inference bill. The interface is
`score -> [0,1]`, so a distilled classifier or embedding router drops in
without touching another stage.

**[4] Extraction, not summarisation.** The model gets one segment and
returns one `Observation` conforming to a JSON schema. It does not decide
truth, resolve contradictions, or write anything. Nightly volume goes
through the Batch API at 50% cost.

**[5] Aggregate on distinct customers.** 50,000 reports of one firmware
bug is one fact with 50,000 pieces of evidence. Counting mentions instead
of customers lets one enterprise account that calls forty times look like
forty corroborating sources — that is how an auto-update pipeline gets
gamed.

**[6] Observed ≠ confirmed.** The store keeps two distinct concepts:
what customers report (`observed`) and what engineering has verified
(`confirmed`). Customer chatter can never demote a confirmed row.
Contradictions and spec corrections route to humans by policy, not by
model judgement.

**[8] Provenance is not optional.** Every issue row traces to the calls,
segments, extractor version and run that produced it — including the
evidence behind candidates that were *rejected*, because "why did we not
act on this" is an audit question too.

**[9] Hybrid retrieval, and SQL alongside it.** "Does XG-482 firmware
7.2.13 have the route-loss bug?" turns on exact tokens; dense vectors put
`7.2.13` and `7.2.1` in nearly the same place. BM25 finds the identifier,
embeddings find the paraphrase, RRF fuses the rankings. Counting questions
bypass retrieval entirely and hit SQL.

## Local implementation vs production

| Concern | Here | Production |
|---|---|---|
| Compute | `ProcessPoolExecutor` over partitions | Spark / Ray, `mapPartitions` with the same stage functions |
| Lake | `data/lake/date=…/part-*.jsonl` | S3 + Delta/Iceberg, partitioned by date/region/centre |
| Transcription | text fixtures | batch ASR with diarization |
| Extraction | rules backend (offline) or Claude | Claude via Batch API, 50% cost |
| Router | lexical scorer | distilled classifier or embedding router |
| KB | SQLite | Postgres (state) + graph store (relations) |
| Index | in-process BM25 + hashed vectors | OpenSearch + a managed vector DB |
| Orchestration | one Python entry point | Airflow / Step Functions, per-stage retries |
| Streaming | n/a | Kafka + Structured Streaming reusing stages 2–6 unchanged |

The stage signatures are all `Iterable -> Iterator`, which is what makes
the Spark port mechanical rather than a rewrite.
