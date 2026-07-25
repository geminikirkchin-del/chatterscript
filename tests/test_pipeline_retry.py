# Tests for pipeline retry logic (ticket 02).
# Plain assert style.

import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from pipeline.jobs import PipelineService
from pipeline.models import PipelineJobStatus, SegmentStatus
from pipeline.quality import QualityVerificationResult


def _make_passing_quality_verifier():
    """Return a quality verifier that always passes, for retry tests."""

    class PassingQualityVerifier:
        def verify(self, **kwargs):
            return QualityVerificationResult(
                passed=True,
                overall_score=1.0,
                failure_reason=None,
                layer_results={},
            )

    return PassingQualityVerifier()


def _make_synthesize_with_failures(fail_indices, sample_rate=24000):
    calls = []

    def fake_synthesize(text, audio_prompt_path=None, **kwargs):
        calls.append({"text": text, "audio_prompt_path": audio_prompt_path, **kwargs})
        idx = len(calls) - 1
        if idx in fail_indices:
            return None, None
        # Generate audio duration roughly matching word count to pass duration check.
        words = max(1, len(text.split()))
        duration = max(2.0, words / 3.0)
        samples_count = int(duration * sample_rate)
        t = np.linspace(0, duration, samples_count, dtype=np.float32)
        # Amplitude-modulated tone so the audio metrics verifier sees realistic LUFS/dynamic range.
        envelope = 0.25 + 0.15 * np.sin(2 * np.pi * 2 * t)
        samples = envelope * np.sin(2 * np.pi * 440 * t)
        samples = samples.astype(np.float32)
        return samples, sample_rate

    return fake_synthesize, calls


def test_failed_segment_retried_then_passes():
    tmp = tempfile.mkdtemp()
    try:
        # First call fails, subsequent calls succeed.
        fake, calls = _make_synthesize_with_failures(fail_indices={0})
        service = PipelineService(
            base_dir=Path(tmp),
            synthesize_fn=fake,
            quality_verifier=_make_passing_quality_verifier(),
        )
        # Use a longer sentence so the fake synthesized audio aligns with the
        # estimated duration and passes the audio-metrics checks.
        job_id = service.submit_job(
            text="This is a longer test sentence that will be synthesized successfully after one retry.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        service.run_job_sync(job_id, max_segment_duration=3.0, pause_ms=100)
        job = service.get_job(job_id)
        assert job.status == PipelineJobStatus.DONE, job.status
        # One merged segment failed once then succeeded on retry.
        assert len(calls) >= 2, calls
        assert job.segments[0].status == SegmentStatus.PASSED
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_permanent_failure_excluded_from_final():
    tmp = tempfile.mkdtemp()
    try:
        # All calls fail for segment 0.
        fake, calls = _make_synthesize_with_failures(fail_indices={0, 1, 2, 3, 4})
        service = PipelineService(
            base_dir=Path(tmp),
            synthesize_fn=fake,
            quality_verifier=_make_passing_quality_verifier(),
        )
        job_id = service.submit_job(
            text="Good sentence that is long enough. Bad sentence that is long enough.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        service.run_job_sync(job_id, max_segment_duration=3.0, pause_ms=100)
        job = service.get_job(job_id)
        # One segment passes, one fails; final should still compose.
        assert job.status == PipelineJobStatus.DONE, job.status
        assert job.final_audio_path is not None
        statuses = {seg.status for seg in job.segments}
        assert SegmentStatus.PASSED in statuses
        assert SegmentStatus.FAILED in statuses
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_retry_count_recorded():
    tmp = tempfile.mkdtemp()
    try:
        fake, calls = _make_synthesize_with_failures(fail_indices={0, 1})
        service = PipelineService(
            base_dir=Path(tmp),
            synthesize_fn=fake,
            quality_verifier=_make_passing_quality_verifier(),
        )
        job_id = service.submit_job(
            text="One sentence only.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        service.run_job_sync(job_id, max_segment_duration=3.0, pause_ms=100)
        job = service.get_job(job_id)
        assert job.segments[0].retry_count >= 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_failed_segment_retried_then_passes()
    test_permanent_failure_excluded_from_final()
    test_retry_count_recorded()
    print("ALL RETRY TESTS PASSED")
