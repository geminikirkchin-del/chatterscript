# Tests for pipeline.segmenter
# Plain assert style (pytest not installed).

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.segmenter import estimate_segment_duration, split_text_into_segments


# --- estimate_segment_duration ---

def test_estimate_english_words():
    text = "Hello world this is a test"
    dur = estimate_segment_duration(text, "en")
    assert dur > 0, dur


def test_estimate_chinese_chars():
    text = "這是一個中文測試句子"
    dur = estimate_segment_duration(text, "zh")
    assert dur > 0, dur


def test_estimate_empty():
    assert estimate_segment_duration("", "en") == 0.0
    assert estimate_segment_duration("   ", "zh") == 0.0


# --- split_text_into_segments ---

def test_short_text_single_segment():
    text = "This is a short sentence."
    segs = split_text_into_segments(text, max_duration=50.0, language="en")
    assert len(segs) == 1, segs
    assert segs[0].strip() == text.strip(), segs[0]


def test_zh_short_text_single_segment():
    text = "這是一個短句。"
    segs = split_text_into_segments(text, max_duration=50.0, language="zh")
    assert len(segs) == 1, segs
    assert segs[0].strip() == text.strip(), segs[0]


def test_en_groups_short_sentences():
    sentences = ["Hello world.", "How are you?", "I am fine."] * 30
    text = " ".join(sentences)
    segs = split_text_into_segments(text, max_duration=50.0, language="en")
    assert len(segs) > 1, "long text should be split"
    for seg in segs:
        assert estimate_segment_duration(seg, "en") <= 50.0 * 1.2, (
            f"segment over target: {estimate_segment_duration(seg, 'en'):.1f}s -> {seg[:80]}"
        )


def test_zh_groups_short_sentences():
    sentences = ["這是第一句。", "這是第二句。", "這是第三句。"] * 60
    text = "".join(sentences)
    segs = split_text_into_segments(text, max_duration=50.0, language="zh")
    assert len(segs) > 1, "long zh text should be split"
    for seg in segs:
        assert estimate_segment_duration(seg, "zh") <= 50.0 * 1.2, (
            f"segment over target: {estimate_segment_duration(seg, 'zh'):.1f}s -> {seg[:80]}"
        )


def test_long_single_sentence_becomes_own_segment():
    long_sentence = " ".join(["word"] * 500) + "."  # ~167s at 3 words/sec, far over 50s
    text = long_sentence + " Then another sentence."
    segs = split_text_into_segments(text, max_duration=50.0, language="en")
    assert len(segs) >= 2, segs
    # The first segment should contain the long sentence.
    assert len(segs[0].split()) >= 400, segs[0]


def test_mixed_punctuation_zh():
    text = "第一句。第二句！第三句？第四句。" * 20
    segs = split_text_into_segments(text, max_duration=50.0, language="zh")
    assert len(segs) >= 2, segs
    total = "".join(segs)
    # Whitespace may be inserted/adjusted, but content should be preserved.
    assert "第一句" in total and "第二句" in total, total


def test_empty_text_returns_empty():
    assert split_text_into_segments("", max_duration=50.0, language="en") == []
    assert split_text_into_segments("   \n\n  ", max_duration=50.0, language="zh") == []


def test_respects_custom_max_duration():
    text = "One two three. Four five six. Seven eight nine."
    segs = split_text_into_segments(text, max_duration=2.0, language="en")
    assert len(segs) >= 2, segs


if __name__ == "__main__":
    test_estimate_english_words()
    test_estimate_chinese_chars()
    test_estimate_empty()
    test_short_text_single_segment()
    test_zh_short_text_single_segment()
    test_en_groups_short_sentences()
    test_zh_groups_short_sentences()
    test_long_single_sentence_becomes_own_segment()
    test_mixed_punctuation_zh()
    test_empty_text_returns_empty()
    test_respects_custom_max_duration()
    print("ALL SEGMENTER TESTS PASSED")
