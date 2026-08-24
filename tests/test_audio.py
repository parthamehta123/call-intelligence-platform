"""Audio ingestion: synthesis, ASR, language ID, diarization.

These run real models and real TTS, so they are slower than the rest of the
suite and skip cleanly where the dependencies are absent.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("sklearn")
if shutil.which("say") is None:  # pragma: no cover - platform dependent
    pytest.skip("macOS `say` unavailable", allow_module_level=True)

from cip.audio import (CHANNELS, diarize, diarize_stereo,  # noqa: E402
                       synthesize_call)
from cip.eval.audio_eval import word_error_rate  # noqa: E402

TURNS = [
    {"speaker": "agent", "text": "Thanks for calling support."},
    {"speaker": "customer", "text": "The VPN keeps disconnecting every ten minutes."},
    {"speaker": "agent", "text": "Let me note that down."},
]


@pytest.fixture(scope="module")
def stereo_call():
    tmp = tempfile.mkdtemp()
    yield synthesize_call(TURNS, Path(tmp) / "call.wav", stereo=True)
    shutil.rmtree(tmp, ignore_errors=True)


def test_word_error_rate_ignores_formatting_differences():
    """ASR writes "7.2" where the speaker said "seven point two"; a string
    comparison would call a correct transcript a failure."""
    assert word_error_rate("the vpn drops", "The VPN drops!") == 0.0
    assert word_error_rate("a b c", "a b") == pytest.approx(1 / 3)


def test_ground_truth_never_travels_with_the_audio(stereo_call):
    """Diarization must recover the speaker, not read it."""
    assert stereo_call.truth
    assert stereo_call.path.exists()
    assert stereo_call.path.suffix == ".wav"


def test_dual_channel_diarization_recovers_the_speaker(stereo_call):
    windows = diarize_stereo(stereo_call.path)
    assert windows, "stereo audio must produce a speaker timeline"

    correct = 0
    for start, end, cluster, _ in windows:
        actual = stereo_call.speaker_at((start + end) / 2)
        if actual is not None and CHANNELS[cluster] == actual:
            correct += 1
    assert correct / len(windows) >= 0.9


def test_channel_dominance_becomes_speaker_confidence(stereo_call):
    """Crosstalk should lower confidence, which is what the attribution
    rules already use to discount a weakly-diarized turn."""
    windows = diarize_stereo(stereo_call.path)
    assert all(0.0 <= confidence <= 1.0 for _, _, _, confidence in windows)
    assert max(confidence for _, _, _, confidence in windows) > 0.9


def test_mono_audio_falls_back_rather_than_failing():
    """A mono recording has no channel to read, so it must degrade to
    clustering instead of returning nothing."""
    tmp = tempfile.mkdtemp()
    try:
        call = synthesize_call(TURNS, Path(tmp) / "mono.wav", stereo=False)
        assert diarize_stereo(call.path) == [], "mono must not be diarized by channel"
        assert diarize(call.path, speakers=2), "clustering fallback must produce output"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
