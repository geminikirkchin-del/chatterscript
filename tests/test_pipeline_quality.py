# Tests for the multi-layer quality verification stack.

import tempfile
from pathlib import Path
from typing import Any, Dict, List
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

    @staticmethod
    def process_characters(reference: str, hypothesis: str):
        return _process_alignment(list(reference), list(hypothesis))

    @staticmethod
    def process_words(reference: str, hypothesis: str):
        return _process_alignment(reference.split(), hypothesis.split())


class _ProcessOutput:
    def __init__(self, substitutions: int, deletions: int, insertions: int, hits: int):
        self.substitutions = substitutions
        self.deletions = deletions
        self.insertions = insertions
        self.hits = hits


def _process_alignment(ref_items, hyp_items) -> _ProcessOutput:
    """Approximate jiwer's S/D/I counts using difflib opcodes."""
    import difflib

    sm = difflib.SequenceMatcher(a=ref_items, b=hyp_items, autojunk=False)
    subs = dels = ins = hits = 0
    for tag, a1, a2, b1, b2 in sm.get_opcodes():
        if tag == "equal":
            hits += a2 - a1
        elif tag == "replace":
            subs += max(a2 - a1, b2 - b1)
        elif tag == "delete":
            dels += a2 - a1
        elif tag == "insert":
            ins += b2 - b1
    return _ProcessOutput(subs, dels, ins, hits)


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
                    transcription="goodbye moon sun",
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
                    reference="hello world today",
                    transcription="goodbye moon sun",
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

    def test_tiny_sentence_below_error_floor_passes(self):
        """1 char wrong in an 8-char sentence is CER 0.125 but must not fail."""
        from pipeline.quality_layers import JiwerContentVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = JiwerContentVerifier()
            context = VerificationContext(
                whisperx_result=_make_whisperx_result(
                    reference="第一永遠先問需求",
                    transcription="第一永遠先問需要",  # 1 char different
                    mean_confidence=0.99,
                    coverage_ratio=1.0,
                )
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="第一永遠先問需求",
                reference_voice_path=None,
                language="zh",
                expected_duration=1.0,
                context=context,
            )
            assert result.passed is True
            assert result.metrics["char_errors"] == 1

    def test_error_floor_still_fails_real_errors(self):
        """5 char errors in 10 chars exceeds both the rate and the floor."""
        from pipeline.quality_layers import JiwerContentVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = JiwerContentVerifier()
            context = VerificationContext(
                whisperx_result=_make_whisperx_result(
                    reference="這是一個測試句子範例",
                    transcription="這是二個無驗句子錯誤",  # 5 chars different
                    mean_confidence=0.9,
                    coverage_ratio=1.0,
                )
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="這是一個測試句子範例",
                reference_voice_path=None,
                language="zh",
                expected_duration=1.0,
                context=context,
            )
            assert result.passed is False
            assert result.failure_reason in ("wer_too_high", "cer_too_high")
            assert result.metrics["char_errors"] >= 3


class TestTempoDriftVerifier:
    def _write_bursts(self, path: Path, intervals_first: float, intervals_second: float):
        """Write tone bursts: first half at one onset rate, second half at another."""
        sr = 24000
        pieces = []
        for half_intervals in (intervals_first, intervals_second):
            t = 0.0
            while t < 3.0:
                pieces.append(_tone_burst(sr, 0.08))
                pieces.append(np.zeros(int(sr * max(half_intervals - 0.08, 0.02)), dtype=np.float32))
                t += half_intervals
        sf.write(str(path), np.concatenate(pieces), sr, subtype="pcm_16")

    def test_stable_tempo_not_flagged(self):
        from pipeline.quality_layers import TempoDriftVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stable.wav"
            self._write_bursts(path, 0.2, 0.2)
            result = TempoDriftVerifier().verify(
                audio_path=str(path),
                original_text="x",
                reference_voice_path=None,
                language="zh",
                expected_duration=6.0,
            )
            assert result.passed is True  # feedback-only, never fails
            assert result.metrics["drift_ratio"] < 1.25
            assert result.metrics["drift_flagged"] is False

    def test_shifting_tempo_flagged_in_metrics(self):
        from pipeline.quality_layers import TempoDriftVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shift.wav"
            self._write_bursts(path, 0.35, 0.12)  # second half ~3x faster
            result = TempoDriftVerifier().verify(
                audio_path=str(path),
                original_text="x",
                reference_voice_path=None,
                language="zh",
                expected_duration=6.0,
            )
            assert result.passed is True  # feedback-only
            assert result.metrics["drift_ratio"] > 1.25
            assert result.metrics["drift_flagged"] is True
            assert result.metrics["rate_second_half"] > result.metrics["rate_first_half"]

    def test_insufficient_onsets_reports_note(self):
        from pipeline.quality_layers import TempoDriftVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.wav"
            sf.write(str(path), _tone_burst(24000, 0.3), 24000, subtype="pcm_16")
            result = TempoDriftVerifier().verify(
                audio_path=str(path),
                original_text="x",
                reference_voice_path=None,
                language="zh",
                expected_duration=0.3,
            )
            assert result.passed is True
            assert result.metrics.get("note") == "insufficient_onsets"


