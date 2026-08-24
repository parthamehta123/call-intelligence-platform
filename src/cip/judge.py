"""LLM judge for abstention.

Three similarity signals were measured and none separates answerable from
unanswerable questions (`docs/RETRIEVAL.md`): rerank score, dense
similarity, and a cross-encoder all overlap. They fail for one reason --
the unanswerable queries are *topically adjacent*. The Pulse 7 is a real
product and warranty is a real topic; the X100 is a real router and
multicast is real networking. What makes them unanswerable is that the
specific attribute asked about is absent, and that is a claim-level
judgement no similarity score encodes.

So ask the question directly: **does this document answer this query?**

Backends:

* ``none``   -- default. No judge, no dependencies, deterministic tests.
* ``claude`` -- ``claude-opus-5`` with schema-constrained output. The
  production path.
* ``local``  -- a small cached instruct model, scored by comparing the
  logits of "yes" and "no" at a single forward pass. Deterministic, needs
  no API key, and exists so the mechanism can be *measured* here rather
  than shipped on faith.

The judge only ever removes documents. It cannot invent a citation, and a
judge that fails open (errors -> keep the document) degrades to the
un-judged behaviour rather than to silence.
"""

from __future__ import annotations

from functools import lru_cache

from .config import CONFIG

# Fail-open is the right behaviour -- a model outage must degrade to
# un-judged retrieval, not to a system that answers nothing. But it means a
# completely dead backend still returns a full set of results, and an eval
# will then print a clean table that is really the un-judged baseline.
# That has now happened twice: a stale hub token, and an unfunded API key.
# Callers can read these counters to refuse to report such a run as a
# measurement.
STATS = {"calls": 0, "failures": 0}


def reset_stats() -> None:
    STATS.update(calls=0, failures=0)
    REJECTIONS.clear()

# Sentences the knowledge base appends for provenance. They are bookkeeping,
# not topical content, and they measurably swing a small judge: the same
# query against the same document flipped from a correct "no" to a wrong
# "yes" once "Reported by 98 distinct customers across APAC, EU, LATAM, US.
# Severity high. Status observed." was appended. The judge is asked about
# the claim, not the audit trail.
# Strictly the audit trail. "Supported firmware versions: 3.4, 3.5" and the
# alias list were on this list once, and both are document *content*: a
# product overview exists to state exactly those things. Stripping them made
# the judge reject overviews for questions they answered, and drove
# product_doc recall to 0.000 in the claude-opus-5 run. The judge was right
# about what it was shown; it was shown the wrong thing.
_PROVENANCE = ("reported by", "severity ", "status ", "first seen", "last seen")


def claim_view(title: str, body: str, limit: int = 400) -> str:
    """The document's substantive claim, with provenance clauses removed."""
    kept = [
        sentence.strip() for sentence in body.split(". ")
        if sentence.strip()
        and not sentence.strip().lower().startswith(_PROVENANCE)
    ]
    return f"{title}. {'. '.join(kept)}"[:limit]


# Two prompts, because the backends need different things and sharing one
# produced an incoherent request: the Claude path was given a strict
# "does it answer" system prompt, a lenient "same topic, even partially"
# question, and a "reply yes or no" tail while being forced to emit JSON.
# Rejecting a product overview that literally lists the firmware versions a
# question asked for is the kind of result that contradiction produces.

LOCAL_PROMPT = """Question: {query}

Document: {document}

Is this document about the same topic as the question?

Answer yes if the document discusses what the question asks about, even
partially, and even if it uses different words.
Answer no only if the document is about a different topic.

Answer (yes or no):"""

CLAUDE_PROMPT = """You are filtering search results, not grading answers.

Question: {query}

Document: {document}

Is this document about the subject the question asks about?

Say yes if the document concerns that subject, even when it is incomplete.
A document that only states a symptom is still about that symptom. A
document that names an issue without giving its cause, status or fix is
still about that issue. A product overview that lists firmware versions is
still about that product's firmware. Partial, terse and stub documents all
count as yes.

Say no only when the document is about a DIFFERENT subject: a different
product, or a different attribute of the same product. A document about
overheating is not about warranty length. A document about static routes is
not about report exports.

The consequence of no is that the reader is shown nothing at all, so
reserve it for documents that genuinely would not help.

Give a one-sentence reason naming the subject of the document.
"""

# Kept for backwards compatibility with callers that imported PROMPT.
PROMPT = LOCAL_PROMPT

# Rejections, with the judge's stated reason. An eval that only prints a
# recall number cannot show *why* documents were dropped, which is the
# information needed to tell a strict judge from a broken prompt.
REJECTIONS: list[dict] = []


