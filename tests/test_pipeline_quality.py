# Tests for the multi-layer quality verification stack.

import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import numpy as np
import pytest
import soundfile as sf

from pipeline.quality import (
    LayerResult,
    PipelineQualityVerifier,
    QualityVerificationResult,
    VerificationContext,
)
from pipeline.quality_layers import FFmpegAudioMetricsVerifier


class TestFinalLoudnormConfig:
    """Candidate 5: final broadcast loudnorm targets are independent keys."""

    def test_final_loudnorm_independent_of_segment_thresholds(self):
        from config import config_manager
        from pipeline.jobs import _final_loudnorm_targets

        # The composed-output loudnorm reads its own dedicated config section.
        target_lufs, true_peak, lra = _final_loudnorm_targets()
        assert target_lufs == -16.0
        assert true_peak == -1.5
        assert lra == 11.0

        # The per-segment audio_metrics layer keeps its lenient peak threshold.
        segment_thresholds = FFmpegAudioMetricsVerifier()._config()
        assert segment_thresholds.get("true_peak_max_dbtp") == 0.5

        # Both come from config, not from inline fallback tables.
        verification = config_manager.get("pipeline.verification", {})
        assert verification["final_loudnorm"]["true_peak_dbtp"] == -1.5
        assert (
            verification["layers"]["audio_metrics"]["thresholds"]["true_peak_max_dbtp"]
            == 0.5
        )


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
            assert result.passed is True
            assert result.score == 1.0
            assert result.failure_reason is None
            assert "lufs" in result.metrics
            assert "dynamic_range_db" in result.metrics

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
            assert result.passed is False
            assert result.failure_reason == "low_rms"

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
            assert "true_peak_dbtp" in result.metrics
            # True peak is recorded but should not cause failure at segment level.
            assert result.failure_reason != "true_peak_exceeded"


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
        return LayerResult(
            passed=self._passed,
            score=self._score,
            failure_reason=self._failure_reason,
            metrics={},
        )


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


# ---------------------------------------------------------------------------
# Ticket 02: WhisperX alignment + jiwer content layers
# ---------------------------------------------------------------------------


def _make_whisperx_result(
    mean_confidence: float = 0.85,
    coverage_ratio: float = 0.96,
    reference: str = "hello world",
    transcription: str = "hello world",
) -> Dict[str, Any]:
    return {
        "transcription": transcription,
        "normalized_transcription": transcription,
        "normalized_reference": reference,
        "mean_word_confidence": mean_confidence,
        "text_coverage_ratio": coverage_ratio,
        "word_count": len(transcription.split()),
        "reference_unit_count": len(reference.split()),
        "language": "en",
        "whisperx_language": "en",
    }


class _FakeJiwer:
    """Minimal fake jiwer module for tests."""

    @staticmethod
    def wer(reference: str, hypothesis: str) -> float:
        ref_words = reference.split()
        hyp_words = hypothesis.split()
        if not ref_words and not hyp_words:
            return 0.0
        if not ref_words or not hyp_words:
            return 1.0
        # Simple word-level error estimate: 1 - overlap / max length.
        ref_set = set(ref_words)
        hyp_set = set(hyp_words)
        common = len(ref_set & hyp_set)
        return 1.0 - common / max(len(ref_set), len(hyp_set))

    @staticmethod
    def cer(reference: str, hypothesis: str) -> float:
        ref_chars = reference.replace(" ", "")
        hyp_chars = hypothesis.replace(" ", "")
        if not ref_chars and not hyp_chars:
            return 0.0
        if not ref_chars or not hyp_chars:
            return 1.0
        ref_set = set(ref_chars)
        hyp_set = set(hyp_chars)
        common = len(ref_set & hyp_set)
        return 1.0 - common / max(len(ref_set), len(hyp_set))


@pytest.fixture(autouse=True)
def patch_jiwer(monkeypatch):
    """Make jiwer available in tests even when not installed in the main env."""
    monkeypatch.setattr("pipeline.quality_layers.jiwer", _FakeJiwer())



