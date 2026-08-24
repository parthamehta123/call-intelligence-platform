"""Audio ingestion: synthesis, transcription, language ID, diarization.

The pipeline's input assumption was text, and everything downstream --
attribution, extraction, corroboration -- rests on knowing *who said what*.
This module supplies that from actual audio rather than assuming it:

    synthesize_call()  two-voice WAV, plus ground truth for evaluation
    transcribe()       faster-whisper large-v3, word timestamps + language
    diarize()          spectral features -> KMeans -> speaker timeline
    attribute()        transcript segments x speaker timeline -> turns

Diarization here is unsupervised clustering over spectral features, not a
trained speaker-embedding model. It is real -- it recovers who spoke from
the waveform, and `cip.eval.audio_eval` measures how often it is right --
but a production system would use a proper diarizer. What matters
downstream is the interface: turns carrying `speaker` and
`speaker_confidence`, which is exactly what the attribution rules already
consume.
"""

from __future__ import annotations

import glob
import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SAMPLE_RATE = 16000
# macOS voices, one per role. Distinct enough that clustering has a signal
# to find, which is the point: this is a test fixture, not a claim that real
# call audio separates this cleanly.
VOICES = {"customer": "Alex", "agent": "Samantha"}
# Channel order for stereo synthesis: index 0 left, index 1 right.
CHANNELS = ("agent", "customer")


@dataclass
class SpeechSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    speaker_confidence: float = 1.0


@dataclass
class SynthesisedCall:
    path: Path
    truth: list[tuple[float, float, str]] = field(default_factory=list)

    def speaker_at(self, when: float) -> str | None:
        for start, end, speaker in self.truth:
            if start <= when < end:
                return speaker
        return None


# --- synthesis --------------------------------------------------------------
def _say_to_wav(text: str, voice: str, path: Path) -> float:
    subprocess.run(
        ["say", "-v", voice, "-o", str(path),
         "--data-format=LEI16@%d" % SAMPLE_RATE, text],
        check=True, capture_output=True)
    with wave.open(str(path)) as handle:
        return handle.getnframes() / handle.getframerate()


def synthesize_call(turns: list[dict], out_path: Path,
                    stereo: bool = True) -> SynthesisedCall:
    """Render turns to one WAV, recording who speaks when.

    The ground truth is returned separately and never travels with the
    audio -- diarization has to recover it, and an evaluation that could
    read the answer would measure nothing.
    """
    import numpy as np

    out_path.parent.mkdir(parents=True, exist_ok=True)
    samples: list = []
    truth: list[tuple[float, float, str]] = []
    cursor = 0.0

    with tempfile.TemporaryDirectory() as tmp:
        for index, turn in enumerate(turns):
            piece = Path(tmp) / f"{index:03d}.wav"
            _say_to_wav(turn["text"], VOICES.get(turn["speaker"], "Alex"), piece)
            with wave.open(str(piece)) as handle:
                audio = np.frombuffer(handle.readframes(handle.getnframes()),
                                      dtype=np.int16)
            samples.append(audio)
            duration = len(audio) / SAMPLE_RATE
            truth.append((cursor, cursor + duration, turn["speaker"]))
            cursor += duration
            # A beat between turns, as in a real call.
            gap = np.zeros(int(0.25 * SAMPLE_RATE), dtype=np.int16)
            samples.append(gap)
            cursor += 0.25

    joined = np.concatenate(samples) if samples else np.zeros(0, dtype="int16")

    if stereo:
        # Two channels, one speaker each -- how contact centres actually
        # record. Diarization then becomes a question about energy rather
        # than a clustering problem, which is precisely why the industry
        # records this way.
        left = np.zeros_like(joined)
        right = np.zeros_like(joined)
        for start, end, speaker in truth:
            a, b = int(start * SAMPLE_RATE), int(end * SAMPLE_RATE)
            target = left if speaker == CHANNELS[0] else right
            target[a:b] = joined[a:b]
        interleaved = np.empty(len(joined) * 2, dtype=np.int16)
        interleaved[0::2], interleaved[1::2] = left, right
        payload, channels = interleaved, 2
    else:
        payload, channels = joined, 1

    with wave.open(str(out_path), "w") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(payload.tobytes())
    return SynthesisedCall(path=out_path, truth=truth)


