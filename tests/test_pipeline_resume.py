# Tests for resume / reconcile / idempotency / split-at-submit.
# Plain assert style, matching the existing pipeline test suite.

import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import soundfile as sf

from pipeline.jobs import PipelineService
from pipeline.models import PipelineJobStatus, SegmentStatus
from pipeline.quality import QualityVerificationResult


def _make_passing_quality_verifier():
    class PassingQualityVerifier:
        def verify(self, **kwargs):
            return QualityVerificationResult(
                passed=True,
                overall_score=1.0,
                failure_reason=None,
                layer_results={},
            )

    return PassingQualityVerifier()


def _make_fake_synthesize(sample_rate=24000):
    calls = []

    def fake_synthesize(text, audio_prompt_path=None, temperature=0.8, exaggeration=0.5,
                        cfg_weight=0.5, seed=0, language="en"):
        calls.append({
            "text": text,
            "temperature": temperature,
            "exaggeration": exaggeration,
            "cfg_weight": cfg_weight,
            "seed": seed,
            "language": language,
        })
        words = max(1, len(text.split()))
        duration = max(2.0, words / 3.0)
        samples_count = int(duration * sample_rate)
        t = np.linspace(0, duration, samples_count, dtype=np.float32)
        envelope = 0.25 + 0.15 * np.sin(2 * np.pi * 2 * t)
        samples = envelope * np.sin(2 * np.pi * 440 * t)
        samples = samples.astype(np.float32)
        return samples, sample_rate

    return fake_synthesize, calls


def _new_service(tmp, synth, calls=None):
    return PipelineService(
        base_dir=Path(tmp),
        synthesize_fn=synth,
        quality_verifier=_make_passing_quality_verifier(),
    )


def _seg_path(service, job, idx):
    return service._job_dir(job) / "segments" / f"{idx}.wav"


def _write_pass(service, job, seg):
    """Synthesize real audio for a segment and mark it PASSED, simulating a
    segment that completed before an (imaginary) crash."""
    samples, sr = service.synthesize_fn(seg.text)
    p = _seg_path(service, job, seg.index)
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), samples, sr, subtype="pcm_16")
    seg.status = SegmentStatus.PASSED
    seg.audio_path = str(p)


def test_submit_splits_and_persists_segments():
    tmp = tempfile.mkdtemp()
    try:
        synth, calls = _make_fake_synthesize()
        service = _new_service(tmp, synth)
        job_id = service.submit_job(
            text="Hello world. How are you? Let's go.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        job = service.get_job(job_id)
        assert job is not None
        assert job.status == PipelineJobStatus.PENDING
        assert len(job.segments) >= 2, "multi-sentence text should split into segments"
        assert all(s.status == SegmentStatus.PENDING for s in job.segments)
        assert all(s.gen_params.get("language") == "en" for s in job.segments)
        # No generation happened yet at submit time.
        assert calls == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reconcile_orphans_resets_running_to_pending():
    tmp = tempfile.mkdtemp()
    try:
        synth, calls = _make_fake_synthesize()
        service = _new_service(tmp, synth)
        job_id = service.submit_job(
            text="Hello world. How are you?",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )
        job = service.get_job(job_id)
        # Simulate a crash mid-run: some segments already passed, job stuck RUNNING.
        for seg in job.segments[:1]:
            _write_pass(service, job, seg)
        job.status = PipelineJobStatus.RUNNING
        service.store.save(job)

        reclaimed = service.reconcile_orphans()
        assert reclaimed == 1

        job2 = service.get_job(job_id)
        assert job2.status == PipelineJobStatus.PENDING
        assert job2.segments[0].status == SegmentStatus.PASSED, "passed segments preserved"
        assert all(s.status == SegmentStatus.PENDING for s in job2.segments[1:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resume_skips_passed_segments_and_completes():
    tmp = tempfile.mkdtemp()
    try:
        synth, calls = _make_fake_synthesize()
        service = _new_service(tmp, synth)
        job_id = service.submit_job(
            text="One sentence here. Second sentence here. Third one. Fourth.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )
        job = service.get_job(job_id)
        n = len(job.segments)
        assert n >= 3

        # Simulate a crash after the first segment completed: mark seg 0 passed,
        # leave the rest pending, job stuck RUNNING.
        _write_pass(service, job, job.segments[0])
        job.status = PipelineJobStatus.RUNNING
        service.store.save(job)

        # Recover: reconcile the orphan, then the worker would run it.
        service.reconcile_orphans()
        calls_before = len(calls)
        service.run_job_sync(job_id, pause_ms=10)

        job = service.get_job(job_id)
        assert job.status == PipelineJobStatus.DONE
        assert all(s.status == SegmentStatus.PASSED for s in job.segments)
        assert job.final_audio_path and Path(job.final_audio_path).exists()

        # Only the non-passed segments (n-1) were regenerated.
        regenerated = len(calls) - calls_before
        assert regenerated == n - 1, f"expected {n-1} regenerations, got {regenerated}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_passed_segment_with_missing_file_is_reset():
    tmp = tempfile.mkdtemp()
    try:
        synth, calls = _make_fake_synthesize()
        service = _new_service(tmp, synth)
        job_id = service.submit_job(
            text="Hello world. How are you?",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )
        job = service.get_job(job_id)
        seg = job.segments[0]
        # Mark PASSED but point at a file that does not exist (e.g. wiped disk).
        seg.status = SegmentStatus.PASSED
        seg.audio_path = str(_seg_path(service, job, seg.index))
        job.status = PipelineJobStatus.PENDING
        service.store.save(job)

        service._reconcile_segment_state(seg)
        assert seg.status == SegmentStatus.PENDING, "missing file must invalidate PASSED"
        assert seg.audio_path is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_job_sync_reruns_entire_job_to_done():
    tmp = tempfile.mkdtemp()
    try:
        synth, calls = _make_fake_synthesize()
        service = _new_service(tmp, synth)
        job_id = service.submit_job(
            text="Hello world. How are you?",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )
        service.run_job_sync(job_id, pause_ms=10)
        job = service.get_job(job_id)
        assert job.status == PipelineJobStatus.DONE
        assert all(s.status == SegmentStatus.PASSED for s in job.segments)
        assert job.final_audio_path and Path(job.final_audio_path).exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
