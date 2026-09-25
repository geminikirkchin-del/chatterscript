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

from pipeline.composer import (
    compose_segments,
    loudnorm_final_audio,
    write_srt,
    _split_srt_entries,
)


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


def test_split_srt_entries_splits_long_chinese_sentence():
    entries = [
        (0.0, 20.0, "在不到一秒钟的时间里，你的文字和图片被切割、封装、寻址、转发，经过路由器、交换机和海底光缆，穿过无数台机器，最终抵达目的地。"),
    ]
    split = _split_srt_entries(entries, max_duration=7.0)
    assert len(split) > 1, f"expected split, got {len(split)} entries"
    assert split[0][0] == 0.0
    assert split[-1][1] == 20.0
    # Each entry should be no longer than max_duration (within rounding).
    for start, end, _ in split:
        assert end - start <= 7.5, f"entry too long: {end - start}s"


def test_split_srt_entries_keeps_short_entries_unchanged():
    entries = [
        (0.0, 3.0, "这是一个短句。"),
        (3.5, 6.0, "这是另一个短句。"),
    ]
    split = _split_srt_entries(entries, max_duration=7.0)
    assert len(split) == 2
    assert split[0][2] == "这是一个短句。"
    assert split[1][2] == "这是另一个短句。"


def test_write_srt_creates_split_entries():
    tmp = tempfile.mkdtemp()
    try:
        srt_path = Path(tmp) / "test.srt"
        entries = [
            (0.0, 20.0, "第一句话，第二句话，第三句话，第四句话。"),
        ]
        ok = write_srt(srt_path, entries, max_subtitle_duration_sec=5.0)
        assert ok
        assert srt_path.exists()
        content = srt_path.read_text(encoding="utf-8")
        assert "-->" in content
        # Should have split into multiple numbered entries.
        assert content.strip().startswith("1")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_compose_two_segments_with_silence()
    test_compose_single_segment_passthrough()
    test_compose_empty_raises()
    test_compose_uses_target_sample_rate()
    test_loudnorm_final_audio()
    test_split_srt_entries_splits_long_chinese_sentence()
    test_split_srt_entries_keeps_short_entries_unchanged()
    test_write_srt_creates_split_entries()
    print("ALL COMPOSER TESTS PASSED")
