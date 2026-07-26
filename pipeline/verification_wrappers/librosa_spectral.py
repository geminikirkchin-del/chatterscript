#!/usr/bin/env python3
"""
Subprocess wrapper for Librosa spectral analysis.

Input:  --audio PATH --reference-voice PATH [--output-json PATH]
Output: JSON with MFCC MSE and spectral contrast metrics.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict

import librosa
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_SR = 24000
N_MFCC = 13
N_BANDS = 6


def _load_audio(path: str, sr: int = DEFAULT_SR) -> np.ndarray:
    audio, loaded_sr = librosa.load(path, sr=sr, mono=True)
    return audio


def _compute_mfcc(audio: np.ndarray, sr: int) -> np.ndarray:
    return librosa.feature.mfcc(y=audio, sr=sr, n_mfcc=N_MFCC)


def _compute_spectral_contrast(audio: np.ndarray, sr: int) -> np.ndarray:
    return librosa.feature.spectral_contrast(y=audio, sr=sr, n_bands=N_BANDS)


def _mfcc_mse(mfcc_a: np.ndarray, mfcc_b: np.ndarray) -> float:
    """Mean squared error between two MFCC matrices (trimmed to same length)."""
    min_len = min(mfcc_a.shape[1], mfcc_b.shape[1])
    if min_len == 0:
        return 0.0
    a = mfcc_a[:, :min_len]
    b = mfcc_b[:, :min_len]
    return float(np.mean((a - b) ** 2))


def _spectral_contrast_ratio(sc_a: np.ndarray, sc_b: np.ndarray) -> float:
    """Average per-band cosine similarity between spectral contrast frames."""
    min_len = min(sc_a.shape[1], sc_b.shape[1])
    if min_len == 0:
        return 0.0
    a = sc_a[:, :min_len]
    b = sc_b[:, :min_len]
    similarities = []
    for i in range(a.shape[1]):
        av = a[:, i]
        bv = b[:, i]
        denom = np.linalg.norm(av) * np.linalg.norm(bv)
        if denom == 0:
            continue
        similarities.append(float(np.dot(av, bv) / denom))
    if not similarities:
        return 0.0
    return float(np.mean(similarities))


def compute_spectral_metrics(audio_path: str, reference_path: str) -> Dict[str, Any]:
    audio = _load_audio(audio_path)
    ref = _load_audio(reference_path)

    mfcc_audio = _compute_mfcc(audio, DEFAULT_SR)
    mfcc_ref = _compute_mfcc(ref, DEFAULT_SR)
    sc_audio = _compute_spectral_contrast(audio, DEFAULT_SR)
    sc_ref = _compute_spectral_contrast(ref, DEFAULT_SR)

    mse = _mfcc_mse(mfcc_audio, mfcc_ref)
    sc_ratio = _spectral_contrast_ratio(sc_audio, sc_ref)

    return {
        "mfcc_mse": round(mse, 6),
        "spectral_contrast_ratio": round(sc_ratio, 4),
        "audio_duration_sec": round(len(audio) / DEFAULT_SR, 3),
        "reference_duration_sec": round(len(ref) / DEFAULT_SR, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Librosa spectral analysis wrapper")
    parser.add_argument("--audio", required=True, help="Path to generated audio file")
    parser.add_argument("--reference-voice", required=True, help="Path to reference voice audio")
    parser.add_argument("--output-json", help="Optional path to write JSON output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Force UTF-8 output (see whisperx_align.py for rationale).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if not Path(args.audio).exists():
        logger.error(f"Audio file not found: {args.audio}")
        print(json.dumps({"error": f"audio file not found: {args.audio}"}, ensure_ascii=False))
        return 1
    if not Path(args.reference_voice).exists():
        logger.error(f"Reference voice file not found: {args.reference_voice}")
        print(json.dumps({"error": f"reference voice not found: {args.reference_voice}"}, ensure_ascii=False))
        return 1

    try:
        result = compute_spectral_metrics(args.audio, args.reference_voice)
    except Exception as e:
        logger.error(f"Librosa spectral processing failed: {e}", exc_info=True)
        # Always emit JSON on stdout so the runner can parse the failure reason.
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        Path(args.output_json).write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
