# Audio verification for TTS pipeline segments.

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_SILENCE_THRESHOLD = 0.01
DEFAULT_CLIP_THRESHOLD = 0.99
DEFAULT_MIN_RMS = 0.01
DEFAULT_MAX_SILENCE_MS = 300.0
DEFAULT_MAX_DURATION_DEVIATION = 0.30


@dataclass
class VerificationThresholds:
    """Thresholds for audio verification checks."""

    max_silence_ms: float = DEFAULT_MAX_SILENCE_MS
    clip_threshold: float = DEFAULT_CLIP_THRESHOLD
    min_rms: float = DEFAULT_MIN_RMS
    max_duration_deviation: float = DEFAULT_MAX_DURATION_DEVIATION


@dataclass
class AudioVerificationResult:
    """Result of audio verification for one segment."""

    passed: bool
    score: float
    failure_reason: Optional[str]
    metrics: Dict[str, Any]


def detect_long_silence(
    audio: np.ndarray,
    sr: int,
    silence_threshold: float = DEFAULT_SILENCE_THRESHOLD,
    min_silence_ms: float = DEFAULT_MAX_SILENCE_MS,
) -> float:
    """
    Return the longest contiguous near-silent region in seconds.

    Args:
        audio: mono float32 audio array.
        sr: sample rate.
        silence_threshold: absolute amplitude below which a sample is considered silent.
        min_silence_ms: minimum silence duration to report (not used for measurement).

    Returns:
        longest silence duration in seconds.
    """
    min_samples = int(min_silence_ms / 1000.0 * sr)
    silent = np.abs(audio) < silence_threshold
    if not silent.any():
        return 0.0

    # Find runs of silence.
    padded = np.concatenate(([False], silent, [False]))
    diff = np.diff(padded.astype(int))
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]

    longest = 0
    for start, end in zip(starts, ends):
        run_len = end - start
        if run_len > longest:
            longest = run_len
    return longest / sr


def _compute_rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))


def _compute_peak(audio: np.ndarray) -> float:
    return float(np.max(np.abs(audio)))


def verify_audio(
    audio: np.ndarray,
    sr: int,
    expected_duration: float,
    thresholds: Optional[VerificationThresholds] = None,
) -> AudioVerificationResult:
    """
    Run audio quality checks on a generated segment.

    Args:
        audio: mono float32 audio array.
        sr: sample rate.
        expected_duration: estimated target duration in seconds.
        thresholds: verification thresholds (uses defaults if None).

    Returns:
        AudioVerificationResult with passed flag, score, failure reason, and metrics.
    """
    if thresholds is None:
        thresholds = VerificationThresholds()

    metrics: Dict[str, Any] = {
        "duration_sec": len(audio) / sr,
        "expected_duration_sec": expected_duration,
        "sample_rate": sr,
    }

    # Check RMS first to avoid noise on truly silent output.
    rms = _compute_rms(audio)
    metrics["rms"] = rms
    if rms < thresholds.min_rms:
        return AudioVerificationResult(
            passed=False,
            score=0.0,
            failure_reason="low_rms",
            metrics=metrics,
        )

    # Check clipping. A single sample near the digital ceiling is common for
    # normalized TTS output; flag only when a non-trivial portion of the signal
    # is clipped so we don't reject otherwise clean audio.
    peak = _compute_peak(audio)
    metrics["peak"] = peak
    clipped_ratio = float(np.mean(np.abs(audio) >= thresholds.clip_threshold))
    metrics["clipped_ratio"] = clipped_ratio
    if clipped_ratio > 0.01:
        return AudioVerificationResult(
            passed=False,
            score=0.0,
            failure_reason="clipping",
            metrics=metrics,
        )

    # Check long internal silence.
    longest_silence = detect_long_silence(
        audio,
        sr,
        silence_threshold=DEFAULT_SILENCE_THRESHOLD,
        min_silence_ms=thresholds.max_silence_ms,
    )
    metrics["longest_silence_sec"] = longest_silence
    if longest_silence * 1000 > thresholds.max_silence_ms:
        return AudioVerificationResult(
            passed=False,
            score=0.0,
            failure_reason="long_silence",
            metrics=metrics,
        )

    # Check duration deviation.
    actual_duration = len(audio) / sr
    if expected_duration > 0:
        deviation = abs(actual_duration - expected_duration) / expected_duration
    else:
        deviation = 0.0
    metrics["duration_deviation"] = deviation
    if deviation > thresholds.max_duration_deviation:
        return AudioVerificationResult(
            passed=False,
            score=0.0,
            failure_reason="duration_deviation",
            metrics=metrics,
        )

    # Score: higher RMS up to a point is better; penalize silence and deviation.
    score = min(1.0, rms / 0.3) * 0.5
    score += 0.25 * (1.0 - min(1.0, longest_silence / 0.3))
    score += 0.25 * (1.0 - min(1.0, deviation / thresholds.max_duration_deviation))
    score = max(0.0, min(1.0, score))

    return AudioVerificationResult(
        passed=True,
        score=score,
        failure_reason=None,
        metrics=metrics,
    )
