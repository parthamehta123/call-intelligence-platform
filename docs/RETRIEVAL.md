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

## The hybrid claim is not supported by this measurement

The README has said, repeatedly, that hybrid retrieval beats either leg
because product IDs and versions need exact lexical matching. **The
ablation shows all three modes performing identically.**

The reason is in the implementation, not the architecture: `embed()` is a
hashed bag-of-words, so the "dense" leg is lexical matching with a
different metric. It cannot disagree with BM25 about `7.2.13` because it
represents `7.2.13` as the same token BM25 does. Fusing two legs that make
the same mistakes buys nothing.

So the honest position: the ablation harness is correct and wired up, and
it currently reports **no evidence for the hybrid claim**. That claim
becomes testable the moment `embed()` is swapped for a real encoder — at
which point `exact_identifier` is the row to watch, because that is where a
real semantic model is expected to fail and BM25 to carry it.

Until then, describing this system as "hybrid retrieval" is a description
of the wiring, not a demonstrated benefit.

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

## Abstention

Unanswerable questions must not draw a confident near-miss. A score
threshold cannot do this job, and the measurement says so plainly:

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