def diarize_stereo(path: Path, window: float = 0.25
                   ) -> list[tuple[float, float, int, float]]:
    """Speaker per window from channel energy.

    The primary path. Dual-channel recording is standard in contact
    centres, and where it exists, inferring the speaker from the waveform
    is solving a problem that has already been solved by the microphone.
    Returns [] for mono audio so the caller can fall back to clustering.
    """
    import numpy as np

    with wave.open(str(path)) as handle:
        if handle.getnchannels() != 2:
            return []
        rate = handle.getframerate()
        raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)

    left, right = raw[0::2].astype(np.float32), raw[1::2].astype(np.float32)
    size = max(1, int(window * rate))
    out = []
    for start in range(0, len(left) - size + 1, size):
        a = np.abs(left[start:start + size]).mean()
        b = np.abs(right[start:start + size]).mean()
        total = a + b
        if total < 1.0:      # silence on both channels
            continue
        cluster = 0 if a >= b else 1
        # Confidence is channel dominance: crosstalk and overtalk push this
        # toward 0.5, which is exactly when attribution should be doubted.
        out.append((start / rate, (start + size) / rate, cluster,
                    float(abs(a - b) / total)))
    return out


# --- transcription + language identification --------------------------------
@lru_cache(maxsize=1)
def _whisper(model_size: str = "large-v3"):
    from faster_whisper import WhisperModel

    pattern = os.path.expanduser(
        f"~/.cache/huggingface/hub/models--Systran--faster-whisper-{model_size}/snapshots/*")
    # Load from the snapshot directory: resolving by repo id goes through the
    # hub, which fails on a stale or absent token for a public model.
    snapshots = [d for d in glob.glob(pattern) if os.path.isdir(d)]
    source = snapshots[-1] if snapshots else f"Systran/faster-whisper-{model_size}"
    return WhisperModel(source, device="cpu", compute_type="int8")


def transcribe(path: Path, model_size: str = "large-v3") -> tuple[list[SpeechSegment], str, float]:
    """Audio -> segments, detected language, language probability.

    Language identification comes free with the ASR pass, and it is the
    honest place for it: guessing the language of a transcript after the
    fact discards the acoustic evidence that settles it.
    """
    segments, info = _whisper(model_size).transcribe(str(path), beam_size=1,
                                                     word_timestamps=False)
    speech = [SpeechSegment(start=s.start, end=s.end, text=s.text.strip())
              for s in segments]
    return speech, info.language, float(info.language_probability)


# --- diarization ------------------------------------------------------------
def _f0(chunk, rate: int, low: float = 60.0, high: float = 320.0) -> float:
    """Fundamental frequency by autocorrelation, in Hz (0.0 if unvoiced)."""
    import numpy as np

    chunk = chunk - chunk.mean()
    if not np.any(chunk):
        return 0.0
    correlation = np.correlate(chunk, chunk, mode="full")[len(chunk) - 1:]
    lo, hi = int(rate / high), int(rate / low)
    if hi >= len(correlation) or hi <= lo:
        return 0.0
    window = correlation[lo:hi]
    peak = int(np.argmax(window)) + lo
    if correlation[0] <= 0 or correlation[peak] / correlation[0] < 0.3:
        return 0.0
    return float(rate / peak)


def _smooth(labels, width: int = 3):
    """Median filter. Speakers hold the floor for several windows at a time,
    so isolated flips are clustering noise rather than turn changes."""
    import numpy as np

    if len(labels) < width:
        return labels
    out = labels.copy()
    half = width // 2
    for i in range(half, len(labels) - half):
        out[i] = np.bincount(labels[i - half:i + half + 1]).argmax()
    return out


