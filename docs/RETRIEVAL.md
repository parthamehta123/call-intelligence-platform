# Retrieval quality

```bash
make eval-retrieval
```

16 labelled queries against a 10-document corpus, 11 of them answerable.
Binary relevance. These are **diagnostics, not benchmark figures** — the
corpus is tiny and I wrote both the queries and the system. They are strong
enough to catch a regression and to settle an ablation; not to publish.

## Results

| mode | Recall@5 | MRR | nDCG@5 |
|---|---|---|---|
| hybrid | 0.758 | 0.727 | 0.703 |
| bm25 | 0.758 | 0.727 | 0.703 |
| dense | 0.758 | 0.727 | 0.703 |

| query type | Recall@5 |
|---|---|
| exact_identifier | 1.000 |
| product_doc | 1.000 |
| mixed | 0.667 |
| paraphrase | 0.600 |

```
query routing (SQL vs RAG)          16/16
abstention on unanswerable queries    1/2
```

## The hybrid claim, now measured with a real encoder

`CIP_EMBEDDER=sentence-transformers` runs `all-MiniLM-L6-v2` locally.
Against the hashed backend:

| backend | mode | Recall@5 | mixed | paraphrase |
|---|---|---|---|---|
| hashed | hybrid / bm25 / dense | 0.758 / 0.758 / 0.758 | 0.667 | 0.600 |
| MiniLM | hybrid / bm25 / dense | **0.788** / 0.758 / **0.788** | **0.833** / 0.667 | 0.600 |

With the hashed backend all three legs are identical, and the reason is the
implementation rather than the architecture: a hashed bag-of-words is
lexical matching wearing a different metric, so it cannot disagree with
BM25 about `7.2.13` — it represents that string as the same token.

With a real encoder the dense leg does something BM25 cannot. Asked for
*"customers asking for a way to download everything at once"*, MiniLM ranks
the bulk-export document **first**; BM25 returns four product overviews and
never finds it. That is the benefit the architecture always claimed.

**But the honest reading is narrower than "hybrid wins".** Hybrid and dense
score identically (0.788); BM25 contributes nothing the dense leg misses on
this query set, including on `exact_identifier`, where lexical matching was
supposed to be decisive. On a 10-document corpus every version string is
also a rare token, so BM25 has no chance to be uniquely right. The claim
that identifiers *need* lexical matching remains **unproven** — it needs a
corpus where near-miss identifiers actually collide.

## Two bugs the measurement found

**The tokenizer swallowed sentence-final punctuation.** `[a-z0-9][a-z0-9\.\-_]*`
allows dots so that `7.2.13` survives as one token — and greedily kept the
full stop too, so "runs abnormally hot." indexed as `hot.`, which never
matched a query's `hot`. **Every sentence-final word in the corpus was
unmatchable.** Fixing it moved Recall@5 from 0.576 to 0.758 and restored
`exact_identifier` to 1.000.

**`INDEX_PATH` was bound as a default argument**, so it was evaluated once
at import. Redirecting the index to a temporary path had no effect and
writes went to the real index — which is how the test that found it also
clobbered the live one. Both `save` and `load` now resolve at call time.

## Abstention is unsolved, and three mechanisms failed to solve it

Unanswerable questions must not draw a confident near-miss. Three separate
signals were measured, and **none separates answerable from unanswerable**
on this corpus.

**1 · Rerank score.** Overlapping:

```
answerable    0.542 – 1.228
unanswerable  0.568, 0.678     <- above three genuine answers
```

**2 · Semantic similarity** (real encoder). Also overlapping:

```
answerable best-relevant   0.886 … 0.235
unanswerable best-any      0.497, 0.570   <- above five genuine answers
```

**3 · Cross-encoder relevance** (`ms-marco-MiniLM-L-6-v2`, cached locally).
Worse than either — genuine answers score far *below* the unanswerable
ones:

```
answerable     8.460 … -11.179
unanswerable  -0.483, -1.470
```

