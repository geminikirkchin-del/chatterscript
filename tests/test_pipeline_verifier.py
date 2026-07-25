# Tests for pipeline.verifier
# Plain assert style.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from pipeline.verifier import (
    AudioVerificationResult,
    VerificationThresholds,
    detect_long_silence,
    verify_audio,
)


def test_normal_audio_passes():
    sr = 24000
    # 1s of moderate-amplitude sine-ish tone
    t = np.arange(sr) / sr
    audio = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    result = verify_audio(audio, sr, expected_duration=1.0)
    assert result.passed, result.failure_reason
    assert result.score > 0.5


def test_silent_audio_fails_low_rms():
    sr = 24000
    audio = np.zeros(sr, dtype=np.float32)
    result = verify_audio(audio, sr, expected_duration=1.0)
    assert not result.passed
    assert "rms" in result.failure_reason.lower() or "silent" in result.failure_reason.lower()


def test_clipping_audio_fails():
    sr = 24000
    audio = np.full(sr, 0.995, dtype=np.float32)
    result = verify_audio(audio, sr, expected_duration=1.0)
    assert not result.passed
    assert "clip" in result.failure_reason.lower()


def test_long_internal_silence_fails():
    sr = 24000
    # 0.2s tone, 0.6s silence, 0.2s tone -> 600ms silence exceeds default 500ms threshold.
    tone = 0.3 * np.ones(int(0.2 * sr), dtype=np.float32)
    silence = np.zeros(int(0.6 * sr), dtype=np.float32)
    tail = 0.3 * np.ones(int(0.2 * sr), dtype=np.float32)
    audio = np.concatenate([tone, silence, tail])
    result = verify_audio(audio, sr, expected_duration=1.0)
    assert not result.passed
    assert "silence" in result.failure_reason.lower()


def test_duration_deviation_fails():
    sr = 24000
    audio = 0.3 * np.ones(int(2.0 * sr), dtype=np.float32)  # 2s actual
    result = verify_audio(audio, sr, expected_duration=1.0)
    assert not result.passed
    assert "duration" in result.failure_reason.lower()


def test_detect_long_silence_returns_duration():
    sr = 24000
    silence = np.zeros(int(0.5 * sr), dtype=np.float32)
    audio = np.concatenate([
        0.3 * np.ones(int(0.2 * sr), dtype=np.float32),
        silence,
        0.3 * np.ones(int(0.3 * sr), dtype=np.float32),
    ])
    longest = detect_long_silence(audio, sr, silence_threshold=0.01, min_silence_ms=300)
    assert longest >= 0.5, longest


def test_threshold_overrides():
    sr = 24000
    audio = 0.3 * np.ones(sr, dtype=np.float32)  # normal
    thresholds = VerificationThresholds(
        max_silence_ms=1000,
        clip_threshold=0.999,
        min_rms=0.001,
        max_duration_deviation=0.5,
    )
    result = verify_audio(audio, sr, expected_duration=1.0, thresholds=thresholds)
    assert result.passed


def test_combined_score_reflects_quality():
    sr = 24000
    t = np.arange(sr) / sr
    good = 0.5 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    bad = np.zeros(sr, dtype=np.float32)
    good_result = verify_audio(good, sr, expected_duration=1.0)
    bad_result = verify_audio(bad, sr, expected_duration=1.0)
    assert good_result.score > bad_result.score


if __name__ == "__main__":
    test_normal_audio_passes()
    test_silent_audio_fails_low_rms()
    test_clipping_audio_fails()
    test_long_internal_silence_fails()
    test_duration_deviation_fails()
    test_detect_long_silence_returns_duration()
    test_threshold_overrides()
    test_combined_score_reflects_quality()
    print("ALL VERIFIER TESTS PASSED")
