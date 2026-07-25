# Tests for pipeline ASR integration (ticket 03).
# Plain assert style.

import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from pipeline.asr import ASRVerifier, normalize_text, compute_similarity
from pipeline.jobs import PipelineService
from pipeline.models import PipelineJobStatus, SegmentStatus
from pipeline.quality import QualityVerificationResult


def test_normalize_text_removes_punctuation():
    assert normalize_text("Hello, World!") == "hello world"
    assert normalize_text("這是，測試。", "zh") == "這是測試"


def test_compute_similarity_identical():
    assert compute_similarity("hello world", "hello world") == 1.0


def test_compute_similarity_empty():
    assert compute_similarity("", "") == 1.0
    assert compute_similarity("hello", "") == 0.0


def test_asr_verifier_disabled_when_whisperx_missing(monkeypatch):
    monkeypatch.setattr("pipeline.asr.WHISPERX_AVAILABLE", False)
    verifier = ASRVerifier()
    result = verifier.verify("/fake/path.wav", "hello world")
    assert result.passed
    assert result.similarity == 1.0


def _make_fake_asr(similarity: float, passed: bool):
    class FakeASR:
        def verify(self, audio_path, original_text, language="en"):
            from pipeline.asr import ASRVerificationResult
            return ASRVerificationResult(
                passed=passed,
                similarity=similarity,
                transcription=original_text,
                failure_reason=None if passed else "asr_mismatch",
                metrics={},
            )
    return FakeASR()


def _make_passing_quality_verifier():
    """Return a quality verifier that always passes, for ASR-focused tests."""

    class PassingQualityVerifier:
        def verify(self, **kwargs):
            return QualityVerificationResult(
                passed=True,
                overall_score=1.0,
                failure_reason=None,
                layer_results={},
            )

    return PassingQualityVerifier()


def test_asr_failure_triggers_retry_and_eventual_pass():
    tmp = tempfile.mkdtemp()
    try:
        calls = []

        def fake_synthesize(text, audio_prompt_path=None, **kwargs):
            calls.append(text)
            words = max(1, len(text.split()))
            duration = words / 3.0
            samples = np.full(int(duration * 24000), 0.3, dtype=np.float32)
            return samples, 24000

        asr = _make_fake_asr(similarity=0.5, passed=False)
        service = PipelineService(
            base_dir=Path(tmp),
            synthesize_fn=fake_synthesize,
            asr_verifier=asr,
            quality_verifier=_make_passing_quality_verifier(),
        )
        job_id = service.submit_job(
            text="This is a reasonably long sentence to avoid merging.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        # Force one segment.
        service.run_job_sync(job_id, max_segment_duration=50.0, pause_ms=100)
        job = service.get_job(job_id)
        # ASR always fails, so segment ultimately fails and job fails.
        assert job.status == PipelineJobStatus.FAILED
        assert job.segments[0].status == SegmentStatus.FAILED
        # asr_score is only recorded on passing attempts.
        assert job.segments[0].asr_score is None
        # Last verification log entry records the ASR failure.
        last_log = job.segments[0].verification_log[-1]
        assert last_log["asr"]["similarity"] == 0.5
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_asr_pass_composes_final():
    tmp = tempfile.mkdtemp()
    try:
        def fake_synthesize(text, audio_prompt_path=None, **kwargs):
            words = max(1, len(text.split()))
            duration = words / 3.0
            samples = np.full(int(duration * 24000), 0.3, dtype=np.float32)
            return samples, 24000

        asr = _make_fake_asr(similarity=0.9, passed=True)
        service = PipelineService(
            base_dir=Path(tmp),
            synthesize_fn=fake_synthesize,
            asr_verifier=asr,
            quality_verifier=_make_passing_quality_verifier(),
        )
        job_id = service.submit_job(
            text="This is a reasonably long sentence to avoid merging.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        service.run_job_sync(job_id, max_segment_duration=50.0, pause_ms=100)
        job = service.get_job(job_id)
        assert job.status == PipelineJobStatus.DONE
        assert job.segments[0].status == SegmentStatus.PASSED
        assert job.segments[0].asr_score == 0.9
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_normalize_text_removes_punctuation()
    test_compute_similarity_identical()
    test_compute_similarity_empty()
    test_asr_verifier_disabled_when_whisperx_missing()
    test_asr_failure_triggers_retry_and_eventual_pass()
    test_asr_pass_composes_final()
    print("ALL ASR TESTS PASSED")
