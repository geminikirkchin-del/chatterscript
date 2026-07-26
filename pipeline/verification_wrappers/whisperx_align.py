#!/usr/bin/env python3
"""
Subprocess wrapper for WhisperX transcription + alignment.

This script is intentionally self-contained so it can run inside the isolated
`.verification_venv` environment while the main TTS server uses a different
Python interpreter / dependency set.

Input:  --audio PATH --reference-text TEXT --language LANG [--model MODEL] [--device DEVICE]
Output: JSON written to stdout or --output-json PATH
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Compatibility: older pyannote.audio uses np.NaN, removed in NumPy 2.0.
try:
    import numpy as np

    if not hasattr(np, "NaN"):
        np.NaN = np.nan  # type: ignore[misc]
except Exception:
    pass

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "small"
DEFAULT_DEVICE = "auto"


def _normalize_zh_script(text: str) -> str:
    """
    Normalize Chinese text for fair WER/CER comparison.

    WhisperX transcribes zh audio in Simplified Chinese with Arabic digits,
    while scripts are often Traditional with Chinese numerals. Convert both
    sides to the same form — Simplified script (OpenCC t2s) and Chinese
    numerals (cn2an an2cn) — so script-variant differences don't count as
    content errors. Both reference and transcription pass through the same
    normalization, so either convention converges.
    """
    try:
        from opencc import OpenCC

        text = OpenCC("t2s").convert(text)
    except Exception as e:
        logger.warning(f"OpenCC unavailable, skipping t2s conversion: {e}")
    try:
        import cn2an

        text = cn2an.transform(text, "an2cn")
    except Exception as e:
        logger.warning(f"cn2an unavailable, skipping digit normalization: {e}")
    return text


def _normalize_text(text: str, language: str = "en") -> str:
    """Strip punctuation and collapse whitespace for coverage comparison."""
    text = text.lower().strip()
    punctuation_pattern = r'[\u3001-\u303f\uff00-\uffef"\'“”‘’.,!?;:@#$%^&*()\[\]{}|\\<>]'
    text = re.sub(punctuation_pattern, "", text)
    if language.lower().startswith("zh"):
        cjk_punctuation_pattern = r'[。！？，、；：“”‘’（）【】《》\u3000]'
        text = re.sub(cjk_punctuation_pattern, "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if language.lower().startswith("zh"):
        text = _normalize_zh_script(text)
    return text


def _detect_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _count_units(text: str, language: str) -> int:
    """Count words for non-CJK, characters for CJK."""
    if language.lower().startswith("zh"):
        return len(text.replace(" ", ""))
    return len(text.split())


def _compute_coverage(reference: str, transcription: str, language: str) -> float:
    """Coverage ratio capped at 1.0; additions do not cause failure."""
    ref_units = _count_units(reference, language)
    trans_units = _count_units(transcription, language)
    if ref_units == 0:
        return 1.0 if trans_units == 0 else 0.0
    return min(1.0, trans_units / ref_units)


def _mean_word_confidence(aligned_segments: List[Dict[str, Any]]) -> float:
    """Average word confidence from alignment output."""
    scores: List[float] = []
    for seg in aligned_segments:
        for word in seg.get("words", []):
            score = word.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def _whisperx_language(language: str) -> str:
    """Map our language codes to WhisperX-compatible ISO codes."""
    lang = language.lower()
    if lang.startswith("zh"):
        return "zh"
    if lang.startswith("en"):
        return "en"
    return lang.split("-")[0]


def run_whisperx(
    audio_path: str,
    reference_text: str,
    language: str,
    model_name: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
) -> Dict[str, Any]:
    import torch
    import whisperx

    # WhisperX / pyannote checkpoints were created before torch 2.6's
    # weights_only default. Patch torch.load locally so these trusted
    # checkpoints can load.
    _orig_torch_load = torch.load

    def _load_weights_only_false(*args, **kwargs):
        # Override any caller that explicitly passes weights_only=True.
        kwargs["weights_only"] = False
        return _orig_torch_load(*args, **kwargs)

    torch.load = _load_weights_only_false

    resolved_device = _detect_device(device)
    logger.info(f"Loading WhisperX model '{model_name}' on {resolved_device}...")
    model = whisperx.load_model(
        model_name,
        resolved_device,
        compute_type="float16" if resolved_device == "cuda" else "int8",
        language=_whisperx_language(language),
    )

    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio, language=_whisperx_language(language))
    segments = result.get("segments", [])
    transcription = " ".join(seg.get("text", "").strip() for seg in segments).strip()

    # Alignment: word-level timestamps + confidence scores.
    align_model, align_metadata = whisperx.load_align_model(
        language_code=_whisperx_language(language),
        device=resolved_device,
    )
    aligned = whisperx.align(
        segments,
        align_model,
        align_metadata,
        audio,
        resolved_device,
        return_char_alignments=False,
    )
    aligned_segments = aligned.get("segments", [])

    norm_reference = _normalize_text(reference_text, language)
    norm_transcription = _normalize_text(transcription, language)
    mean_confidence = _mean_word_confidence(aligned_segments)
    coverage_ratio = _compute_coverage(norm_reference, norm_transcription, language)

    return {
        "transcription": transcription,
        "normalized_transcription": norm_transcription,
        "normalized_reference": norm_reference,
        "mean_word_confidence": round(mean_confidence, 4),
        "text_coverage_ratio": round(coverage_ratio, 4),
        "word_count": _count_units(norm_transcription, language),
        "reference_unit_count": _count_units(norm_reference, language),
        "language": language,
        "whisperx_language": _whisperx_language(language),
        "aligned_segments": aligned_segments,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="WhisperX transcription + alignment wrapper")
    parser.add_argument("--audio", required=True, help="Path to input audio file")
    parser.add_argument("--reference-text", required=True, help="Original text the audio should speak")
    parser.add_argument("--language", default="en", help="Language code (e.g. en, zh)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="WhisperX model name")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="torch device (cuda/cpu/auto)")
    parser.add_argument("--output-json", help="Optional path to write JSON output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Force UTF-8 output so Chinese transcription text prints correctly even
    # when the parent console codepage is cp1252 (Windows default).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if not Path(args.audio).exists():
        logger.error(f"Audio file not found: {args.audio}")
        print(json.dumps({"error": f"audio file not found: {args.audio}"}, ensure_ascii=False))
        return 1

    try:
        result = run_whisperx(
            audio_path=args.audio,
            reference_text=args.reference_text,
            language=args.language,
            model_name=args.model,
            device=args.device,
        )
    except Exception as e:
        logger.error(f"WhisperX processing failed: {e}", exc_info=True)
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