@lru_cache(maxsize=4096)
def _cached_verdict(backend: str, model: str, query: str, document: str) -> bool:
    if backend == "claude":
        return _judge_claude(query, document, model)
    if backend == "local":
        return _judge_local(query, document, model)
    raise ValueError(f"unknown judge backend {backend!r}")


def judge(query: str, document: str) -> bool:
    """True if the document answers the query. Fails open."""
    if CONFIG.judge == "none":
        return True
    STATS["calls"] += 1
    try:
        return _cached_verdict(CONFIG.judge, CONFIG.judge_model, query, document[:2000])
    except Exception as exc:  # pragma: no cover - depends on backend availability
        # Failing closed would turn a transient model error into a system
        # that answers nothing. Degrade to un-judged retrieval instead, and
        # say so rather than hiding it.
        STATS["failures"] += 1
        if STATS["failures"] == 1:
            print(f"[cip.judge] {type(exc).__name__}: {exc}; keeping document "
                  f"unjudged (further failures counted, not repeated)")
        return True


# --- Claude -----------------------------------------------------------------
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "answers_the_question": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["answers_the_question", "reason"],
    "additionalProperties": False,
}


def _judge_claude(query: str, document: str, model: str) -> bool:
    import json

    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system="You judge whether a document answers a question. Judge the "
               "content, not the document's genre: an overview, a release "
               "note and an issue report are all capable of answering.",
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        messages=[{"role": "user",
                   "content": CLAUDE_PROMPT.format(query=query, document=document)}],
    )
    if response.stop_reason == "refusal":
        return True
    raw = next(b.text for b in response.content if b.type == "text")
    payload = json.loads(raw)
    verdict = bool(payload["answers_the_question"])

    from .security.audit import trace_model_call
    trace_model_call(component="judge", model=model,
                     prompt=CLAUDE_PROMPT.format(query=query, document=document),
                     response=raw, verdict=verdict)
    if not verdict:
        REJECTIONS.append({"query": query, "document": document[:70],
                           "reason": payload.get("reason", "")})
    return verdict


# --- local ------------------------------------------------------------------
def _cached_snapshot(model_name: str) -> str | None:
    """Path to a fully downloaded snapshot, or None.

    Loading by repo id resolves through the hub even with
    `local_files_only=True` -- Qwen's tokenizer config triggers a lookup for
    `additional_chat_templates`, which 401s against a stale token and errors
    under `HF_HUB_OFFLINE`. Pointing at the directory skips resolution
    entirely. The model loaded by id; only the tokenizer did not, which is
    why this failed as a silent fail-open rather than an obvious crash.
    """
    import glob
    import os

    cache = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    hub = os.path.join(cache, "hub") if not cache.endswith("hub") else cache
    pattern = os.path.join(hub, "models--" + model_name.replace("/", "--"),
                           "snapshots", "*")
    snapshots = [d for d in sorted(glob.glob(pattern)) if os.path.isdir(d)]
    return snapshots[-1] if snapshots else None


@lru_cache(maxsize=2)
def _load_local(model_name: str):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    source = _cached_snapshot(model_name) or model_name
    local_only = source != model_name
    tokenizer = AutoTokenizer.from_pretrained(source, local_files_only=local_only)
    model = AutoModelForCausalLM.from_pretrained(
        source, dtype=torch.float32, local_files_only=local_only)
    model.eval()
    return tokenizer, model


def _chat_prompt(tokenizer, content: str) -> str:
    """Render the chat prompt without reaching the network.

    `apply_chat_template` fetches `additional_chat_templates` from the hub
    on every call. With a stale token that is a 401; with HF_HUB_OFFLINE it
    is an offline error. Either way the judge failed open on every document
    and reported verdicts that were really just defaults. ChatML is the
    format Qwen and most small instruct models use, so the fallback is a
    correct prompt rather than a degraded one.
    """
    try:
        return tokenizer.apply_chat_template(
            [{"role": "user", "content": content}],
            tokenize=False, add_generation_prompt=True)
    except Exception:
        return (f"<|im_start|>user\n{content}<|im_end|>\n"
                f"<|im_start|>assistant\n")


def _judge_local(query: str, document: str, model_name: str) -> bool:
    """One forward pass, comparing P(yes) against P(no).

    Generating and parsing free text would need a decode loop and a parser
    for every way a small model can say yes. Reading the two logits is
    deterministic, needs no parsing, and is a single pass.
    """
    import torch

    tokenizer, model = _load_local(model_name)
    content = LOCAL_PROMPT.format(query=query, document=document)
    text = _chat_prompt(tokenizer, content)
    inputs = tokenizer(text, return_tensors="pt")
    with torch.no_grad():
        logits = model(**inputs).logits[0, -1]

    def score(word: str) -> float:
        return max(float(logits[tokenizer.encode(variant, add_special_tokens=False)[0]])
                   for variant in (word, word.capitalize(), f" {word}"))

    return score("yes") > score("no")