class TestWhisperXAlignmentVerifier:
    def test_passes_with_good_confidence_and_coverage(self):
        from pipeline.quality_layers import WhisperXAlignmentVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = WhisperXAlignmentVerifier()
            context = VerificationContext(
                whisperx_result=_make_whisperx_result(
                    mean_confidence=0.85, coverage_ratio=0.96
                )
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
                context=context,
            )
            assert result.passed is True
            assert result.score == 1.0
            assert result.metrics["mean_word_confidence"] == 0.85

    def test_fails_low_confidence(self):
        from pipeline.quality_layers import WhisperXAlignmentVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = WhisperXAlignmentVerifier()
            context = VerificationContext(
                whisperx_result=_make_whisperx_result(
                    mean_confidence=0.50, coverage_ratio=0.96
                )
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
                context=context,
            )
            assert result.passed is False
            assert result.failure_reason == "whisperx_low_confidence"

    def test_whisperx_unavailable_without_result_on_context(self):
        from pipeline.quality_layers import WhisperXAlignmentVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = WhisperXAlignmentVerifier()
            # Both a missing context and a None whisperx_result (runner failed)
            # must behave like the old runner-failure path.
            for context in (None, VerificationContext(whisperx_result=None)):
                result = verifier.verify(
                    audio_path=str(path),
                    original_text="hello world",
                    reference_voice_path=None,
                    language="en",
                    expected_duration=1.0,
                    context=context,
                )
                assert result.passed is False
                assert result.failure_reason == "whisperx_unavailable"

    def test_fails_low_coverage(self):
        from pipeline.quality_layers import JiwerContentVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = JiwerContentVerifier()
            context = VerificationContext(
                whisperx_result=_make_whisperx_result(
                    reference="hello world today",
                    transcription="hello world",
                    mean_confidence=0.85,
                    coverage_ratio=0.67,
                )
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world today",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
                context=context,
            )
            assert result.passed is False
            assert result.failure_reason == "wer_too_high"


class TestJiwerContentVerifier:
    def test_passes_identical_text(self):
        from pipeline.quality_layers import JiwerContentVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = JiwerContentVerifier()
            context = VerificationContext(
                whisperx_result=_make_whisperx_result(
                    reference="hello world", transcription="hello world"
                )
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
                context=context,
            )
            assert result.passed is True
            assert result.metrics["wer"] == 0.0
            assert result.metrics["cer"] == 0.0

    def test_fails_high_wer(self):
        from pipeline.quality_layers import JiwerContentVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = JiwerContentVerifier()
            context = VerificationContext(
                whisperx_result=_make_whisperx_result(
                    reference="hello world",
                    transcription="goodbye moon",
                )
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
                context=context,
            )
            assert result.passed is False
            assert result.failure_reason == "wer_too_high"

    def test_chinese_wer_uses_characters(self):
        from pipeline.quality_layers import JiwerContentVerifier

        whisperx_result = _make_whisperx_result(
            reference="你好世界",
            transcription="你好世界",
            mean_confidence=0.90,
            coverage_ratio=1.0,
        )
        whisperx_result["word_count"] = 4
        whisperx_result["reference_unit_count"] = 4

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = JiwerContentVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="你好世界",
                reference_voice_path=None,
                language="zh",
                expected_duration=1.0,
                context=VerificationContext(whisperx_result=whisperx_result),
            )
            assert result.passed is True
            assert result.metrics["wer"] == 0.0


