# Target architecture → implementation

A box-by-box audit of the reference architecture against what is actually
in this repo. Every "Built" below was checked against the code, not
remembered. Anything a reader could reasonably assume works and does not
is marked plainly.

**Built 26 · Partial 9 · Not built 5**

## Ingestion and preprocessing

| Diagram box | Status | Where |
|---|---|---|
| Raw data lake, partitioned | **Partial** — `date=` only; region / call-centre partitioning is described but not implemented | `generate.py`, `pipeline/ingest.py`; a UC volume on Databricks |
| Transcription (audio → text) | **Not built** — fixtures are text | — |
| Speaker diarization | **Partial** — no audio, so labels come from the generator; the *contract* and its error handling are built and measured | `docs/ATTRIBUTION.md`, `src/cip/eval/attribution_eval.py` |
| Normalization / dedup | **Built** — content-hash dedupe, global shuffle on Spark | `pipeline/preprocess.py`, `spark/dedupe.py` |
| Language detection | **Not built** | — |
| PII masking | **Built** — redacted at the boundary, before persistence | `security/dlp.py`, applied in `preprocess.py` |

## Routing, chunking, resolution

| Diagram box | Status | Where |
|---|---|---|
| Relevance filter / router | **Built** — discards ~70% before inference; interface is `score → [0,1]` so a classifier drops in | `pipeline/route.py` |
| Topic / semantic-aware chunking | **Partial** — speaker turns + lexical topic markers + size cap, preserving `call_id`/speaker/timestamps. Not a semantic model | `pipeline/preprocess.py` |
| Product / entity resolution | **Built** — aliases, SKU, version, family, canonical ID, plus a CRM product hint for calls that never name the product | `catalog.py` |

## Extraction under untrusted input

| Diagram box | Status | Where |
|---|---|---|
| Untrusted content marked at source | **Built** — every segment stamped untrusted where it is created | `schemas.py`, `security/taint.py` |
| Extraction agent, structured output only | **Built** — JSON-schema-constrained, **no tools at all**; offline rules backend and a real Claude backend (`output_config.format`, Batch API at 50% cost) | `pipeline/extract.py` |

## Agent security boundary

| Diagram box | Status | Where |
|---|---|---|
| Prompt guard | **Built** — deliberately a *signal*, not the containment | `security/prompt_guard.py` |
| Tool / exec guard | **Built** — allowlist, argument schemas, destructive-op refusal | `security/policy.py`, `tools.py` |
| Data / DLP guard | **Built** — PII inbound, secrets blocked outbound | `security/dlp.py` |
| Taint / provenance tracking | **Built** — propagating labels + one audited declassification gate | `security/taint.py`, `security/declassify.py` |
| Policy engine | **Built** — RBAC, action, contextual, destination, confidence thresholds; **no LLM in the decision path** | `security/policy.py` |
| Allow / deny → block, log, alert, human review | **Built** | `security/audit.py`, `review_queue` |
| Trusted writer, narrow API, no arbitrary shell | **Built** — there is no `shell`, no `execute_sql`, no `write_file` anywhere | `tools.py` |
| Sandbox / isolation for privileged execution | **Not built** — design guidance only | `docs/SECURITY.md` |
| Egress control | **Partial** — allowlist + SSRF guard enforced in-process; no proxy or firewall | `security/egress.py` |

## Validation and canonical state

| Diagram box | Status | Where |
|---|---|---|
| Schema validation, dedup, aggregation | **Built** — aggregates on *distinct customers*, not mentions | `schemas.py`, `pipeline/aggregate.py` |
| Conflict detection, confidence scoring | **Built** | `pipeline/reconcile.py` |
| Customer observation ≠ official truth | **Built** — `observed` vs `confirmed`; chatter never demotes a confirmed row | `kb.py`, `spark/publish.py` |
| Auto-approve vs supervisor / human review | **Built** — contradictions and spec corrections always route to a human | `pipeline/reconcile.py` |
| Canonical product state | **Built** — SQLite locally, Delta on Unity Catalog | `kb.py`, `spark/ddl.py` |
| Knowledge graph | **Not built** — relational only | — |
| Immutable evidence store | **Built** — append-only, full provenance incl. rejected candidates | `kb.py`, `spark/publish.py` |
| Change event / CDC | **Built** — dirty flags drive incremental re-embedding | `kb.py`, `retrieval.refresh_index` |

