# Target architecture → implementation

A box-by-box audit of the reference architecture against what is actually
in this repo. Every "Built" below was checked against the code, not
remembered. Anything a reader could reasonably assume works and does not
is marked plainly.

**Built 47 · Partial 0 · Not built 0**

## Ingestion and preprocessing

| Diagram box | Status | Where |
|---|---|---|
| Raw data lake, partitioned | **Built** — `date=/region=/centre=`, pruned by path: 96 partitions → 24 for one region → 8 for one centre | `generate.py`, `pipeline/ingest.py` |
| Transcription (audio → text) | **Built** — faster-whisper `large-v3` on real synthesised audio; WER 0.089, and that is formatting ("seven point two" → "7.2") | `src/cip/audio.py`, `docs/AUDIO.md` |
| Speaker diarization | **Built** — dual-channel (the contact-centre standard) at 1.000; mono clustering fallback measured at 0.667 and documented as the weaker path | `src/cip/audio.py`, `docs/AUDIO.md` |
| Normalization / dedup | **Built** — content-hash dedupe, global shuffle on Spark | `pipeline/preprocess.py`, `spark/dedupe.py` |
| Language detection | **Built** — from the ASR pass, where the acoustic evidence is; `en` at p=0.97 | `src/cip/audio.py` |
| PII masking | **Built** — redacted at the boundary, before persistence | `security/dlp.py`, applied in `preprocess.py` |

## Routing, chunking, resolution

| Diagram box | Status | Where |
|---|---|---|
| Relevance filter / router | **Built** — discards ~70% before inference; interface is `score → [0,1]` so a classifier drops in | `pipeline/route.py` |
| Topic / semantic-aware chunking | **Built** — embedding similarity between adjacent customer turns, alongside the lexical markers; engages only with a real encoder, since hashed similarity would split on vocabulary | `pipeline/preprocess.py` |
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
| Sandbox / isolation for privileged execution | **Built** — scrubbed environment, disposable cwd jail, rlimits and a wall-clock kill; enforceable limits probed per platform rather than assumed (macOS ignores `RLIMIT_AS`) | `src/cip/security/sandbox.py` |
| Egress control | **Built** — enforced beneath the socket layer, so `requests`, `urllib` and raw sockets are all covered; hostname recovered through DNS, raw-IP and DNS-rebinding bypasses closed | `src/cip/security/netguard.py` |

## Validation and canonical state

| Diagram box | Status | Where |
|---|---|---|
| Schema validation, dedup, aggregation | **Built** — aggregates on *distinct customers*, not mentions | `schemas.py`, `pipeline/aggregate.py` |
| Conflict detection, confidence scoring | **Built** | `pipeline/reconcile.py` |
| Customer observation ≠ official truth | **Built** — `observed` vs `confirmed`; chatter never demotes a confirmed row | `kb.py`, `spark/publish.py` |
| Auto-approve vs supervisor / human review | **Built** — contradictions and spec corrections always route to a human | `pipeline/reconcile.py` |
| Canonical product state | **Built** — SQLite locally, Delta on Unity Catalog | `kb.py`, `spark/ddl.py` |
| Knowledge graph | **Built** — networkx over the same canonical state (derived, not a second store); shared failure modes, blast radius, issue-to-issue paths | `src/cip/graph.py` |
| Immutable evidence store | **Built** — append-only, full provenance incl. rejected candidates | `kb.py`, `spark/publish.py` |
| Change event / CDC | **Built** — dirty flags drive incremental re-embedding | `kb.py`, `retrieval.refresh_index` |

## Retrieval and serving