def _frame_features(path: Path, window: float = 0.5):
    """Log-magnitude spectra averaged over short windows.

    A speaker-embedding model would be better. This is deliberately simple
    and dependency-free so the audio path runs anywhere, and its accuracy is
    measured rather than assumed.
    """
    import numpy as np

    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        audio = np.frombuffer(handle.readframes(handle.getnframes()),
                              dtype=np.int16).astype(np.float32)

    size = int(window * rate)
    if size <= 0 or len(audio) < size:
        return np.zeros((0, 1)), np.zeros(0), rate

    frames, times = [], []
    for start in range(0, len(audio) - size + 1, size):
        chunk = audio[start:start + size]
        windowed = chunk * np.hanning(len(chunk))
        spectrum = np.abs(np.fft.rfft(windowed))
        # 24 log-spaced bands: coarse spectral shape.
        edges = np.geomspace(1, len(spectrum) - 1, 25).astype(int)
        bands = [np.log1p(spectrum[a:b].mean() if b > a else 0.0)
                 for a, b in zip(edges[:-1], edges[1:])]
        # Fundamental frequency, weighted heavily. Spectral shape alone
        # clustered barely above chance (0.667 on two speakers); pitch is
        # what actually separates voices, and omitting it was the reason.
        bands += [_f0(chunk, rate) / 100.0] * 6
        frames.append(bands)
        times.append(start / rate)
    return np.asarray(frames), np.asarray(times), rate


def diarize(path: Path, speakers: int = 2) -> list[tuple[float, float, int, float]]:
    """(start, end, cluster, confidence) over the whole file."""
    import numpy as np
    from sklearn.cluster import KMeans

    features, times, _ = _frame_features(path)
    if len(features) < speakers:
        return []

    energy = features.mean(axis=1)
    # Silence carries no speaker identity and would form its own cluster.
    voiced = energy > (energy.min() + 0.25 * (energy.max() - energy.min()))
    if voiced.sum() < speakers:
        return []

    normalised = (features - features.mean(axis=0)) / (features.std(axis=0) + 1e-6)
    model = KMeans(n_clusters=speakers, n_init=10, random_state=0)
    labels = _smooth(model.fit_predict(normalised[voiced]))

    distances = model.transform(normalised[voiced])
    ordered = np.sort(distances, axis=1)
    # Margin between the nearest and next-nearest centroid, squashed to
    # [0,1]. This becomes speaker_confidence, which the attribution rules
    # already use to discount weakly-diarized turns.
    margin = (ordered[:, 1] - ordered[:, 0]) / (ordered[:, 1] + 1e-6)

    window = float(times[1] - times[0]) if len(times) > 1 else 0.5
    out, voiced_index = [], 0
    for index, is_voiced in enumerate(voiced):
        if not is_voiced:
            continue
        out.append((float(times[index]), float(times[index] + window),
                    int(labels[voiced_index]),
                    float(min(1.0, max(0.0, margin[voiced_index])))))
        voiced_index += 1
    return out


def attribute(segments: list[SpeechSegment],
              diarization: list[tuple[float, float, int, float]],
              cluster_names: dict[int, str]) -> list[SpeechSegment]:
    """Assign a speaker to each transcript segment by time overlap."""
    for segment in segments:
        overlapping = [(cluster, confidence) for start, end, cluster, confidence
                       in diarization
                       if min(end, segment.end) - max(start, segment.start) > 0]
        if not overlapping:
            segment.speaker, segment.speaker_confidence = None, 0.0
            continue
        votes: dict[int, float] = {}
        for cluster, confidence in overlapping:
            votes[cluster] = votes.get(cluster, 0.0) + 1.0
        winner = max(votes, key=votes.get)
        share = votes[winner] / sum(votes.values())
        confidences = [c for k, c in overlapping if k == winner]
        segment.speaker = cluster_names.get(winner)
        # Both how cleanly the frames clustered and how consistently the
        # segment sat in one cluster.
        segment.speaker_confidence = round(
            share * (sum(confidences) / len(confidences)), 3)
    return segments