## Retrieval and serving

| Diagram box | Status | Where |
|---|---|---|
| RAG indexing over **approved** knowledge only | **Built** — the index is built from validated state, never from transcripts | `retrieval.py` |
| Embeddings | **Partial** — hashed bag-of-words stand-in; swap `embed()` for a real encoder | `retrieval.py` |
| Vector index | **Partial** — in-process, not Pinecone/pgvector | `retrieval.py` |
| Lexical / BM25 index | **Partial** — in-process, not OpenSearch | `retrieval.py` |
| Query router: structured → SQL, document → RAG | **Built** | `agent.py` |
| Structured retrieval | **Built** — SQL over canonical state | `agent.py`, `tools.py` |
| Hybrid retrieval: metadata filter + vector + BM25 + RRF | **Built** | `retrieval.hybrid_search` |
| Reranker | **Partial** — heuristic overlap + status prior, not a cross-encoder | `retrieval._rerank_entry` |
| Hybrid retrieval **benefit** | **Unproven** — ablation wired up and reports all three legs identical, because `embed()` is a hashed bag-of-words | `docs/RETRIEVAL.md` |
| Grounded agent: citations, abstain on weak evidence | **Built** — both routes abstain rather than fill from memory | `agent.py` |

## Observability

| Diagram box | Status | Where |
|---|---|---|
| Pipeline metrics | **Built** | `run_metrics` table |
| Policy decisions | **Built** — every allow/deny queryable | `policy_audit` table, `spark/audit_sink.py` |
| Security events | **Built** | `security/audit.py` |
| Lineage / provenance | **Built** | `evidence` table |
| Prompt / tool traces | **Partial** — tool calls and decisions audited; no prompt-level tracing | `security/audit.py` |
| Router quality (precision / recall / threshold sweep) | **Built** — measured on two labelled sets, with a CI regression gate | `src/cip/eval/`, `docs/EVAL.md` |
| Retrieval quality (Recall@K, MRR, nDCG, routing, abstention) | **Built** — labelled query set, leg ablation, CI floors | `src/cip/eval/retrieval_eval.py`, `docs/RETRIEVAL.md` |
| Groundedness of generated answers | **Not built** — trivially 1.0 for the offline agent; needs an LLM judge with the Claude backend | `docs/RETRIEVAL.md` |

## The six that are genuinely missing

Ranked by how much they matter to the architecture's claims:

1. **A real embedding model.** Retrieval is now measured
   (`docs/RETRIEVAL.md`) and the ablation reports hybrid, BM25 and dense as
   *identical* — the hashed bag-of-words `embed()` cannot disagree with
   BM25, so the repeatedly-stated hybrid benefit has no supporting
   evidence. This is now the highest-value change: it converts a claim into
   something measurable. The *router* is separately measured
   (`docs/EVAL.md`): precision 0.977 / recall 1.000 on 4,000 generated segments
   (a saturated set), 0.733 / 0.917 on 32 hand-written hard cases, with a
   CI floor.
   Retrieval quality — Recall@K, nDCG, groundedness, citation correctness
   — is still unmeasured, and 32 hand-labelled cases is a smoke test
   rather than a benchmark.
2. **Transcription.** Still no audio path. Diarization is now handled as a
   contract rather than assumed away — agent speech can never become a
   customer observation, weak attribution scales confidence, and
   corroboration counts only well-attributed claims (`docs/ATTRIBUTION.md`,
   0.95% measured contamination). Real ASR and a real diarizer remain
   unbuilt.
3. **Sandbox isolation.** The security story ends at "the agent holds no
   dangerous tools". It does not cover the case where privileged execution
   is genuinely required.
4. **Real embeddings and a real reranker.** Both are labelled stand-ins,
   and both flatter the retrieval numbers.
5. **Knowledge graph.** Relational state answers the current questions;
   product→feature→issue traversal would need the graph.
6. **Language detection.** A multilingual call centre would silently feed
   the extractor text it cannot reason about.