def _tone_burst(sr: int, duration: float) -> np.ndarray:
    t = np.arange(int(sr * duration), dtype=np.float32) / sr
    return 0.5 * np.sin(2 * np.pi * 440 * t)


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


# ---------------------------------------------------------------------------
# Ending-artifact verifier (elongated final syllable + trailing audio)
# ---------------------------------------------------------------------------


def _make_ending_artifact_whisperx_result(
    words: List[Dict[str, Any]],
    reference: str = "reference text",
) -> Dict[str, Any]:
    """Build a WhisperX result with aligned word timestamps."""
    return {
        "transcription": " ".join(str(w.get("word", "")) for w in words),
        "normalized_transcription": reference,
        "normalized_reference": reference,
        "mean_word_confidence": 0.85,
        "text_coverage_ratio": 1.0,
        "word_count": len(words),
        "reference_unit_count": len(reference),
        "language": "zh",
        "whisperx_language": "zh",
        "aligned_segments": [
            {
                "text": " ".join(str(w.get("word", "")) for w in words),
                "start": words[0].get("start", 0.0) if words else 0.0,
                "end": words[-1].get("end", 0.0) if words else 0.0,
                "words": words,
            }
        ],
    }


def _write_tone_of_duration(path: Path, duration_sec: float, sr: int = 24000) -> None:
    """Write a continuous sine tone of the exact requested duration."""
    t = np.linspace(0, duration_sec, int(sr * duration_sec), dtype=np.float32)
    tone = 0.5 * np.sin(2 * np.pi * 440 * t)
    sf.write(str(path), tone, sr, subtype="PCM_16")


