# Audio composition: concatenate segment WAVs with inter-segment silence.

import logging
from pathlib import Path
from typing import List

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


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
    if not segment_paths:
        raise ValueError("Cannot compose empty segment list")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pause_samples = int(pause_ms / 1000.0 * sr)
    silence = np.zeros(pause_samples, dtype=np.float32)

    pieces: List[np.ndarray] = []
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
                return False
        pieces.append(data)
        pieces.append(silence)

    # Remove trailing silence.
    if pieces:
        pieces.pop()

    final = np.concatenate(pieces)
    sf.write(str(output_path), final, sr, subtype="pcm_16")
    logger.info(f"Composed final audio: {output_path} ({len(final)} samples @ {sr}Hz)")
    return True
