# Tests for pipeline.polish (post-generation audio polish).
# Plain assert style.

import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import soundfile as sf

from pipeline.polish import polish_segment_audio

SR = 24000


def _tone(duration: float, amp: float = 0.4) -> np.ndarray:
    t = np.arange(int(duration * SR), dtype=np.float32) / SR
    return amp * np.sin(2 * np.pi * 440 * t)


def _silence(duration: float) -> np.ndarray:
    return np.zeros(int(duration * SR), dtype=np.float32)


def _write(path: Path, audio: np.ndarray):
    sf.write(str(path), audio, SR, subtype="pcm_16")


def _silent_runs_sec(audio: np.ndarray, threshold_db: float = -40.0):
    """Simple silence run detector for assertions (50ms frames)."""
    frame = int(SR * 0.05)
    n = len(audio) // frame
    if n == 0:
        return []
    frames = audio[: n * frame].astype(np.float64).reshape(n, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-10))
    runs = []
    i = 0
    while i < n:
        if db[i] >= threshold_db:
            i += 1
            continue
        j = i
        while j < n and db[j] < threshold_db:
            j += 1
        runs.append((i * 0.05, j * 0.05))
        i = j
    return runs


def test_long_pause_compressed_to_target():
    tmp = tempfile.mkdtemp()
    try:
        audio = np.concatenate([_tone(0.5), _silence(1.2), _tone(0.5)])
        path = Path(tmp) / "seg.wav"
        _write(path, audio)

        report = polish_segment_audio(str(path), denoise=False)

        out, _ = sf.read(str(path), dtype="float32")
        runs = _silent_runs_sec(out)
        assert len(runs) == 1, runs
        start, end = runs[0]
        assert abs((end - start) - 0.35) < 0.1, f"pause should be ~0.35s, got {end-start}"
        assert len(report["pauses_compressed"]) == 1
        assert abs(report["pauses_compressed"][0]["original_sec"] - 1.2) < 0.1
        # Total: 0.5 + 0.35 + 0.5 = ~1.35s
        assert abs(len(out) / SR - 1.35) < 0.1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_head_and_tail_silence_trimmed():
    tmp = tempfile.mkdtemp()
    try:
        audio = np.concatenate([_silence(1.0), _tone(0.6), _silence(0.9)])
        path = Path(tmp) / "seg.wav"
        _write(path, audio)

        report = polish_segment_audio(str(path), denoise=False)

        out, _ = sf.read(str(path), dtype="float32")
        # Total: ~0.2 head + 0.6 tone + ~0.2 tail = ~1.0s
        assert abs(len(out) / SR - 1.0) < 0.15, len(out) / SR
        assert report["head_trimmed_sec"] > 0.7
        assert report["tail_trimmed_sec"] > 0.6
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_natural_pauses_untouched():
    tmp = tempfile.mkdtemp()
    try:
        audio = np.concatenate([_tone(0.4), _silence(0.3), _tone(0.4)])
        path = Path(tmp) / "seg.wav"
        _write(path, audio)

        report = polish_segment_audio(str(path), denoise=False)

        out, _ = sf.read(str(path), dtype="float32")
        assert abs(len(out) / SR - 1.1) < 0.05, "natural 0.3s pause must not be touched"
        assert report["pauses_compressed"] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_denoise_reduces_noise_floor():
    tmp = tempfile.mkdtemp()
    try:
        rng = np.random.default_rng(42)
        noise_bed = 0.02 * rng.standard_normal(int(1.6 * SR)).astype(np.float32)
        audio = np.concatenate([_silence(0.3), _tone(1.0), _silence(0.3)]) + noise_bed
        audio = np.clip(audio, -1.0, 1.0).astype(np.float32)
        path = Path(tmp) / "seg.wav"
        _write(path, audio)

        report = polish_segment_audio(str(path), denoise=True)

        assert report["denoise_applied"] is True
        before = report["noise_floor_before_db"]
        after = report["noise_floor_after_db"]
        assert after < before - 5.0, f"noise floor should drop by >5dB: {before} -> {after}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_job_polishes_segments_before_verification():
    from pipeline.jobs import PipelineService
    from pipeline.models import PipelineJobStatus, SegmentStatus
    from pipeline.quality import QualityVerificationResult

    class PassingVerifier:
        def verify(self, **kwargs):
            return QualityVerificationResult(
                passed=True, overall_score=1.0, failure_reason=None, layer_results={}
            )

    tmp = tempfile.mkdtemp()
    try:
        def fake_synthesize(text, audio_prompt_path=None, **kwargs):
            audio = np.concatenate([_tone(0.5), _silence(1.2), _tone(0.5)])
            return audio.astype(np.float32), SR

        service = PipelineService(
            base_dir=Path(tmp),
            synthesize_fn=fake_synthesize,
            quality_verifier=PassingVerifier(),
        )
        job_id = service.submit_job(
            text="One sentence that is long enough to be a segment.",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"temperature": 0.8, "language": "en", "seed": 888},
        )
        service.run_job_sync(job_id, max_segment_duration=50.0, pause_ms=100)
        job = service.get_job(job_id)
        assert job.status == PipelineJobStatus.DONE
        seg = job.segments[0]
        assert seg.status == SegmentStatus.PASSED
        polish = seg.verification_log[-1].get("polish", {})
        assert polish, "verification log should carry the polish report"
        assert len(polish.get("pauses_compressed", [])) == 1
        # The shipped segment audio has the 1.2s pause compressed to ~0.35s.
        out, _ = sf.read(seg.audio_path, dtype="float32")
        assert abs(len(out) / SR - 1.35) < 0.15
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compose_polishes_legacy_unpolished_segments():
    """_compose_final_audio must polish segment files that predate the polish step."""
    from pipeline.jobs import PipelineService
    from pipeline.models import PipelineJob, PipelineJobStatus

    tmp = tempfile.mkdtemp()
    try:
        service = PipelineService(base_dir=Path(tmp), synthesize_fn=lambda **k: (None, None))
        job_id = "pipe-legacy"
        job = PipelineJob(
            job_id=job_id,
            text="legacy",
            voice_config={"mode": "predefined", "voice_id": "test.wav"},
            gen_params={"language": "en"},
            status=PipelineJobStatus.DONE,
            segments=[],
            created_at=0.0,
            updated_at=0.0,
            pipeline_config={},
        )
        service.store.save(job)

        # A "legacy" segment file with a 1.2s pause and no polish history.
        seg_dir = Path(tmp) / job_id / "segments"
        seg_dir.mkdir(parents=True)
        audio = np.concatenate([_tone(0.5), _silence(1.2), _tone(0.5)])
        seg_path = seg_dir / "0.wav"
        _write(seg_path, audio)
        before_len = len(sf.read(str(seg_path), dtype="float32")[0])

        final_path = service._compose_final_audio(job, [seg_path], pause_ms=100)
        assert final_path is not None and Path(final_path).exists()

        after_len = len(sf.read(str(seg_path), dtype="float32")[0])
        assert after_len < before_len - 0.5 * SR, "legacy segment must be polished at compose"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_long_pause_compressed_to_target()
    test_head_and_tail_silence_trimmed()
    test_natural_pauses_untouched()
    test_denoise_reduces_noise_floor()
    test_job_polishes_segments_before_verification()
    test_compose_polishes_legacy_unpolished_segments()
    print("ALL POLISH TESTS PASSED")