class TestEndingArtifactVerifier:
    def test_fails_elongated_last_word(self):
        from pipeline.quality_layers import EndingArtifactVerifier

        # Six words; median duration 0.2s, last word 0.5s -> 2.5x ratio.
        words = [
            {"word": "一", "start": 0.0, "end": 0.2, "score": 0.95},
            {"word": "二", "start": 0.2, "end": 0.4, "score": 0.95},
            {"word": "三", "start": 0.4, "end": 0.6, "score": 0.95},
            {"word": "四", "start": 0.6, "end": 0.8, "score": 0.95},
            {"word": "五", "start": 0.8, "end": 1.0, "score": 0.95},
            {"word": "六", "start": 1.0, "end": 1.5, "score": 0.95},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "elongated.wav"
            _write_tone_of_duration(path, 1.5)
            verifier = EndingArtifactVerifier()
            context = VerificationContext(
                whisperx_result=_make_ending_artifact_whisperx_result(words)
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="reference text",
                reference_voice_path=None,
                language="zh",
                expected_duration=1.5,
                context=context,
            )
            assert result.passed is False
            assert result.failure_reason == "last_word_too_long"
            assert result.metrics["last_word_ratio"] == 2.5

    def test_fails_loud_trailing_audio(self):
        from pipeline.quality_layers import EndingArtifactVerifier

        # Clean words ending at 1.0s, but audio continues with audible noise for
        # 0.8s. The combined duration + tail-loudness check must flag it.
        words = [
            {"word": "一", "start": 0.0, "end": 0.2, "score": 0.95},
            {"word": "二", "start": 0.2, "end": 0.4, "score": 0.95},
            {"word": "三", "start": 0.4, "end": 0.6, "score": 0.95},
            {"word": "四", "start": 0.6, "end": 0.8, "score": 0.95},
            {"word": "五", "start": 0.8, "end": 1.0, "score": 0.95},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trailing.wav"
            sr = 24000
            tone = 0.5 * np.sin(2 * np.pi * 440 * np.linspace(0, 1.0, sr, dtype=np.float32))
            # Audible noise tail (~-30 dBFS) after the last word.
            np.random.seed(42)
            noise = np.random.normal(0.0, 0.03, int(sr * 0.8)).astype(np.float32)
            sf.write(str(path), np.concatenate([tone, noise]), sr, subtype="PCM_16")

            verifier = EndingArtifactVerifier()
            context = VerificationContext(
                whisperx_result=_make_ending_artifact_whisperx_result(words)
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="reference text",
                reference_voice_path=None,
                language="zh",
                expected_duration=1.8,
                context=context,
            )
            assert result.passed is False
            assert result.failure_reason == "trailing_audio_too_long"
            assert result.metrics["trailing_audio_sec"] > 0.7
            assert result.metrics["trailing_tail_rms_db"] > -45.0

    def test_passes_clean_ending(self):
        from pipeline.quality_layers import EndingArtifactVerifier

        # Six words of equal duration; audio ends with the last word.
        words = [
            {"word": "一", "start": 0.0, "end": 0.2, "score": 0.95},
            {"word": "二", "start": 0.2, "end": 0.4, "score": 0.95},
            {"word": "三", "start": 0.4, "end": 0.6, "score": 0.95},
            {"word": "四", "start": 0.6, "end": 0.8, "score": 0.95},
            {"word": "五", "start": 0.8, "end": 1.0, "score": 0.95},
            {"word": "六", "start": 1.0, "end": 1.2, "score": 0.95},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean.wav"
            _write_tone_of_duration(path, 1.2)
            verifier = EndingArtifactVerifier()
            context = VerificationContext(
                whisperx_result=_make_ending_artifact_whisperx_result(words)
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="reference text",
                reference_voice_path=None,
                language="zh",
                expected_duration=1.2,
                context=context,
            )
            assert result.passed is True
            assert result.failure_reason is None
            assert result.metrics["last_word_ratio"] == 1.0

    def test_skips_short_sentences(self):
        from pipeline.quality_layers import EndingArtifactVerifier

        # Only three words; median-duration check is skipped, so even a huge
        # last-word ratio must not fail the segment.
        words = [
            {"word": "短", "start": 0.0, "end": 0.2, "score": 0.95},
            {"word": "句", "start": 0.2, "end": 0.4, "score": 0.95},
            {"word": "子", "start": 0.4, "end": 1.0, "score": 0.95},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.wav"
            _write_tone_of_duration(path, 1.0)
            verifier = EndingArtifactVerifier()
            context = VerificationContext(
                whisperx_result=_make_ending_artifact_whisperx_result(words)
            )
            result = verifier.verify(
                audio_path=str(path),
                original_text="reference text",
                reference_voice_path=None,
                language="zh",
                expected_duration=1.0,
                context=context,
            )
            assert result.passed is True
            assert result.failure_reason is None
            assert result.metrics["meaningful_word_count"] == 3

    def test_whisperx_unavailable_without_context(self):
        from pipeline.quality_layers import EndingArtifactVerifier

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            _write_test_tone(str(path))
            verifier = EndingArtifactVerifier()
            result = verifier.verify(
                audio_path=str(path),
                original_text="reference text",
                reference_voice_path=None,
                language="zh",
                expected_duration=1.0,
                context=None,
            )
            assert result.passed is False
            assert result.failure_reason == "whisperx_unavailable"

    def test_segment_147_seed888_fails_seed891_passes(self):
        """Regression test using the real 147.wav variants if they exist."""
        from pipeline.quality_layers import EndingArtifactVerifier

        base_dir = Path("outputs/pipeline_jobs/pipe-67880bdb6dee_internet-dialogue-zh-cn/segments")
        bad_path = base_dir / "147.wav"
        good_path = base_dir / "147_variant_seed891.wav"

        if not bad_path.exists() or not good_path.exists():
            pytest.skip("segment 147 variant files not available")

        verifier = EndingArtifactVerifier()

        # Seed 888: last word "性" is ~0.342s vs median ~0.18s -> ratio ~1.9x.
        bad_words = [
            {"word": "實", "start": 0.252, "end": 0.433, "score": 1.0},
            {"word": "際", "start": 0.433, "end": 0.594, "score": 0.998},
            {"word": "上", "start": 0.594, "end": 0.795, "score": 1.0},
            {"word": ",", "start": 0.795, "end": 0.935, "score": 0.857},
            {"word": "很", "start": 0.935, "end": 1.156, "score": 1.0},
            {"word": "多", "start": 1.156, "end": 1.317, "score": 1.0},
            {"word": "應", "start": 1.317, "end": 1.458, "score": 0.999},
            {"word": "用", "start": 1.458, "end": 1.699, "score": 1.0},
            {"word": "會", "start": 1.699, "end": 1.819, "score": 1.0},
            {"word": "在", "start": 1.819, "end": 2.041, "score": 1.0},
            {"word": "U", "start": 2.041, "end": 2.221, "score": 0.889},
            {"word": "T", "start": 2.221, "end": 2.382, "score": 0.875},
            {"word": "P", "start": 2.382, "end": 2.543, "score": 0.875},
            {"word": "之", "start": 2.543, "end": 2.684, "score": 0.963},
            {"word": "上", "start": 2.684, "end": 2.945, "score": 0.999},
            {"word": "自", "start": 2.945, "end": 3.106, "score": 0.999},
            {"word": "己", "start": 3.106, "end": 3.286, "score": 0.998},
            {"word": "實", "start": 3.286, "end": 3.487, "score": 0.997},
            {"word": "現", "start": 3.487, "end": 3.688, "score": 0.967},
            {"word": "可", "start": 3.688, "end": 3.849, "score": 0.999},
            {"word": "靠", "start": 3.849, "end": 4.07, "score": 0.997},
            {"word": "性", "start": 4.07, "end": 4.412, "score": 0.942},
            {"word": "。", "start": 4.412, "end": 4.432, "score": 0.754},
        ]
        bad_context = VerificationContext(
            whisperx_result=_make_ending_artifact_whisperx_result(bad_words)
        )
        bad_result = verifier.verify(
            audio_path=str(bad_path),
            original_text="实际上，很多应用会在UDP之上自己实现可靠性。",
            reference_voice_path=None,
            language="zh",
            expected_duration=4.5,
            context=bad_context,
        )
        assert bad_result.passed is False
        assert bad_result.failure_reason == "last_word_too_long"
        assert bad_result.metrics["last_word_ratio"] > 1.5

        # Seed 891: last word "性" is short, trailing audio is minimal.
        good_words = [
            {"word": "實", "start": 0.312, "end": 0.492, "score": 0.999},
            {"word": "際", "start": 0.492, "end": 0.653, "score": 0.999},
            {"word": "上", "start": 0.653, "end": 0.833, "score": 1.0},
            {"word": ",", "start": 0.833, "end": 1.174, "score": 0.941},
            {"word": "很", "start": 1.174, "end": 1.375, "score": 1.0},
            {"word": "多", "start": 1.375, "end": 1.536, "score": 1.0},
            {"word": "應", "start": 1.536, "end": 1.696, "score": 0.999},
            {"word": "用", "start": 1.696, "end": 1.897, "score": 1.0},
            {"word": "會", "start": 1.897, "end": 2.057, "score": 1.0},
            {"word": "在", "start": 2.057, "end": 2.278, "score": 1.0},
            {"word": "物", "start": 2.278, "end": 2.478, "score": 0.91},
            {"word": "體", "start": 2.478, "end": 2.639, "score": 0.943},
            {"word": "批", "start": 2.639, "end": 2.779, "score": 0.939},
            {"word": "置", "start": 2.779, "end": 2.92, "score": 0.857},
            {"word": "上", "start": 2.92, "end": 3.241, "score": 1.0},
            {"word": "自", "start": 3.241, "end": 3.381, "score": 0.988},
            {"word": "己", "start": 3.381, "end": 3.562, "score": 0.999},
            {"word": "實", "start": 3.562, "end": 3.742, "score": 0.998},
            {"word": "現", "start": 3.742, "end": 3.963, "score": 0.963},
            {"word": "可", "start": 3.963, "end": 4.123, "score": 0.976},
            {"word": "靠", "start": 4.123, "end": 4.364, "score": 0.991},
            {"word": "性", "start": 4.364, "end": 4.384, "score": 0.026},
        ]
        good_context = VerificationContext(
            whisperx_result=_make_ending_artifact_whisperx_result(good_words)
        )
        good_result = verifier.verify(
            audio_path=str(good_path),
            original_text="实际上，很多应用会在UDP之上自己实现可靠性。",
            reference_voice_path=None,
            language="zh",
            expected_duration=5.0,
            context=good_context,
        )
        assert good_result.passed is True
        assert good_result.failure_reason is None
        # Seed 891 has a ~0.7s fade-out but the tail is quiet (< -40 dB).
        assert good_result.metrics["trailing_tail_rms_db"] < -40.0
