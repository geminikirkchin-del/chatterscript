# Tests for the multi-layer quality verification stack.

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import soundfile as sf

from pipeline.quality import PipelineQualityVerifier, QualityVerificationResult
from pipeline.quality_layers import FFmpegAudioMetricsVerifier


def _write_test_tone(path: str, duration_sec: float = 2.0, sr: int = 24000) -> None:
    """Write an amplitude-modulated sine tone with moderate dynamic range."""
    samples = int(sr * duration_sec)
    t = np.linspace(0, duration_sec, samples, dtype=np.float32)
    # Amplitude modulation gives clear loud/quiet windows while keeping LUFS near target.
    envelope = 0.25 + 0.15 * np.sin(2 * np.pi * 2 * t)
    tone = envelope * np.sin(2 * np.pi * 440 * t)
    tone = tone.astype(np.float32)
    sf.write(path, tone, sr, subtype="PCM_16")


def _write_silent_file(path: str, duration_sec: float = 1.0, sr: int = 24000) -> None:
    """Write a near-silent file."""
    silence = np.zeros(int(sr * duration_sec), dtype=np.float32)
    sf.write(path, silence, sr, subtype="PCM_16")


class TestFFmpegAudioMetricsVerifier:
    def test_passes_for_normal_tone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = FFmpegAudioMetricsVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
            )
            assert result["passed"] is True
            assert result["score"] == 1.0
            assert result["failure_reason"] is None
            assert "lufs" in result["metrics"]
            assert "dynamic_range_db" in result["metrics"]

    def test_fails_low_rms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "silent.wav"
            _write_silent_file(str(path))
            verifier = FFmpegAudioMetricsVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
            )
            assert result["passed"] is False
            assert result["failure_reason"] == "low_rms"

    def test_records_true_peak_without_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = FFmpegAudioMetricsVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
            )
            assert "true_peak_dbtp" in result["metrics"]
            # True peak is recorded but should not cause failure at segment level.
            assert result["failure_reason"] != "true_peak_exceeded"


class MockVerifier:
    """Mock verifier for testing the orchestrator."""

    def __init__(self, name: str, passed: bool, score: float, failure_reason: str | None):
        self._name = name
        self._passed = passed
        self._score = score
        self._failure_reason = failure_reason

    @property
    def name(self) -> str:
        return self._name

    def verify(self, **kwargs):
        return {
            "passed": self._passed,
            "score": self._score,
            "failure_reason": self._failure_reason,
            "metrics": {},
        }


def test_ffmpeg_audio_metrics_passes_for_normal_tone():
    TestFFmpegAudioMetricsVerifier().test_passes_for_normal_tone()


def test_ffmpeg_audio_metrics_fails_low_rms():
    TestFFmpegAudioMetricsVerifier().test_fails_low_rms()


def test_ffmpeg_audio_metrics_records_true_peak():
    TestFFmpegAudioMetricsVerifier().test_records_true_peak_without_failing()


def test_quality_verifier_passes_when_all_hardfail_layers_pass():
    TestPipelineQualityVerifier().test_passes_when_all_hardfail_layers_pass()


def test_quality_verifier_fails_when_hardfail_layer_fails():
    TestPipelineQualityVerifier().test_fails_when_hardfail_layer_fails()


def test_quality_verifier_feedback_only_layer_does_not_fail():
    TestPipelineQualityVerifier().test_feedback_only_layer_does_not_fail()


class TestPipelineQualityVerifier:
    def test_passes_when_all_hardfail_layers_pass(self):
        verifier = PipelineQualityVerifier(
            layers=[
                MockVerifier("basic_audio", True, 0.8, None),
                MockVerifier("audio_metrics", True, 0.9, None),
            ],
            layer_config={
                "basic_audio": {"hardfail": True},
                "audio_metrics": {"hardfail": True},
            },
        )
        result = verifier.verify(
            audio_path="x.wav",
            original_text="hello",
            reference_voice_path=None,
            language="en",
            expected_duration=1.0,
        )
        assert result.passed is True
        assert result.overall_score > 0.8
        assert result.failure_reason is None

    def test_fails_when_hardfail_layer_fails(self):
        verifier = PipelineQualityVerifier(
            layers=[
                MockVerifier("basic_audio", True, 0.8, None),
                MockVerifier("audio_metrics", False, 0.2, "lufs_out_of_range"),
            ],
            layer_config={
                "basic_audio": {"hardfail": True},
                "audio_metrics": {"hardfail": True},
            },
        )
        result = verifier.verify(
            audio_path="x.wav",
            original_text="hello",
            reference_voice_path=None,
            language="en",
            expected_duration=1.0,
        )
        assert result.passed is False
        assert result.failure_reason == "lufs_out_of_range"

    def test_feedback_only_layer_does_not_fail(self):
        verifier = PipelineQualityVerifier(
            layers=[
                MockVerifier("basic_audio", True, 0.8, None),
                MockVerifier("resemblyzer_speaker", False, 0.2, "similarity_low"),
            ],
            layer_config={
                "basic_audio": {"hardfail": True},
                "resemblyzer_speaker": {"hardfail": False},
            },
        )
        result = verifier.verify(
            audio_path="x.wav",
            original_text="hello",
            reference_voice_path=None,
            language="en",
            expected_duration=1.0,
        )
        assert result.passed is True
        assert "resemblyzer_speaker" in result.layer_results
