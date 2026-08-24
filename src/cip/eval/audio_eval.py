"""Does the audio path recover who said what?

Everything downstream of attribution -- extraction, corroboration, the
distinct-customer count that decides publication -- assumes the speaker
label is right. With text fixtures that was an assumption. With real audio
it is measurable, so it is measured.

Reported:
  transcription WER      against the text that was spoken
  language accuracy      detected language vs the language synthesised
  diarization accuracy   speaker per segment vs who actually spoke
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..audio import (attribute, diarize, diarize_stereo, synthesize_call,
                     transcribe)


@dataclass
class AudioReport:
    segments: int = 0
    wer: float = 0.0
    language: str = ""
    language_probability: float = 0.0
    diarization_correct: int = 0
    diarization_total: int = 0
    method: str = ""
    mismatches: list[str] = field(default_factory=list)

    @property
    def diarization_accuracy(self) -> float:
        return (self.diarization_correct / self.diarization_total
                if self.diarization_total else 0.0)

    def render(self) -> str:
        lines = [
            "=== audio ingestion ===",
            f"  transcript segments      {self.segments}",
            f"  word error rate          {self.wer:.3f}",
            f"  language detected        {self.language} "
            f"(p={self.language_probability:.2f})",
            f"  diarization ({self.method})".ljust(28)
            + f" {self.diarization_accuracy:.3f} "
            f"({self.diarization_correct}/{self.diarization_total} segments)",
        ]
        if self.mismatches:
            lines.append("  misattributed:")
            lines += [f"    {m}" for m in self.mismatches[:5]]
        return "\n".join(lines)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein over words. ASR output differs from the prompt in
    formatting -- "seven point two" becomes "7.2" -- so a raw string
    comparison would report failure where the transcript is correct."""
    ref, hyp = _words(reference), _words(hypothesis)
    if not ref:
        return 0.0
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        current = [i]
        for j, h in enumerate(hyp, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (r != h)))
        previous = current
    return previous[-1] / len(ref)


DEMO_TURNS = [
    {"speaker": "agent", "text": "Thanks for calling support, how can I help?"},
    {"speaker": "customer",
     "text": "After installing firmware seven point two the VPN keeps disconnecting every ten minutes."},
    {"speaker": "agent", "text": "I'm sorry about that. Let me note the details."},
    {"speaker": "customer",
     "text": "It started right after we moved to that release."},
    {"speaker": "agent", "text": "Understood. Is there anything else today?"},
]


def evaluate_audio(turns: list[dict] | None = None,
                   stereo: bool = True) -> AudioReport:
    turns = turns or DEMO_TURNS
    report = AudioReport(method="dual-channel" if stereo else "mono clustering")

    with tempfile.TemporaryDirectory() as tmp:
        call = synthesize_call(turns, Path(tmp) / "call.wav", stereo=stereo)
        segments, language, probability = transcribe(call.path)
        report.segments = len(segments)
        report.language, report.language_probability = language, probability
        report.wer = word_error_rate(" ".join(t["text"] for t in turns),
                                     " ".join(s.text for s in segments))

        clusters = diarize_stereo(call.path) if stereo else []
        if not clusters:
            report.method = "mono clustering"
            clusters = diarize(call.path, speakers=2)
        if not clusters:
            return report

        # Clusters are unlabelled by construction, so map each to the speaker
        # it overlaps most. This is scoring, not cheating: it resolves the
        # arbitrary cluster ids, and both clusters could still map to the same
        # speaker, which would show up as poor accuracy.
        tally: dict[tuple[int, str], int] = {}
        for start, end, cluster, _ in clusters:
            actual = call.speaker_at((start + end) / 2)
            if actual:
                tally[(cluster, actual)] = tally.get((cluster, actual), 0) + 1
        names: dict[int, str] = {}
        for cluster in {c for _, _, c, _ in clusters}:
            options = {s: n for (c, s), n in tally.items() if c == cluster}
            if options:
                names[cluster] = max(options, key=options.get)

        attribute(segments, clusters, names)
        for segment in segments:
            actual = call.speaker_at((segment.start + segment.end) / 2)
            if actual is None:
                continue
            report.diarization_total += 1
            if segment.speaker == actual:
                report.diarization_correct += 1
            else:
                report.mismatches.append(
                    f"said by {actual}, labelled {segment.speaker}: "
                    f"{segment.text[:52]}")
    return report
