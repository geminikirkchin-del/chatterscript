#!/usr/bin/env python3
"""
Subprocess wrapper for Resemblyzer speaker embedding similarity.

Input:  --audio PATH --reference-voice PATH [--output-json PATH]
Output: JSON with cosine similarity and embedding shapes.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict

import numpy as np

logger = logging.getLogger(__name__)


def _load_audio(path: str, sr: int = 16000) -> np.ndarray:
    """Load audio and resample to the target sample rate."""
    import librosa

    audio, loaded_sr = librosa.load(path, sr=sr, mono=True)
    return audio


def compute_similarity(audio_path: str, reference_path: str) -> Dict[str, Any]:
    from resemblyzer import VoiceEncoder, preprocess_wav

    encoder = VoiceEncoder()

    audio_wav = preprocess_wav(_load_audio(audio_path))
    ref_wav = preprocess_wav(_load_audio(reference_path))

    audio_embed = encoder.embed_utterance(audio_wav)
    ref_embed = encoder.embed_utterance(ref_wav)

    # Cosine similarity.
    similarity = float(
        np.dot(audio_embed, ref_embed)
        / (np.linalg.norm(audio_embed) * np.linalg.norm(ref_embed))
    )

    return {
        "cosine_similarity": round(similarity, 4),
        "embedding_shape": list(audio_embed.shape),
        "reference_embedding_shape": list(ref_embed.shape),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resemblyzer speaker similarity wrapper")
    parser.add_argument("--audio", required=True, help="Path to generated audio file")
    parser.add_argument("--reference-voice", required=True, help="Path to reference voice audio")
    parser.add_argument("--output-json", help="Optional path to write JSON output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not Path(args.audio).exists():
        logger.error(f"Audio file not found: {args.audio}")
        return 1
    if not Path(args.reference_voice).exists():
        logger.error(f"Reference voice file not found: {args.reference_voice}")
        return 1

    try:
        result = compute_similarity(args.audio, args.reference_voice)
    except Exception as e:
        logger.error(f"Resemblyzer processing failed: {e}", exc_info=True)
        return 1

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        Path(args.output_json).write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