It is trained on MS MARCO web passages; these documents are terse generated
summaries ("Bulk CSV export requested. Reported by 105 distinct
customers…"), and the domain mismatch makes its ordering unusable here.

**Why all three fail is the same reason.** The unanswerable queries are
*topically adjacent*: the Pulse 7 is a real product and warranty is a real
topic; the X100 is a real router and multicast is real networking. What
makes them unanswerable is that the specific attribute asked about is
absent from the corpus — a claim-level judgement, not a similarity one. No
amount of threshold tuning turns a similarity score into that judgement.

**4 · An LLM judge** — asking directly whether the document answers the
question — is the only mechanism that works, and it is now built
(`src/cip/judge.py`, `CIP_JUDGE=claude|local`). It runs last, on the ranked
shortlist only, so cost is bounded at top_k per query, and it can only
remove documents: it never reorders and never adds.

Measured with `Qwen2.5-1.5B-Instruct` running locally:

| prompt | Recall@5 | abstention |
|---|---|---|
| judge off | 0.758 | 1/2 |
| leaning to "no" | **0.091** | **2/2** |
| balanced | 0.758 | 1/2 |

The first prompt told the model "a document about the right product but the
wrong topic does NOT answer it". It achieved perfect abstention by
rejecting nearly everything — 90% of correct answers with it.

The second is balanced, costs no recall, and is genuinely discriminating on
a direct probe (5/6, rejecting both warranty questions while keeping all
three real answers). It still misses the IPv6 case, where "do you support
IPv6" against an overview listing "supported firmware versions" is a
defensible yes for a small model.

All four combinations, measured after `hf auth logout` removed the invalid
stored token (these models are public; an *invalid* token is worse than
none, because the hub 401s where anonymous access succeeds):

| embedder | judge | Recall@5 | abstention |
|---|---|---|---|
| hashed | off | 0.758 | 1/2 |
| hashed | local 1.5B | 0.758 | 1/2 |
| MiniLM | off | **0.788** | 0/2 |
| MiniLM | local 1.5B | **0.788** | 0/2 |

The judge is verified as engaged rather than failing open — mixed verdicts,
5/6 on a direct probe. It costs no recall in any configuration. **It also
does not rescue abstention**: with the real encoder, semantic admission
lets the product's documents through and the judge does not veto them.

### A third sensitivity: document boilerplate

The same query against the same document flips verdict on metadata alone:

```
"PULSE7 Overheating. Device runs abnormally hot on 1.9."          -> no  (correct)
"...Reported by 98 distinct customers across APAC, EU, LATAM,
   US on versions 1.9. Severity high. Status observed..."         -> yes (wrong)
```

Provenance clauses are bookkeeping, not topical content, so `claim_view()`
strips them before judging. That removed the wrong-topic *issue* document
from the warranty query's citations — but the product *overview* still
passes, which a small model can defend: an overview genuinely is "about"
the product.

So the judge now demonstrates three independent sensitivities — prompt
lean, document boilerplate, and document type — each of which moved its
verdicts. That is the case for a stronger judge, not for further tuning of
this one.

**The finding is the sensitivity, not the score.** A rewording moved
Recall@5 from 0.091 to 0.758. A 1.5B model largely follows the prompt's
lean rather than judging the document, which is precisely the argument for
a stronger judge. The `claude-opus-5` path is written with
schema-constrained output and **is not measured here** — this environment
has no Anthropic credentials, so treat it as untested code.

### The tradeoff that is actually shipped

`topical_coverage()` — do any of the query's *discriminating* terms appear
in the document, ignoring product names and corpus-wide boilerplate — takes
abstention from 0/2 to 1/2. But it is a lexical gate on top of semantic
retrieval, and it cancels the encoder: it rejected the bulk-export document
MiniLM had ranked first, because it shares no literal term with "download
everything at once". With coverage as the only admission rule, the real
encoder's advantage disappears entirely and all three legs return to 0.758.

So admission is `coverage > 0 OR similarity >= CIP_DENSE_FLOOR` (default
0.35), and the cost is stated rather than hidden:

| setting | Recall@5 | abstention |
|---|---|---|
| `CIP_DENSE_FLOOR=0.35` (default) | 0.788 | 0/2 |
| `CIP_DENSE_FLOOR=1.01` (coverage only) | 0.758 | 1/2 |

Coverage only ever fixed one of the two cases, and it costs the entire
semantic benefit — so the default keeps retrieval working and leaves
abstention documented as unsolved rather than half-mitigated by something
that cripples the encoder.

### Historic note: why a score threshold was tried first

```
answerable queries scored   0.542 – 1.228
unanswerable queries scored 0.568, 0.678
```

The distributions overlap, and unanswerable questions outscored three
genuine answers. The reason is structural — the score is carried by the
product name, which both share.

`topical_coverage()` asks a different question: do any of the query's
*discriminating* terms appear in the document? Product names are excluded
(they are what makes an irrelevant document look close), as are terms in
more than half the corpus (`customers` appears in every issue document).
Zero coverage means abstain.

That takes abstention from 0/2 to 1/2. The survivor is honest about the
method's limit:

> *"do you support IPv6 multicast routing on the X100"* → cites the X100
> overview, because **"support"** stems onto **"supported firmware
> versions"**.

A real token match with no semantic relation. Lexical coverage cannot
distinguish that from a genuine hit, and tuning the threshold on 16
self-authored queries would be fitting the test, not fixing the problem.
Robust abstention needs a cross-encoder or an LLM judge over the retrieved
context — the same conclusion the paraphrase gap reaches.

## What is still unmeasured

**Groundedness of generated answers.** The offline agent's answer *is* the
retrieved document text, so groundedness is 1.0 by construction and
measuring it would be theatre. It becomes a real question with the Claude
backend, and needs an LLM judge or human labels.

**Citation correctness beyond existence.** Every citation returned is a
document that was actually retrieved, but nothing checks that the cited
document *supports* the specific claim.

**Scale.** 10 documents. Ranking behaviour at 10 documents says very little
about ranking at 100,000.
