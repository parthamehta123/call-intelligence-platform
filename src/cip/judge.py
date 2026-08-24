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

# Sentences the knowledge base appends for provenance. They are bookkeeping,
# not topical content, and they measurably swing a small judge: the same
# query against the same document flipped from a correct "no" to a wrong
# "yes" once "Reported by 98 distinct customers across APAC, EU, LATAM, US.
# Severity high. Status observed." was appended. The judge is asked about
# the claim, not the audit trail.
_PROVENANCE = ("reported by", "severity ", "status ", "first seen", "last seen",
               "known aliases", "supported firmware versions")


def claim_view(title: str, body: str, limit: int = 400) -> str:
    """The document's substantive claim, with provenance clauses removed."""
    kept = [
        sentence.strip() for sentence in body.split(". ")
        if sentence.strip()
        and not sentence.strip().lower().startswith(_PROVENANCE)
    ]
    return f"{title}. {'. '.join(kept)}"[:limit]


PROMPT = """Question: {query}

Document: {document}

Is this document about the same topic as the question?

Answer yes if the document discusses what the question asks about, even
partially, and even if it uses different words.
Answer no only if the document is about a different topic.

Answer (yes or no):"""


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
    try:
        return _cached_verdict(CONFIG.judge, CONFIG.judge_model, query, document[:2000])
    except Exception as exc:  # pragma: no cover - depends on backend availability
        # Failing closed would turn a transient model error into a system
        # that answers nothing. Degrade to un-judged retrieval instead, and
        # say so rather than hiding it.
        print(f"[cip.judge] {type(exc).__name__}: {exc}; keeping document unjudged")
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
        system="You judge whether a document answers a question. Be strict: "
               "a document about the right product but the wrong topic does "
               "not answer the question.",
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        messages=[{"role": "user",
                   "content": PROMPT.format(query=query, document=document)}],
    )
    if response.stop_reason == "refusal":
        return True
    payload = json.loads(next(b.text for b in response.content if b.type == "text"))
    return bool(payload["answers_the_question"])


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
    content = PROMPT.format(query=query, document=document)
    text = _chat_prompt(tokenizer, content)
    inputs = tokenizer(text, return_tensors="pt")
    with torch.no_grad():
        logits = model(**inputs).logits[0, -1]

    def score(word: str) -> float:
        return max(float(logits[tokenizer.encode(variant, add_special_tokens=False)[0]])
                   for variant in (word, word.capitalize(), f" {word}"))

    return score("yes") > score("no")
