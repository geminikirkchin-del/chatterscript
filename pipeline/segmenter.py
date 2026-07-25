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
    Split long text into segments targeting a maximum audio duration.

    Sentences are produced with the existing sentence splitter, then greedily
    grouped until adding the next sentence would exceed the target duration.
    A single sentence longer than the target forms its own segment.
    """
    if not text or not text.strip():
        return []

    sentences = utils.split_into_sentences(text)
    if not sentences:
        stripped = text.strip()
        return [stripped] if stripped else []

    segments: List[List[str]] = []
    current_group: List[str] = []
    current_duration = 0.0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence_duration = estimate_segment_duration(sentence, language)

        if not current_group:
            current_group.append(sentence)
            current_duration = sentence_duration
            continue

        if current_duration + sentence_duration <= max_duration:
            current_group.append(sentence)
            current_duration += sentence_duration
        else:
            segments.append(current_group)
            current_group = [sentence]
            current_duration = sentence_duration

    if current_group:
        segments.append(current_group)

    return [_join_group(group, language) for group in segments if group]


def _join_delimiter(prev: str, next_: str, language: str) -> str:
    """Return the delimiter to use between two sentence strings."""
    if not prev:
        return ""
    # CJK text does not use spaces between sentences.
    if language.lower().startswith("zh") or utils._is_cjk_or_fullwidth(prev[-1]):
        return ""
    return " "


def _join_group(group: List[str], language: str) -> str:
    """Join a group of sentences preserving natural spacing per language."""
    if not group:
        return ""
    result = group[0]
    for sentence in group[1:]:
        delimiter = _join_delimiter(result, sentence, language)
        result = result + delimiter + sentence
    return result
