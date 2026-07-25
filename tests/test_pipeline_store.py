# Tests for pipeline.store
# Plain assert style.

import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.store import JobStore
from pipeline.models import PipelineJob, Segment, SegmentStatus, PipelineJobStatus


def test_create_job_persists_atomic():
    tmp = tempfile.mkdtemp()
    try:
        store = JobStore(base_dir=Path(tmp))
        job = PipelineJob(
            job_id="job-1",
            text="hello world",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8},
            status=PipelineJobStatus.PENDING,
            segments=[],
        )
        store.save(job)
        loaded = store.load("job-1")
        assert loaded is not None
        assert loaded.job_id == "job-1"
        assert loaded.text == "hello world"
        assert loaded.status == PipelineJobStatus.PENDING
        assert loaded.segments == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_update_and_reload():
    tmp = tempfile.mkdtemp()
    try:
        store = JobStore(base_dir=Path(tmp))
        job = PipelineJob(
            job_id="job-2",
            text="hello",
            voice_config={},
            gen_params={},
            status=PipelineJobStatus.PENDING,
            segments=[],
        )
        store.save(job)
        job.status = PipelineJobStatus.RUNNING
        store.save(job)
        loaded = store.load("job-2")
        assert loaded.status == PipelineJobStatus.RUNNING
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_list_jobs_sorted():
    tmp = tempfile.mkdtemp()
    try:
        store = JobStore(base_dir=Path(tmp))
        for jid in ["job-c", "job-a", "job-b"]:
            job = PipelineJob(
                job_id=jid,
                text="x",
                voice_config={},
                gen_params={},
                status=PipelineJobStatus.PENDING,
                segments=[],
            )
            store.save(job)
        ids = [j.job_id for j in store.list_jobs()]
        assert ids == sorted(ids), ids
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_missing_returns_none():
    tmp = tempfile.mkdtemp()
    try:
        store = JobStore(base_dir=Path(tmp))
        assert store.load("missing") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_save_with_segments():
    tmp = tempfile.mkdtemp()
    try:
        store = JobStore(base_dir=Path(tmp))
        job = PipelineJob(
            job_id="job-3",
            text="hello world",
            voice_config={},
            gen_params={},
            status=PipelineJobStatus.RUNNING,
            segments=[
                Segment(
                    index=0,
                    text="hello",
                    status=SegmentStatus.PENDING,
                    audio_path=None,
                    score=None,
                    failure_reason=None,
                    retry_count=0,
                    gen_params={},
                )
            ],
        )
        store.save(job)
        loaded = store.load("job-3")
        assert len(loaded.segments) == 1
        assert loaded.segments[0].text == "hello"
        assert loaded.segments[0].status == SegmentStatus.PENDING
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_create_job_persists_atomic()
    test_update_and_reload()
    test_list_jobs_sorted()
    test_load_missing_returns_none()
    test_save_with_segments()
    print("ALL STORE TESTS PASSED")
