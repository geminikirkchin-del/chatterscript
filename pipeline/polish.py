# Post-generation audio polish: denoise + pause normalization.
#
# Decided in the grilling session (2026-07-26):
# - Runs on every segment AFTER generation, BEFORE verification, so the whole
#   verification chain checks the exact audio that ships.
# - Denoise first (the noise floor would otherwise break silence detection and
#   hurt WhisperX), then normalize pauses.
# - A repair report is returned so the model's raw behaviour stays visible in
#   segment logs and the feedback store.

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

# Denoise: spectral subtraction using the segment's own pre-speech noise bed
# as the profile. Chosen after measuring ffmpeg denoisers on real output:
# anlmdn/afftdn barely touched the vocoder bed (-35.6 -> -36.2 dB), while a
# self-profiled spectral subtract achieved -35.6 -> -49.9 dB with speech
# level essentially unchanged.
SS_ALPHA = 1.5     # over-subtraction factor
SS_BETA = 0.02     # spectral floor as a fraction of local magnitude
SS_ONSET_DB = -30.0  # frames above this count as speech
SS_FALLBACK_QUIET_FRAC = 0.05  # if no pre-speech bed, profile from quietest frames

# Pause normalization parameters (grilling option 2: moderate).
SILENCE_THRESHOLD_DB = -40.0   # what counts as silence
PAUSE_COMPRESS_ABOVE_SEC = 0.6  # only compress pauses longer than this
PAUSE_TARGET_SEC = 0.35         # compress long pauses down to this
PAUSE_MIN_KEEP_SEC = 0.25       # never shorten a pause below this
HEAD_TAIL_MAX_SEC = 0.2         # trim leading/trailing silence to at most this
EDGE_FADE_SEC = 0.02            # micro-fades at joints to avoid clicks
FRAME_SEC = 0.01                # RMS frame size for silence detection
MIN_RUN_SEC = 0.1               # ignore sub-100ms dips (consonant gaps)


def _frame_rms_db(audio: np.ndarray, sr: int) -> np.ndarray:
    """RMS dB per FRAME_SEC frame."""
    frame = max(1, int(sr * FRAME_SEC))
    n_frames = len(audio) // frame
    if n_frames == 0:
        return np.array([])
    frames = audio[: n_frames * frame].astype(np.float64).reshape(n_frames, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-10))


def _silent_runs(db: np.ndarray) -> List[Tuple[float, float]]:
    """Return [(start_sec, end_sec)] of silent runs >= MIN_RUN_SEC."""
    runs: List[Tuple[float, float]] = []
    silent = db < SILENCE_THRESHOLD_DB
    i, n = 0, len(silent)
    while i < n:
        if not silent[i]:
            i += 1
            continue
        j = i
        while j < n and silent[j]:
            j += 1
        start, end = i * FRAME_SEC, j * FRAME_SEC
        if end - start >= MIN_RUN_SEC:
            runs.append((start, end))
        i = j
    return runs


def _apply_fade(piece: np.ndarray, sr: int) -> np.ndarray:
    """Short raised-cosine fade in/out on a piece to avoid joint clicks."""
    n = int(sr * EDGE_FADE_SEC)
    if n <= 0 or len(piece) < 2 * n:
        return piece
    out = piece.copy()
    t = np.linspace(0.0, np.pi / 2.0, n)
    out[:n] *= np.sin(t) ** 2
    out[-n:] *= np.cos(t) ** 2
    return out


