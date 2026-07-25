# Tests for pipeline.jobs happy path
# Plain assert style.

import sys
import tempfile
import shutil
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import soundfile as sf

from pipeline.jobs import PipelineService
from pipeline.models import PipelineJobStatus, SegmentStatus


def _make_fake_synthesize(sample_rate=24000):
    calls = []

    def fake_synthesize(text, audio_prompt_path=None, temperature=0.8, exaggeration=0.5,
                        cfg_weight=0.5, seed=0, language="en"):
        calls.append({
            "text": text,
            "audio_prompt_path": audio_prompt_path,
            "temperature": temperature,
            "exaggeration": exaggeration,
            "cfg_weight": cfg_weight,
            "seed": seed,
            "language": language,
        })
        words = max(1, len(text.split()))
        duration = words / 3.0
        samples = np.full(int(duration * sample_rate), 0.3, dtype=np.float32)
        return samples, sample_rate

    return fake_synthesize, calls


def test_submit_job_returns_id_and_persists():
    tmp = tempfile.mkdtemp()
    try:
        fake, calls = _make_fake_synthesize()
        service = PipelineService(base_dir=Path(tmp), synthesize_fn=fake)
        job_id = service.submit_job(
            text="Hello world. How are you?",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        assert job_id
        job = service.get_job(job_id)
        assert job is not None
        assert job.status in (PipelineJobStatus.PENDING, PipelineJobStatus.RUNNING, PipelineJobStatus.DONE)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_job_happy_path_composes_final():
    tmp = tempfile.mkdtemp()
    try:
        fake, calls = _make_fake_synthesize()
        service = PipelineService(base_dir=Path(tmp), synthesize_fn=fake)
        job_id = service.submit_job(
            text="Hello. World.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        service.run_job_sync(job_id, max_segment_duration=50.0, pause_ms=100)
        job = service.get_job(job_id)
        assert job.status == PipelineJobStatus.DONE, job.status
        assert job.final_audio_path is not None
        assert Path(job.final_audio_path).exists()
        # Two very short sentences are merged into one segment.
        assert len(calls) >= 1, calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_job_preserves_segments():
    tmp = tempfile.mkdtemp()
    try:
        fake, calls = _make_fake_synthesize()
        service = PipelineService(base_dir=Path(tmp), synthesize_fn=fake)
        job_id = service.submit_job(
            text="Sentence one is quite long and should stand alone. "
                 "Sentence two is also long enough to be its own segment. "
                 "Sentence three is similarly lengthy and will not be merged.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en"},
        )
        service.run_job_sync(job_id, max_segment_duration=3.0, pause_ms=100)
        job = service.get_job(job_id)
        assert len(job.segments) == 3, job.segments
        for seg in job.segments:
            assert seg.status == SegmentStatus.PASSED
            assert seg.audio_path is not None
            assert Path(seg.audio_path).exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_list_jobs():
    tmp = tempfile.mkdtemp()
    try:
        fake, _ = _make_fake_synthesize()
        service = PipelineService(base_dir=Path(tmp), synthesize_fn=fake)
        ids = []
        for i in range(3):
            jid = service.submit_job(
                text=f"Job {i}.",
                voice_config={"mode": "predefined", "voice_id": "test.wav"},
                gen_params={"temperature": 0.8, "language": "en"},
            )
            ids.append(jid)
        summaries = service.list_jobs()
        assert len(summaries) == 3
        assert set(ids) == {s.job_id for s in summaries}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_submit_job_returns_id_and_persists()
    test_run_job_happy_path_composes_final()
    test_run_job_preserves_segments()
    test_list_jobs()
    print("ALL JOBS TESTS PASSED")