class TestWhisperXContextSharing:
    def test_orchestrator_runs_whisperx_once_for_all_whisperx_layers(self, monkeypatch):
        from pipeline.quality_layers import JiwerContentVerifier, WhisperXAlignmentVerifier

        call_count = {"n": 0}

        def fake_run(audio_path, reference_text, language, model_name, device, **kwargs):
            call_count["n"] += 1
            return _make_whisperx_result(reference="hello world", transcription="hello world")

        monkeypatch.setattr(
            "pipeline.verification_wrappers.runner.run_whisperx_align", fake_run
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = PipelineQualityVerifier(
                layers=[WhisperXAlignmentVerifier(), JiwerContentVerifier()],
                layer_config={
                    "whisperx_alignment": {"hardfail": True},
                    "jiwer_content": {"hardfail": True},
                },
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
            )
            assert call_count["n"] == 1, "WhisperX should only run once per verify() call"
            assert result.layer_results["whisperx_alignment"]["passed"] is True
            assert result.layer_results["jiwer_content"]["passed"] is True

    def test_orchestrator_shares_unavailable_result_with_all_layers(self, monkeypatch):
        from pipeline.quality_layers import JiwerContentVerifier, WhisperXAlignmentVerifier

        call_count = {"n": 0}

        def fake_run(audio_path, reference_text, language, model_name, device, **kwargs):
            call_count["n"] += 1
            return None

        monkeypatch.setattr(
            "pipeline.verification_wrappers.runner.run_whisperx_align", fake_run
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = PipelineQualityVerifier(
                layers=[WhisperXAlignmentVerifier(), JiwerContentVerifier()],
                layer_config={
                    "whisperx_alignment": {"hardfail": True},
                    "jiwer_content": {"hardfail": True},
                },
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
            )
            assert call_count["n"] == 1, "WhisperX should only run once per verify() call"
            assert (
                result.layer_results["whisperx_alignment"]["failure_reason"]
                == "whisperx_unavailable"
            )
            assert (
                result.layer_results["jiwer_content"]["failure_reason"]
                == "whisperx_unavailable"
            )


# ---------------------------------------------------------------------------
# Ticket 03: Speaker + spectral feedback layers
# ---------------------------------------------------------------------------


class TestResemblyzerSpeakerVerifier:
    def test_passes_feedback_only_with_similarity(self, monkeypatch):
        from pipeline.quality_layers import ResemblyzerSpeakerVerifier

        def fake_run(audio_path, reference_voice_path):
            return {"cosine_similarity": 0.82, "embedding_shape": [256]}

        monkeypatch.setattr(
            "pipeline.verification_wrappers.runner.run_resemblyzer_speaker", fake_run
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = ResemblyzerSpeakerVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=str(path),
                language="en",
                expected_duration=1.0,
            )
            assert result.passed is True
            assert result.score == 0.82
            assert result.metrics["cosine_similarity"] == 0.82

    def test_no_reference_voice_returns_zero_score(self):
        from pipeline.quality_layers import ResemblyzerSpeakerVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = ResemblyzerSpeakerVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
            )
            assert result.passed is True
            assert result.score == 0.0
            assert result.metrics["cosine_similarity"] is None


class TestLibrosaSpectralVerifier:
    def test_passes_feedback_only_with_metrics(self, monkeypatch):
        from pipeline.quality_layers import LibrosaSpectralVerifier

        def fake_run(audio_path, reference_voice_path):
            return {
                "mfcc_mse": 0.03,
                "spectral_contrast_ratio": 0.85,
                "audio_duration_sec": 2.0,
                "reference_duration_sec": 2.0,
            }

        monkeypatch.setattr(
            "pipeline.verification_wrappers.runner.run_librosa_spectral", fake_run
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = LibrosaSpectralVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=str(path),
                language="en",
                expected_duration=1.0,
            )
            assert result.passed is True
            assert result.score == 0.85
            assert result.metrics["mfcc_mse"] == 0.03
            assert result.metrics["spectral_contrast_ratio"] == 0.85

    def test_no_reference_voice_returns_zero_score(self):
        from pipeline.quality_layers import LibrosaSpectralVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = LibrosaSpectralVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="hello world",
                reference_voice_path=None,
                language="en",
                expected_duration=1.0,
            )
            assert result.passed is True
            assert result.score == 0.0
            assert result.metrics["mfcc_mse"] is None
