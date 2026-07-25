# Tests for pipeline.composer
# Plain assert style.

import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import soundfile as sf

import subprocess

from pipeline.composer import compose_segments, loudnorm_final_audio


def _write_wav(path: Path, samples: np.ndarray, sr: int):
    sf.write(str(path), samples, sr, subtype="pcm_16")


def test_compose_two_segments_with_silence():
    tmp = tempfile.mkdtemp()
    try:
        sr = 24000
        seg1 = np.full(sr, 0.5, dtype=np.float32)  # 1s
        seg2 = np.full(sr, 0.3, dtype=np.float32)  # 1s
        p1 = Path(tmp) / "seg0.wav"
        p2 = Path(tmp) / "seg1.wav"
        _write_wav(p1, seg1, sr)
        _write_wav(p2, seg2, sr)

        out = Path(tmp) / "out.wav"
        compose_segments([p1, p2], out, sr=sr, pause_ms=150)

        data, read_sr = sf.read(str(out), dtype="float32")
        assert read_sr == sr
        expected_len = len(seg1) + len(seg2) + int(0.150 * sr)
        assert abs(len(data) - expected_len) <= 2, f"expected ~{expected_len}, got {len(data)}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compose_single_segment_passthrough():
    tmp = tempfile.mkdtemp()
    try:
        sr = 24000
        seg = np.full(int(0.5 * sr), 0.5, dtype=np.float32)
        p = Path(tmp) / "seg0.wav"
        _write_wav(p, seg, sr)

        out = Path(tmp) / "out.wav"
        compose_segments([p], out, sr=sr, pause_ms=150)

        data, read_sr = sf.read(str(out), dtype="float32")
        assert read_sr == sr
        assert len(data) == len(seg), f"expected {len(seg)}, got {len(data)}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compose_empty_raises():
    tmp = tempfile.mkdtemp()
    try:
        out = Path(tmp) / "out.wav"
        try:
            compose_segments([], out, sr=24000, pause_ms=150)
            raise AssertionError("expected ValueError for empty segment list")
        except ValueError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compose_uses_target_sample_rate():
    tmp = tempfile.mkdtemp()
    try:
        sr = 24000
        seg = np.full(sr, 0.1, dtype=np.float32)
        p = Path(tmp) / "seg0.wav"
        _write_wav(p, seg, sr)

        out = Path(tmp) / "out.wav"
        compose_segments([p], out, sr=sr, pause_ms=100)

        data, read_sr = sf.read(str(out), dtype="float32")
        assert read_sr == sr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ffmpeg_available() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def test_loudnorm_final_audio():
    if not _ffmpeg_available():
        return
    tmp = tempfile.mkdtemp()
    try:
        sr = 24000
        # Create a loud sine-ish tone that will be pulled down by loudnorm.
        t = np.arange(sr, dtype=np.float32) / sr
        seg = 0.9 * np.sin(2 * np.pi * 440 * t)
        input_path = Path(tmp) / "input.wav"
        output_path = Path(tmp) / "output.wav"
        _write_wav(input_path, seg, sr)

        ok = loudnorm_final_audio(
            input_path, output_path, target_lufs=-20.0, true_peak=-2.0, sample_rate=sr
        )
        assert ok
        assert output_path.exists()

        data, read_sr = sf.read(str(output_path), dtype="float32")
        assert read_sr == sr
        assert len(data) == len(seg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_compose_two_segments_with_silence()
    test_compose_single_segment_passthrough()
    test_compose_empty_raises()
    test_compose_uses_target_sample_rate()
    test_loudnorm_final_audio()
    print("ALL COMPOSER TESTS PASSED")
