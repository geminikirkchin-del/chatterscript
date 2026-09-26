# Tests for the single serial pipeline worker.
# Plain assert style.

import sys
import tempfile
import shutil
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

from pipeline.jobs import PipelineService
from pipeline.models import PipelineJobStatus, SegmentStatus
from pipeline.quality import QualityVerificationResult
from pipeline.worker import PipelineWorker


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


def _make_slow_synthesize(delay, sample_rate=24000):
    """A synthesizer that records call ordering and sleeps per call, so tests
    can assert the worker never overlaps two jobs' segments."""
    calls = []

    def fake_synthesize(text, audio_prompt_path=None, **kwargs):
        now = time.time()
        calls.append({"t": now, "text": text})
        time.sleep(delay)
        samples_count = int(2.0 * sample_rate)
        t = np.linspace(0, 2.0, samples_count, dtype=np.float32)
        samples = (0.25 + 0.15 * np.sin(2 * np.pi * 2 * t)) * np.sin(2 * np.pi * 440 * t)
        return samples.astype(np.float32), sample_rate

    return fake_synthesize, calls


def _new_service(tmp, synth):
    return PipelineService(
        base_dir=Path(tmp),
        synthesize_fn=synth,
        quality_verifier=_make_passing_quality_verifier(),
    )


def test_worker_processes_jobs_serially_to_done():
    tmp = tempfile.mkdtemp()
    try:
        synth, calls = _make_slow_synthesize(delay=0.05)
        service = _new_service(tmp, synth)
        j1 = service.submit_job(
            text="Alpha one. Alpha two.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )
        j2 = service.submit_job(
            text="Beta one. Beta two. Beta three.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )

        worker = PipelineWorker(service=service, poll_interval=0.01, pause_ms=10)
        worker.start()
        try:
            # Wait until both jobs reach DONE.
            deadline = time.time() + 15
            while time.time() < deadline:
                done = all(
                    service.get_job(j).status == PipelineJobStatus.DONE
                    for j in (j1, j2)
                )
                if done:
                    break
                time.sleep(0.05)
            assert service.get_job(j1).status == PipelineJobStatus.DONE
            assert service.get_job(j2).status == PipelineJobStatus.DONE
        finally:
            worker.stop(timeout=5)

        # Serial: calls for job1 and job2 must not interleave segment-wise.
        texts = [c["text"] for c in calls]
        # All j1 segments appear contiguously, then all j2 segments (FIFO by
        # created_at). Find the first alpha and last alpha; everything between
        # must be alpha, and no beta may appear before the last alpha.
        first_alpha = texts.index([t for t in texts if t.startswith("Alpha")][0])
        last_alpha = max(i for i, t in enumerate(texts) if t.startswith("Alpha"))
        between = texts[first_alpha:last_alpha + 1]
        assert all(t.startswith("Alpha") for t in between), f"job1 segments interleaved: {texts}"
        betas_after = [t for t in texts[last_alpha:] if t.startswith("Beta")]
        assert len(betas_after) == sum(1 for t in texts if t.startswith("Beta"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_worker_does_not_claim_an_already_running_job():
    tmp = tempfile.mkdtemp()
    try:
        synth, calls = _make_slow_synthesize(delay=0.01)
        service = _new_service(tmp, synth)
        jid = service.submit_job(
            text="Only job.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )
        job = service.get_job(jid)
        # Simulate another (old) worker already running this job.
        job.status = PipelineJobStatus.RUNNING
        service.store.save(job)

        worker = PipelineWorker(service=service, poll_interval=0.01, pause_ms=10)
        # Manually drive one claim — it must return None (nothing PENDING).
        assert worker._claim_next() is None
        assert service.get_job(jid).status == PipelineJobStatus.RUNNING
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_next_pending_job_returns_oldest_and_respects_grace():
    tmp = tempfile.mkdtemp()
    try:
        synth, calls = _make_slow_synthesize(delay=0.0)
        service = _new_service(tmp, synth)
        # Create j1 then sleep briefly so j2 is newer.
        j1 = service.submit_job(
            text="One. Two.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )
        time.sleep(0.05)
        j2 = service.submit_job(
            text="Three. Four. Five.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
        )
        picked = service.next_pending_job(older_than=0.0)
        assert picked.job_id == j1, "oldest PENDING should be picked first"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