def _normalize_pauses(audio: np.ndarray, sr: int) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    Compress over-long pauses and trim head/tail silence.

    Returns (new_audio, report). The report lists every pause that was
    compressed and how much was trimmed from the edges.
    """
    total_sec = len(audio) / sr
    db = _frame_rms_db(audio, sr)
    runs = _silent_runs(db)

    report: Dict[str, Any] = {
        "pauses_compressed": [],
        "head_trimmed_sec": 0.0,
        "tail_trimmed_sec": 0.0,
        "duration_before_sec": round(total_sec, 3),
    }

    if not runs:
        report["duration_after_sec"] = round(total_sec, 3)
        return audio, report

    pieces: List[np.ndarray] = []
    cursor = 0.0
    for start, end in runs:
        # Speech (or non-silent) part before this run.
        if start > cursor:
            pieces.append(audio[int(cursor * sr) : int(start * sr)])

        run_dur = end - start
        is_head = cursor == 0.0 and start <= FRAME_SEC * 1.5
        is_tail = end >= total_sec - FRAME_SEC * 1.5

        if is_head:
            keep = min(run_dur, HEAD_TAIL_MAX_SEC)
            report["head_trimmed_sec"] = round(run_dur - keep, 3)
        elif is_tail:
            keep = min(run_dur, HEAD_TAIL_MAX_SEC)
            report["tail_trimmed_sec"] = round(run_dur - keep, 3)
        elif run_dur > PAUSE_COMPRESS_ABOVE_SEC:
            keep = max(PAUSE_TARGET_SEC, PAUSE_MIN_KEEP_SEC)
            report["pauses_compressed"].append(
                {
                    "start_sec": round(start, 3),
                    "original_sec": round(run_dur, 3),
                    "new_sec": round(keep, 3),
                }
            )
        else:
            keep = run_dur  # natural pause — leave it alone

        silence_piece = audio[int(start * sr) : int(start * sr + keep * sr)]
        pieces.append(_apply_fade(silence_piece, sr))
        cursor = end

    # Trailing speech after the last run.
    if cursor < total_sec:
        pieces.append(audio[int(cursor * sr) :])

    if not pieces:
        report["duration_after_sec"] = 0.0
        return np.array([], dtype=audio.dtype), report

    new_audio = np.concatenate(pieces)
    report["duration_after_sec"] = round(len(new_audio) / sr, 3)
    return new_audio, report


def _noise_floor_db(audio: np.ndarray, sr: int, probe_sec: float = 0.3) -> Optional[float]:
    """RMS dB of the first probe_sec seconds (pre-speech noise floor estimate)."""
    n = min(len(audio), int(sr * probe_sec))
    if n == 0:
        return None
    rms = float(np.sqrt(np.mean(audio[:n].astype(np.float64) ** 2)))
    return round(20 * np.log10(max(rms, 1e-10)), 1)


def _stft(x: np.ndarray, nper: int = 2048, hop: int = 512) -> np.ndarray:
    w = np.hanning(nper)
    return np.array([
        np.fft.rfft(x[i : i + nper] * w) for i in range(0, len(x) - nper + 1, hop)
    ])


def _istft(specs: np.ndarray, length: int, nper: int = 2048, hop: int = 512) -> np.ndarray:
    w = np.hanning(nper)
    out = np.zeros(length)
    wsum = np.zeros(length)
    for k, s in enumerate(specs):
        i = k * hop
        frame = np.fft.irfft(s, nper)
        out[i : i + nper] += frame * w
        wsum[i : i + nper] += w ** 2
    nz = wsum > 1e-8
    out[nz] /= wsum[nz]
    return out


def _speech_onset_sample(audio: np.ndarray, sr: int) -> int:
    """First sample where a 20ms frame exceeds SS_ONSET_DB."""
    frame = max(1, int(sr * 0.02))
    n = len(audio) // frame
    if n == 0:
        return 0
    frames = audio[: n * frame].astype(np.float64).reshape(n, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-10))
    above = np.nonzero(db > SS_ONSET_DB)[0]
    return int(above[0]) * frame if len(above) else len(audio)


def _spectral_subtract(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Remove the segment's constant noise bed via spectral subtraction.

    The noise profile comes from the segment's own pre-speech bed (the TTS
    vocoder emits a constant electronic bed that generic denoisers barely
    touch). If there is no usable pre-speech region, the quietest frames of
    the whole segment are used as the profile.
    """
    if len(audio) < int(sr * 0.5):
        return audio  # too short to profile anything safely

    onset = _speech_onset_sample(audio, sr)
    bed_end = max(0, onset - int(sr * 0.05))
    if bed_end >= int(sr * 0.1):
        bed = audio[:bed_end]
    else:
        # Fallback: quietest frames of the segment as the noise profile.
        frame = max(1, int(sr * 0.02))
        n = len(audio) // frame
        frames = audio[: n * frame].reshape(n, frame)
        rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
        k = max(1, int(n * SS_FALLBACK_QUIET_FRAC))
        quiet_idx = np.argsort(rms)[:k]
        bed = frames[quiet_idx].reshape(-1)
        if len(bed) < int(sr * 0.05):
            return audio

    if len(bed) < 2048:
        return audio

    # Reflect-pad so the first/last STFT windows reconstruct cleanly;
    # without padding the ISTFT edges blow up (window normalization).
    pad = 1024
    padded = np.pad(audio, pad, mode="reflect")
    specs = _stft(padded)
    if specs.size == 0:
        return audio
    mag, phase = np.abs(specs), np.angle(specs)
    profile = np.mean(np.abs(_stft(bed)), axis=0)
    mag_out = np.maximum(mag - SS_ALPHA * profile, SS_BETA * mag)
    out = _istft(mag_out * np.exp(1j * phase), len(padded))
    return out[pad : pad + len(audio)].astype(np.float32)


def polish_segment_audio(
    input_path: str,
    output_path: Optional[str] = None,
    *,
    denoise: bool = True,
) -> Dict[str, Any]:
    """
    Polish one generated segment: denoise, then normalize pauses.

    Writes the polished audio to output_path (defaults to in-place replace of
    input_path) and returns a repair report describing everything that was
    changed, so the model's raw behaviour stays visible downstream.
    """
    src = Path(input_path)
    dst = Path(output_path) if output_path else src
    report: Dict[str, Any] = {"denoise_applied": False}

    audio, sr = sf.read(str(src), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    if denoise:
        report["noise_floor_before_db"] = _noise_floor_db(audio, sr)
        audio = _spectral_subtract(audio, sr)
        report["noise_floor_after_db"] = _noise_floor_db(audio, sr)
        report["denoise_applied"] = True

    new_audio, pause_report = _normalize_pauses(audio, sr)
    report.update(pause_report)

    if len(new_audio) == 0:
        logger.warning(f"Polish produced empty audio for {src}; keeping original")
        report["error"] = "empty_after_polish"
        return report

    sf.write(str(dst), new_audio, sr, subtype="pcm_16")

    logger.info(
        f"Polished {src.name}: denoise={report['denoise_applied']}, "
        f"pauses_compressed={len(report['pauses_compressed'])}, "
        f"head_trim={report['head_trimmed_sec']}s, tail_trim={report['tail_trimmed_sec']}s, "
        f"duration {report['duration_before_sec']}s -> {report['duration_after_sec']}s"
    )
    return report