| Diagram box | Status | Where |
|---|---|---|
| RAG indexing over **approved** knowledge only | **Built** — the index is built from validated state, never from transcripts | `retrieval.py` |
| Embeddings | **Built** — pluggable backends; `sentence-transformers/all-MiniLM-L6-v2` runs locally, hashed remains the dependency-free default | `src/cip/embedding.py`, `docs/RETRIEVAL.md` |
| Vector index | **Built** — FAISS `IndexFlatIP` over normalised vectors; exact at this size, IVF/HNSW at scale without changing the call site | `src/cip/index_backends.py` |
| Lexical / BM25 index | **Built** — inverted index with postings, so a query visits only documents containing its terms rather than scanning the corpus | `src/cip/index_backends.py` |
| Query router: structured → SQL, document → RAG | **Built** | `agent.py` |
| Structured retrieval | **Built** — SQL over canonical state | `agent.py`, `tools.py` |
| Hybrid retrieval: metadata filter + vector + BM25 + RRF | **Built** | `retrieval.hybrid_search` |
| Reranker | **Built** — cross-encoder (`ms-marco-MiniLM-L-6-v2`); MRR 0.682 → 0.773, nDCG 0.669 → 0.736, recall unchanged since it only reorders | `src/cip/rerank.py` |
| Hybrid retrieval **benefit** | **Built** — proven on a near-miss identifier corpus: dense 0.667, BM25 1.000, equal-weight fusion 0.833, identifier-weighted fusion 1.000 | `src/cip/eval/identifier_eval.py`, `docs/RETRIEVAL.md` |
| Grounded agent: citations, abstain on weak evidence | **Built** — both routes abstain rather than fill from memory | `agent.py` |

## Observability

| Diagram box | Status | Where |
|---|---|---|
| Pipeline metrics | **Built** | `run_metrics` table |
| Policy decisions | **Built** — every allow/deny queryable | `policy_audit` table, `spark/audit_sink.py` |
| Security events | **Built** | `security/audit.py` |
| Lineage / provenance | **Built** | `evidence` table |
| Prompt / tool traces | **Built** — every model call recorded verbatim with its response, DLP-redacted first so a trace cannot leak the PII the pipeline removed | `security/audit.py` |
| Router quality (precision / recall / threshold sweep) | **Built** — measured on two labelled sets, with a CI regression gate | `src/cip/eval/`, `docs/EVAL.md` |
| Retrieval quality (Recall@K, MRR, nDCG, routing, abstention) | **Built** — labelled query set, leg ablation, CI floors | `src/cip/eval/retrieval_eval.py`, `docs/RETRIEVAL.md` |
| Groundedness of generated answers | **Built** — per-sentence support against cited documents, plus a fabricated-citation count; 1.000 on the offline agent, which confirms the plumbing rather than testing generation | `src/cip/eval/groundedness_eval.py` |
| Abstention judge | **Built** — `claude-opus-5` framed as a retrieval filter achieves 2/2 abstention where rerank score, similarity, a cross-encoder and a 1.5B judge all failed; measured cost is one document the labels had wrong | `src/cip/judge.py`, `docs/RETRIEVAL.md` |

## What is still true about the limits

Nothing in the table above is unimplemented. These are the boundaries of
what could be *shown* in this environment, which is a different claim from
what was built — and they are listed rather than folded into the coverage
count.

1. **Scale.** Every figure comes from 4,000 synthetic calls and a
   10-document corpus. The architecture is shaped for 10 TB/day — stage
   functions are `Iterable -> Iterator`, the lake prunes by partition, both
   indexes are real structures rather than scans — but no run at that
   volume has happened, and ranking behaviour over ten documents says
   little about ranking over a hundred thousand.

2. **The labels are still mine, but no longer have to be.** The harness for
   somebody else to produce them is built (`docs/LABELLING.md`): stratified
   sampling toward the decision boundary, the model's score withheld from
   the annotator, Cohen's kappa over double-labelled items, and contested
   items excluded from the export rather than settled by vote. What it
   cannot do is supply a second annotator — until one sits down with it,
   every figure here still rests on one person's judgement.

3. **Synthetic input throughout.** The calls are generated and the audio is
   text-to-speech, so there is no crosstalk, no accents, no background
   noise and no overtalk — which is precisely what makes ASR and
   diarization hard in a real contact centre.

4. **Platform gaps, reported rather than papered over.** macOS ignores
   `RLIMIT_AS`, so the sandbox cannot cap memory here; that needs Linux or a
   container. `enforceable_limits()` says which limits apply at runtime
   instead of implying all of them do.

5. **The Claude extractor has now run, capped at 50 segments** — see
   `docs/EXTRACTION.md`. It agrees with the rules extractor 15/15 on
   product and type, 14/15 on severity, and required a controlled
   vocabulary for `issue_key` before it agreed at all. An uncapped day was
   attempted and stopped on an exhausted credit balance, so every headline
   figure in this repo still comes from the rules extractor and the
   comparison rests on 15 segments.
