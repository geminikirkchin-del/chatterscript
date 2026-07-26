# Text segmentation for the long-form TTS pipeline.

import logging
import re
from typing import List

import utils

logger = logging.getLogger(__name__)

# Estimated speech rates derived from Edu-content-prep length targets.
# These are approximations; actual durations depend on the speaker and language.
SPEECH_RATES = {
    "zh": 5.5,  # ~5.5 CJK characters per second
    "zh-cn": 5.5,
    "zh-hk": 5.5,
    "zh-tw": 5.5,
    "en": 3.0,  # ~3 words per second
    "en-us": 3.0,
    "en-gb": 3.0,
}


def estimate_segment_duration(text: str, language: str) -> float:
    """Estimate audio duration in seconds for a segment."""
    if not text or not text.strip():
        return 0.0

    lang = (language or "en").lower()
    rate = SPEECH_RATES.get(lang, SPEECH_RATES.get(lang.split("-")[0], 3.0))

    if lang.startswith("zh") or lang in ("zh-cn", "zh-hk", "zh-tw"):
        # Count CJK/full-width characters as the primary speech units.
        cjk_count = sum(1 for ch in text if utils._is_cjk_or_fullwidth(ch))
        return cjk_count / rate

    # For other languages, count words.
    words = len(text.split())
    return words / rate


def split_text_into_segments(text: str, max_duration: float, language: str = "en") -> List[str]:
    """
    Split long text into segments of exactly one sentence each.

    Long-form TTS quality degrades sharply when a segment packs multiple
    sentences, so the pipeline generates one sentence per segment and lets the
    composer add natural pauses between them. `max_duration` is the target
    ceiling for a single sentence: a sentence whose estimated duration exceeds
    it still forms its own segment (we never split mid-sentence) but is logged
    for visibility.
    """
    if not text or not text.strip():
        return []

    sentences = utils.split_into_sentences(text)
    if not sentences:
        stripped = text.strip()
        return [stripped] if stripped else []

    segments: List[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence_duration = estimate_segment_duration(sentence, language)
        if sentence_duration > max_duration:
            logger.warning(
                f"Single sentence exceeds target duration "
                f"({sentence_duration:.1f}s > {max_duration:.1f}s): {sentence[:60]}..."
            )
        segments.append(sentence)

    return segments
