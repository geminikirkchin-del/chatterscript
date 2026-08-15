# Audio composition: concatenate segment WAVs with inter-segment silence.

import logging
import subprocess
from pathlib import Path
from typing import List, Tuple

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


def _seconds_to_srt_ts(seconds: float) -> str:
    """Convert seconds to SRT timestamp format HH:MM:SS,mmm."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        millis -= 1000
        secs += 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(
    subtitle_path: Path,
    entries: List[Tuple[float, float, str]],
) -> bool:
    """
    Write subtitle entries to an SRT file.

    Args:
        subtitle_path: destination .srt path.
        entries: list of (start_sec, end_sec, text).

    Returns:
        True on success.
    """
    try:
        subtitle_path.parent.mkdir(parents=True, exist_ok=True)
        with open(subtitle_path, "w", encoding="utf-8") as f:
            for idx, (start, end, text) in enumerate(entries, start=1):
                f.write(f"{idx}\n")
                f.write(f"{_seconds_to_srt_ts(start)} --> {_seconds_to_srt_ts(end)}\n")
                f.write(f"{text.strip()}\n\n")
        logger.info(f"Wrote SRT subtitles: {subtitle_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to write SRT {subtitle_path}: {e}", exc_info=True)
        return False


def compose_segments_with_timing(
    segment_paths: List[Path],
    output_path: Path,
    sr: int,
    pause_ms: float = 150.0,
) -> Tuple[bool, List[Tuple[float, float]]]:
    """
    Concatenate WAV files with silence between them and return segment timings.

    Returns:
        (success, timings) where timings is a list of (start_sec, end_sec)
        for each input segment in the composed timeline.
    """
    if not segment_paths:
        raise ValueError("Cannot compose empty segment list")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pause_samples = int(pause_ms / 1000.0 * sr)
    pause_duration = pause_samples / sr
    silence = np.zeros(pause_samples, dtype=np.float32)

    pieces: List[np.ndarray] = []
    timings: List[Tuple[float, float]] = []
    current_time = 0.0

    for seg_path in segment_paths:
        data, file_sr = sf.read(str(seg_path), dtype="float32")
        if data.ndim > 1:
            data = data[:, 0]
        if file_sr != sr:
            try:
                import librosa

                data = librosa.resample(y=data, orig_sr=file_sr, target_sr=sr)
            except Exception as e:
                logger.error(f"Failed to resample {seg_path} from {file_sr} to {sr}: {e}")
                return False, []
        duration = len(data) / sr
        timings.append((current_time, current_time + duration))
        current_time += duration + pause_duration
        pieces.append(data)
        pieces.append(silence)

    # Remove trailing silence.
    if pieces:
        pieces.pop()

    final = np.concatenate(pieces)
    sf.write(str(output_path), final, sr, subtype="pcm_16")
    logger.info(f"Composed final audio: {output_path} ({len(final)} samples @ {sr}Hz)")
    return True, timings


def compose_segments(
    segment_paths: List[Path],
    output_path: Path,
    sr: int,
    pause_ms: float = 150.0,
) -> bool:
    """
    Concatenate WAV files with silence between them.

    Args:
        segment_paths: ordered list of segment WAV file paths.
        output_path: destination path for the composed audio.
        sr: target sample rate (segments are resampled if their rate differs).
        pause_ms: silence duration between segments in milliseconds.

    Returns:
        True on success, False otherwise.
    """
    success, _ = compose_segments_with_timing(
        segment_paths, output_path, sr, pause_ms
    )
    return success


def loudnorm_final_audio(
    input_path: Path,
    output_path: Path,
    target_lufs: float = -16.0,
    true_peak: float = -1.5,
    lra: float = 11.0,
    sample_rate: int = 24000,
) -> bool:
    """
    Apply FFmpeg loudnorm to the composed final audio.

    This is a single-pass normalization with the measured input parameters left
    to FFmpeg's internal lookahead. It enforces a broadcast-friendly integrated
    loudness and true-peak ceiling on the whole long-form output.

    Args:
        input_path: composed WAV file to normalize.
        output_path: destination for the normalized WAV file.
        target_lufs: target integrated loudness in LUFS.
        true_peak: maximum true peak in dBTP.
        lra: loudness range target in LU.
        sample_rate: output sample rate.

    Returns:
        True on success, False otherwise.
    """
    if not input_path.exists():
        logger.error(f"loudnorm input not found: {input_path}")
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # loudnorm's TP parameter must be <= 0 dBTP.
    tp = min(0.0, float(true_peak))
    filter_str = (
        f"loudnorm=print_format=json:linear=true:"
        f"I={target_lufs}:TP={tp}:LRA={lra}"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i", str(input_path),
        "-af", filter_str,
        "-ar", str(sample_rate),
        "-ac", "1",
        str(output_path),
    ]
    logger.info(f"Applying final loudnorm to {input_path}: target {target_lufs} LUFS, {tp} dBTP")
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except Exception as e:
        logger.error(f"Failed to run FFmpeg loudnorm: {e}")
        return False

    if result.returncode != 0:
        logger.error(f"FFmpeg loudnorm failed (exit {result.returncode}):\n{result.stdout[-2000:]}")
        return False

    if not output_path.exists():
        logger.error("FFmpeg loudnorm produced no output file")
        return False

    logger.info(f"Final loudnorm complete: {output_path}")
    return True
