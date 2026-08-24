"""Is every claim in an answer supported by something that was cited?

The failure this catches is the one RAG is supposed to prevent and often
does not: an answer that reads as though it came from the documents,
containing a sentence that came from the model.

Scored per sentence. A sentence is grounded when some cited document
supports it -- by embedding similarity where a real encoder is configured,
by content-word overlap otherwise, since hashed similarity would just be
overlap measured twice.

Honest reading for the offline agent: its answer *is* the retrieved
document text, so groundedness is 1.0 by construction and the number
confirms the plumbing rather than testing a model. The metric earns its
keep against a generative backend, where a sentence can be invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import CONFIG


@dataclass
class GroundednessReport:
    answers: int = 0
    sentences: int = 0
    grounded: int = 0
    citations: int = 0
    fabricated_citations: int = 0
    ungrounded: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.grounded / self.sentences if self.sentences else 1.0

    def render(self) -> str:
        lines = [
            "=== groundedness ===",
            f"  answers scored           {self.answers}",
            f"  sentences                {self.sentences}",
            f"  grounded in a citation   {self.grounded} ({self.score:.3f})",
            f"  citations returned       {self.citations}",
            f"  citations not retrieved  {self.fabricated_citations}",
        ]
        if self.ungrounded:
            lines.append("  unsupported sentences:")
            lines += [f"    {s[:96]}" for s in self.ungrounded[:5]]
        return "\n".join(lines)


_STOP = {"the", "a", "an", "is", "are", "of", "and", "to", "in", "for", "on",
         "with", "it", "this", "that", "was", "were", "be", "by", "from", "as",
         "at", "or", "has", "have", "no", "not"}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9.\-]*", text.lower())
            if w not in _STOP and len(w) > 1}


def supported(sentence: str, sources: list[str]) -> bool:
    if not sentence.strip() or not sources:
        return False
    if CONFIG.embedder != "hashed":
        try:
            from ..retrieval import cosine, embed_many

            vectors = embed_many([sentence] + sources)
            return any(cosine(vectors[0], v) >= 0.5 for v in vectors[1:])
        except Exception:
            pass
    words = _content_words(sentence)
    if not words:
        return True
    return any(len(words & _content_words(source)) / len(words) >= 0.6
               for source in sources)


def evaluate_groundedness() -> GroundednessReport:
    from ..agent import ask
    from ..retrieval import Index
    from .retrieval_eval import load_cases

    index = Index.load()
    report = GroundednessReport()

    for case in load_cases():
        if case.route != "rag":
            continue
        answer = ask(case.query)
        if not answer.citations:
            continue
        report.answers += 1

        sources = []
        for citation in answer.citations:
            doc_id = citation.get("doc_id")
            report.citations += 1
            document = index.docs.get(doc_id)
            if document is None:
                # A cited document that was never retrieved is the worst
                # case: the answer looks sourced and is not.
                report.fabricated_citations += 1
                continue
            sources.append(f"{document.title}. {document.body}")

        for sentence in re.split(r"(?<=[.!?])\s+", answer.answer):
            if len(sentence.strip()) < 12:
                continue
            report.sentences += 1
            if supported(sentence, sources):
                report.grounded += 1
            else:
                report.ungrounded.append(sentence.strip())
    return report
