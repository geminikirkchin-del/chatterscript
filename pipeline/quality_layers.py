# Concrete quality verifier layers.

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import soundfile as sf
import numpy as np

from config import config_manager
from pipeline.quality import LayerResult, QualityVerifier, VerificationContext

logger = logging.getLogger(__name__)

# Optional jiwer import. If unavailable, JiwerContentVerifier reports
# jiwer_unavailable instead of crashing.
try:
    import jiwer
except Exception:
    jiwer = None  # type: ignore[assignment]


def _layer_thresholds(layer_name: str) -> Dict[str, Any]:
    """
    Load a layer's thresholds from pipeline.verification config.

    Defaults live only in config.py's DEFAULT_CONFIG (always merged into the
    loaded config), so no inline fallback tables are duplicated here.
    """
    verification_cfg = config_manager.get("pipeline.verification", {})
    layer_cfg = verification_cfg.get("layers", {}).get(layer_name, {})
    return dict(layer_cfg.get("thresholds", {}))


_missing_threshold_warned: set = set()


def _get_threshold(
    thresholds: Dict[str, Any], key: str, layer_name: str
) -> Optional[float]:
    """
    Read a single threshold as float. If the key is absent (config defaults
    normally guarantee its presence), log once and return None so the caller
    can skip that particular check instead of duplicating a default number.
    """
    value = thresholds.get(key)
    if value is None:
        warn_key = (layer_name, key)
        if warn_key not in _missing_threshold_warned:
            _missing_threshold_warned.add(warn_key)
            logger.warning(
                f"Threshold '{key}' for layer '{layer_name}' is not configured; "
                "the related check will be skipped."
            )
        return None
    return float(value)


def _load_runner(func_name: str, label: str):
    """
    Import a runner function from verification_wrappers.runner.

    Returns (callable, None) on success or (None, exception) on import
    failure, so each layer can shape its own unavailable-path metrics.
    Imported lazily at call time so tests can monkeypatch runner functions.
    """
    try:
        from pipeline.verification_wrappers import runner as _runner

        return getattr(_runner, func_name), None
    except Exception as e:
        logger.error(f"Failed to import {label} runner: {e}")
        return None, e


def _run_ffmpeg_loudnorm(audio_path: str, target_lufs: float, true_peak: float) -> Dict[str, Any]:
    """
    Run FFmpeg loudnorm filter in print-format mode to measure input loudness.

    Returns a dict with input_i (LUFS), input_tp (dBTP), input_lra (LRA),
    input_thresh, target_offset, etc.
    """
    # loudnorm's TP parameter must be in [-9, 0]; clamp for measurement.
    loudnorm_tp = min(0.0, float(true_peak))
    filter_str = (
        f"loudnorm=print_format=json:linear=true:"
        f"I={target_lufs}:TP={loudnorm_tp}:LRA=11"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i", audio_path,
        "-af", filter_str,
        "-f", "null",
        "-",
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = result.stdout
    # loudnorm prints a JSON block starting with "[Parsed_loudnorm" and ending with "}".
    try:
        start = output.index("{")
        end = output.rindex("}") + 1
        data = json.loads(output[start:end])
        return data
    except (ValueError, json.JSONDecodeError) as e:
        logger.error(f"Failed to parse FFmpeg loudnorm output: {e}\n{output}")
        return {}


def _compute_rms_and_peak(audio_path: str) -> Dict[str, float]:
    """Compute RMS and peak using soundfile + numpy."""
    import numpy as np

    audio, sr = sf.read(audio_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return {
        "rms": float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))),
        "peak": float(np.max(np.abs(audio))),
        "duration_sec": float(len(audio) / sr),
    }


def _compute_dynamic_range_db(audio_path: str) -> float:
    """
    Estimate dynamic range as the difference between the 95th and 10th percentile
    of short-term RMS (100ms windows). This avoids outliers while capturing real
    variation.
    """
    import numpy as np

    audio, sr = sf.read(audio_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    window_size = int(sr * 0.1)  # 100ms
    if window_size == 0:
        return 0.0
    rms_windows = []
    for i in range(0, len(audio) - window_size + 1, window_size):
        window = audio[i : i + window_size]
        rms = np.sqrt(np.mean(window.astype(np.float64) ** 2))
        rms_windows.append(rms)
    if not rms_windows:
        return 0.0
    rms_windows = np.array(rms_windows)
    # Convert to dB, with a floor to avoid log(0).
    db = 20 * np.log10(np.maximum(rms_windows, 1e-10))
    p95 = np.percentile(db, 95)
    p10 = np.percentile(db, 10)
    return float(p95 - p10)


class FFmpegAudioMetricsVerifier(QualityVerifier):
    """
    Audio metrics verifier using FFmpeg loudnorm and local numpy calculations.

    Checks:
    - Integrated LUFS within tolerance of target
    - True Peak below maximum dBTP
    - RMS above minimum
    - Dynamic Range above minimum
    """

    @property
    def name(self) -> str:
        return "audio_metrics"

    def _config(self) -> Dict[str, Any]:
        return _layer_thresholds(self.name)

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[VerificationContext] = None,
    ) -> LayerResult:
        cfg = self._config()
        target_lufs = _get_threshold(cfg, "target_lufs", self.name)
        lufs_tolerance = _get_threshold(cfg, "lufs_tolerance", self.name)
        true_peak_max = _get_threshold(cfg, "true_peak_max_dbtp", self.name)
        min_rms = _get_threshold(cfg, "min_rms", self.name)
        min_dynamic_range = _get_threshold(cfg, "min_dynamic_range_db", self.name)

        # The loudnorm measurement needs both the loudness target and the peak
        # ceiling; without them no loudness metrics are recorded and the LUFS
        # check is skipped.
        if target_lufs is not None and true_peak_max is not None:
            loudnorm = _run_ffmpeg_loudnorm(audio_path, target_lufs, true_peak_max)
        else:
            loudnorm = {}
        basic = _compute_rms_and_peak(audio_path)
        dynamic_range_db = _compute_dynamic_range_db(audio_path)

        input_lufs = float(loudnorm.get("input_i", -70.0))
        input_tp = float(loudnorm.get("input_tp", 0.0))

        metrics = {
            "lufs": input_lufs,
            "true_peak_dbtp": input_tp,
            "rms": basic["rms"],
            "peak": basic["peak"],
            "dynamic_range_db": dynamic_range_db,
            "duration_sec": basic["duration_sec"],
            "target_lufs": target_lufs,
            "lufs_tolerance": lufs_tolerance,
            "true_peak_max_dbtp": true_peak_max,
        }

        failure_reason: Optional[str] = None
        # Check RMS first so truly silent files are flagged as low_rms (matching the
        # legacy basic verifier behaviour).
        # A threshold of None means the key is not configured; that check is skipped.
        if min_rms is not None and basic["rms"] < min_rms:
            failure_reason = "low_rms"
        elif min_dynamic_range is not None and dynamic_range_db < min_dynamic_range:
            failure_reason = "dynamic_range_low"
        # Note: LUFS and true_peak are recorded in metrics but are NOT segment
        # hard-fails. Raw single-sentence TTS output is naturally quieter than the
        # broadcast target (-16 LUFS) because integrated loudness includes the
        # sentence's leading/trailing pauses; the final compose applies loudnorm to
        # enforce the broadcast loudness and true-peak targets on the whole output
        # (Ticket 04). Hard-failing segments on LUFS rejected healthy audio.

        # Simple score: 1.0 if all good, otherwise proportional to how far off.
        if failure_reason is None:
            score = 1.0
        else:
            score = 0.5  # Hard-fail layers get a baseline low score on failure.

        return LayerResult(
            passed=failure_reason is None,
            score=score,
            failure_reason=failure_reason,
            metrics=metrics,
        )


# ---------------------------------------------------------------------------
# WhisperX alignment + jiwer content layers (Ticket 02)
# ---------------------------------------------------------------------------


def _get_whisperx_config() -> Dict[str, Any]:
    """Read pipeline.asr config used by both WhisperX layers."""
    return config_manager.get("pipeline.asr", {})


def run_whisperx_alignment(
    audio_path: str,
    original_text: str,
    language: str,
) -> Optional[Dict[str, Any]]:
    """
    Run WhisperX alignment once per segment.

    Called by PipelineQualityVerifier when any enabled layer declares
    requires_whisperx; the result is shared with all layers via
    VerificationContext.whisperx_result. Returns None if the runner or the
    isolated venv is unavailable.
    """
    asr_config = _get_whisperx_config()
    model_name = asr_config.get("model", "small")
    # Use the same device the TTS engine resolved to (whisperx will re-resolve
    # 'auto' internally). 'auto' keeps the wrapper portable.
    device = asr_config.get("device", "auto")

    run_fn, import_error = _load_runner("run_whisperx_align", "WhisperX")
    if run_fn is None:
        return None

    return run_fn(
        audio_path=audio_path,
        reference_text=original_text,
        language=language,
        model_name=model_name,
        device=device,
    )


class WhisperXAlignmentVerifier(QualityVerifier):
    """
    Hard-fail verifier using WhisperX word-level alignment.

    Checks:
    - Mean word confidence >= threshold
    - Text coverage ratio (transcribed vs original units) >= threshold
    """

    requires_whisperx = True

    @property
    def name(self) -> str:
        return "whisperx_alignment"

    def _config(self) -> Dict[str, Any]:
        return _layer_thresholds(self.name)

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[VerificationContext] = None,
    ) -> LayerResult:
        cfg = self._config()
        min_confidence = _get_threshold(cfg, "min_mean_word_confidence", self.name)
        min_coverage = _get_threshold(cfg, "min_text_coverage_ratio", self.name)

        result = context.whisperx_result if context is not None else None

        if result is None:
            return LayerResult(
                passed=False,
                score=0.0,
                failure_reason="whisperx_unavailable",
                metrics={
                    "min_mean_word_confidence": min_confidence,
                    "min_text_coverage_ratio": min_coverage,
                },
            )

        mean_confidence = float(result.get("mean_word_confidence", 0.0))
        coverage_ratio = float(result.get("text_coverage_ratio", 0.0))

        metrics = {
            "mean_word_confidence": mean_confidence,
            "text_coverage_ratio": coverage_ratio,
            "transcription": result.get("transcription", ""),
            "normalized_transcription": result.get("normalized_transcription", ""),
            "normalized_reference": result.get("normalized_reference", ""),
            "word_count": result.get("word_count", 0),
            "reference_unit_count": result.get("reference_unit_count", 0),
            "min_mean_word_confidence": min_confidence,
            "min_text_coverage_ratio": min_coverage,
        }

        failure_reason: Optional[str] = None
        if min_confidence is not None and mean_confidence < min_confidence:
            failure_reason = "whisperx_low_confidence"
        elif min_coverage is not None and coverage_ratio < min_coverage:
            failure_reason = "text_coverage_low"

        if failure_reason is None:
            score = 1.0
        else:
            score = 0.5

        return LayerResult(
            passed=failure_reason is None,
            score=score,
            failure_reason=failure_reason,
            metrics=metrics,
        )


class EndingArtifactVerifier(QualityVerifier):
    """
    Hard-fail verifier for sentence-ending prosodic artifacts.

    Detects two common TTS seed-lottery failures at the end of a segment:

    1. Elongated final syllable/word (drawn-out vowel, breathy tail).
       Measured by WhisperX word-duration ratio: last meaningful word
       duration vs. median word duration.
    2. Excessive trailing non-speech audio (uncut silence, long fade, or
       low-energy noise after the last word). Measured by frame-level RMS.

    Uses WhisperX word timestamps from the shared verification context so the
    expensive alignment runs only once per segment.
    """

    requires_whisperx = True

    @property
    def name(self) -> str:
        return "ending_artifact"

    def _config(self) -> Dict[str, Any]:
        return _layer_thresholds(self.name)

    def _last_active_time(
        self, audio: np.ndarray, sr: int, threshold_db: float = -50.0
    ) -> float:
        """Return the time (seconds) of the last frame above threshold_db."""
        frame_sec = 0.01
        frame = max(1, int(sr * frame_sec))
        n = len(audio) // frame
        if n == 0:
            return 0.0
        frames = audio[: n * frame].astype(np.float64).reshape(n, frame)
        rms = np.sqrt(np.mean(frames ** 2, axis=1))
        db = 20 * np.log10(np.maximum(rms, 1e-10))
        active = np.where(db > threshold_db)[0]
        if len(active) == 0:
            return 0.0
        return float(active[-1] + 1) * frame_sec

    def _last_meaningful_word(
        self, words: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Return the last word that is not pure punctuation and has duration."""
        punctuation_pattern = r"^[\u3001-\u303f\uff00-\uffef\"\'\"\"''.,!?;:@#$%^&*()\[\]{}|\\<>\u3000]+$"
        for w in reversed(words):
            text = w.get("word", "").strip()
            duration = w.get("end", 0.0) - w.get("start", 0.0)
            if duration > 0.0 and not re.match(punctuation_pattern, text):
                return w
        return words[-1] if words else None

    def _last_n_words_rms_db(
        self, audio: np.ndarray, sr: int, words: List[Dict[str, Any]], n: int = 6
    ) -> Optional[float]:
        """RMS dB of the final n words' audio region, or None if unavailable."""
        if not words:
            return None
        meaningful = [w for w in words if w.get("end", 0.0) > w.get("start", 0.0)]
        if not meaningful:
            return None
        region = meaningful[-n:] if len(meaningful) >= n else meaningful
        start = region[0].get("start", 0.0)
        end = region[-1].get("end", 0.0)
        if end <= start:
            return None
        start_sample = int(start * sr)
        end_sample = min(int(end * sr), len(audio))
        if end_sample <= start_sample:
            return None
        chunk = audio[start_sample:end_sample]
        rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
        return float(20 * np.log10(max(rms, 1e-10)))

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[VerificationContext] = None,
    ) -> LayerResult:
        cfg = self._config()
        max_last_word_ratio = _get_threshold(cfg, "max_last_word_ratio", self.name)
        max_trailing_audio_sec = _get_threshold(cfg, "max_trailing_audio_sec", self.name)
        min_word_count = _get_threshold(cfg, "min_word_count", self.name)

        result = context.whisperx_result if context is not None else None

        if result is None:
            return LayerResult(
                passed=False,
                score=0.0,
                failure_reason="whisperx_unavailable",
                metrics={
                    "max_last_word_ratio": max_last_word_ratio,
                    "max_trailing_audio_sec": max_trailing_audio_sec,
                    "min_word_count": min_word_count,
                },
            )

        audio, sr = sf.read(audio_path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        duration_sec = float(len(audio) / sr)

        # Flatten WhisperX word timestamps.
        words: List[Dict[str, Any]] = []
        for seg in result.get("aligned_segments", []):
            for w in seg.get("words", []):
                words.append(w)

        word_durations = [
            w.get("end", 0.0) - w.get("start", 0.0)
            for w in words
            if w.get("end", 0.0) > w.get("start", 0.0)
        ]

        failure_reason: Optional[str] = None
        metrics: Dict[str, Any] = {
            "word_count": len(words),
            "meaningful_word_count": len(word_durations),
            "audio_duration_sec": round(duration_sec, 3),
            "max_last_word_ratio": max_last_word_ratio,
            "max_trailing_audio_sec": max_trailing_audio_sec,
            "min_word_count": min_word_count,
        }

        # 1) Final-syllable elongation check (needs enough words for a stable median).
        min_words = int(min_word_count) if min_word_count is not None else 5
        if len(word_durations) >= min_words and len(word_durations) >= 2:
            median_duration = sorted(word_durations)[len(word_durations) // 2]
            last_word = self._last_meaningful_word(words)
            if last_word is not None:
                last_duration = last_word.get("end", 0.0) - last_word.get("start", 0.0)
                last_word_text = last_word.get("word", "")
                last_word_score = last_word.get("score")
                last_word_ratio = (
                    last_duration / median_duration if median_duration > 0 else 0.0
                )
                tail_rms_db = self._last_n_words_rms_db(audio, sr, words)

                metrics.update(
                    {
                        "last_word": last_word_text,
                        "last_word_duration_sec": round(last_duration, 3),
                        "last_word_score": round(last_word_score, 3)
                        if isinstance(last_word_score, (int, float))
                        else None,
                        "median_word_duration_sec": round(median_duration, 3),
                        "last_word_ratio": round(last_word_ratio, 3),
                        "tail_rms_db": round(tail_rms_db, 2)
                        if tail_rms_db is not None
                        else None,
                    }
                )

                if (
                    max_last_word_ratio is not None
                    and last_word_ratio > max_last_word_ratio
                ):
                    failure_reason = "last_word_too_long"

        # 2) Trailing non-speech audio check (works even for short sentences).
        #    We compare the time between the last WhisperX word and the file end
        #    with the RMS of the tail itself. A long, audible tail (constant noise
        #    or loud artifact) fails; a long quiet fade-out is allowed.
        max_trailing_tail_rms_db = _get_threshold(
            cfg, "max_trailing_tail_rms_db", self.name
        )
        if failure_reason is None and (
            max_trailing_audio_sec is not None
            or max_trailing_tail_rms_db is not None
        ):
            last_word = self._last_meaningful_word(words)
            speech_end_sec = (
                last_word.get("end", 0.0) if last_word is not None else 0.0
            )
            if speech_end_sec <= 0.0:
                # WhisperX did not give a usable end time; fall back to energy.
                speech_end_sec = self._last_active_time(audio, sr, threshold_db=-60.0)

            trailing_audio_sec = max(0.0, duration_sec - speech_end_sec)
            metrics["speech_end_sec"] = round(speech_end_sec, 3)
            metrics["trailing_audio_sec"] = round(trailing_audio_sec, 3)

            trailing_tail_rms_db: Optional[float] = None
            if trailing_audio_sec > 0.0:
                trailing_start_sample = int(speech_end_sec * sr)
                trailing_region = audio[trailing_start_sample:]
                if len(trailing_region) > 0:
                    # Use the last 500ms of the trailing region so a loud early
                    # part of a fade does not drown out a quiet tail.
                    tail_window_samples = min(int(sr * 0.5), len(trailing_region))
                    tail_chunk = trailing_region[-tail_window_samples:]
                    tail_rms = float(
                        np.sqrt(np.mean(tail_chunk.astype(np.float64) ** 2))
                    )
                    trailing_tail_rms_db = float(
                        20 * np.log10(max(tail_rms, 1e-10))
                    )

            metrics["trailing_tail_rms_db"] = (
                round(trailing_tail_rms_db, 2)
                if trailing_tail_rms_db is not None
                else None
            )

            long_tail = (
                max_trailing_audio_sec is not None
                and trailing_audio_sec > max_trailing_audio_sec
            )
            loud_tail = (
                max_trailing_tail_rms_db is not None
                and trailing_tail_rms_db is not None
                and trailing_tail_rms_db > max_trailing_tail_rms_db
            )

            if long_tail and loud_tail:
                failure_reason = "trailing_audio_too_long"

        score = 1.0 if failure_reason is None else 0.5

        return LayerResult(
            passed=failure_reason is None,
            score=score,
            failure_reason=failure_reason,
            metrics=metrics,
        )


class JiwerContentVerifier(QualityVerifier):
    """
    Hard-fail verifier comparing WhisperX transcription to original text.

    Checks:
    - Word Error Rate (WER) <= threshold
    - Character Error Rate (CER) <= threshold

    For Chinese content, WER is computed on character-split text so it behaves
    like a per-character word error rate.
    """

    requires_whisperx = True

    @property
    def name(self) -> str:
        return "jiwer_content"

    def _config(self) -> Dict[str, Any]:
        return _layer_thresholds(self.name)

    def _prepare_for_wer(self, text: str, language: str) -> str:
        """For CJK, split into space-separated characters for jiwer WER."""
        if language.lower().startswith("zh"):
            # Remove spaces first, then join each character with a space.
            chars = list(text.replace(" ", ""))
            return " ".join(chars)
        return text

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[VerificationContext] = None,
    ) -> LayerResult:
        cfg = self._config()
        max_wer = _get_threshold(cfg, "max_wer", self.name)
        max_cer = _get_threshold(cfg, "max_cer", self.name)

        result = context.whisperx_result if context is not None else None

        if result is None:
            return LayerResult(
                passed=False,
                score=0.0,
                failure_reason="whisperx_unavailable",
                metrics={
                    "max_wer": max_wer,
                    "max_cer": max_cer,
                },
            )

        reference = result.get("normalized_reference", "")
        hypothesis = result.get("normalized_transcription", "")

        if jiwer is None:
            logger.error("jiwer is not available")
            return LayerResult(
                passed=False,
                score=0.0,
                failure_reason="jiwer_unavailable",
                metrics={
                    "max_wer": max_wer,
                    "max_cer": max_cer,
                },
            )

        cer = float(jiwer.cer(reference, hypothesis))
        wer_input_ref = self._prepare_for_wer(reference, language)
        wer_input_hyp = self._prepare_for_wer(hypothesis, language)
        wer = float(jiwer.wer(wer_input_ref, wer_input_hyp))

        # Absolute error counts. A relative threshold alone is statistically
        # fragile on short sentences: 1 char wrong in 8 chars is already CER
        # 0.125 even when the audio is fine. A rate only fails the segment when
        # the absolute error count also reaches min_errors.
        min_errors = _get_threshold(cfg, "min_errors", self.name)
        min_errors = int(min_errors) if min_errors is not None else 0
        char_out = jiwer.process_characters(reference, hypothesis)
        char_errors = char_out.substitutions + char_out.deletions + char_out.insertions
        word_out = jiwer.process_words(wer_input_ref, wer_input_hyp)
        word_errors = word_out.substitutions + word_out.deletions + word_out.insertions

        metrics = {
            "wer": round(wer, 4),
            "cer": round(cer, 4),
            "char_errors": char_errors,
            "word_errors": word_errors,
            "min_errors": min_errors,
            "max_wer": max_wer,
            "max_cer": max_cer,
            "reference": reference,
            "hypothesis": hypothesis,
        }

        failure_reason: Optional[str] = None
        if max_wer is not None and wer > max_wer and word_errors >= min_errors:
            failure_reason = "wer_too_high"
        elif max_cer is not None and cer > max_cer and char_errors >= min_errors:
            failure_reason = "cer_too_high"

        if failure_reason is None:
            score = 1.0
        else:
            score = 0.5

        return LayerResult(
            passed=failure_reason is None,
            score=score,
            failure_reason=failure_reason,
            metrics=metrics,
        )


# ---------------------------------------------------------------------------
# Ticket 03: Speaker + spectral feedback layers (feedback-only)
# ---------------------------------------------------------------------------


def _onset_times(audio: np.ndarray, sr: int) -> np.ndarray:
    """
    Detect syllable onsets as short-term energy peaks (pure numpy).

    Chosen after measuring whisperx zh word timestamps: they are frame-quantized
    and unreliable for rate computation, so tempo drift is estimated from the
    audio directly. Returns onset times in seconds.
    """
    frame = max(1, int(sr * 0.02))
    n = len(audio) // frame
    if n < 3:
        return np.array([])
    frames = audio[: n * frame].astype(np.float64).reshape(n, frame)
    db = 20 * np.log10(np.maximum(np.sqrt(np.mean(frames ** 2, axis=1)), 1e-10))
    floor = np.percentile(db, 20)
    thresh = max(floor + 8, -45)
    min_sep = int(0.1 / 0.02)  # 100ms minimum separation
    peaks: list[int] = []
    for i in range(1, n - 1):
        if db[i] > thresh and db[i] >= db[i - 1] and db[i] > db[i + 1]:
            if not peaks or i - peaks[-1] >= min_sep:
                peaks.append(i)
    return np.array(peaks, dtype=float) * 0.02


class TempoDriftVerifier(QualityVerifier):
    """
    Feedback-only tempo-stability verifier.

    Compares onset (syllable) rate between the first and second half of the
    speech-active region. Calibrated on verified segments: stable narration
    scores <=1.11, a known speed-shifting take scored 1.35 — hence the 1.25
    default threshold. Feedback-only until more data accrues.
    """

    @property
    def name(self) -> str:
        return "tempo_drift"

    def _config(self) -> Dict[str, Any]:
        return _layer_thresholds(self.name)

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[VerificationContext] = None,
    ) -> LayerResult:
        cfg = self._config()
        max_drift = _get_threshold(cfg, "max_drift_ratio", self.name)

        audio, sr = sf.read(audio_path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        peaks = _onset_times(audio, sr)
        metrics: Dict[str, Any] = {
            "onset_count": int(len(peaks)),
            "max_drift_ratio": max_drift,
        }
        if len(peaks) < 6:
            # Too short/sparse to judge — report but never flag.
            metrics["note"] = "insufficient_onsets"
            return LayerResult(passed=True, score=0.0, failure_reason=None, metrics=metrics)

        t0, t1 = peaks[0], peaks[-1]
        mid = (t0 + t1) / 2
        rate_first = len([p for p in peaks if p < mid]) / max(mid - t0, 0.1)
        rate_second = len([p for p in peaks if p >= mid]) / max(t1 - mid, 0.1)
        drift = max(rate_first, rate_second) / max(min(rate_first, rate_second), 0.1)

        metrics.update(
            {
                "drift_ratio": round(float(drift), 3),
                "rate_first_half": round(float(rate_first), 2),
                "rate_second_half": round(float(rate_second), 2),
            }
        )
        flagged = max_drift is not None and drift > max_drift
        metrics["drift_flagged"] = bool(flagged)
        # Feedback-only: score reflects stability but never fails the segment.
        score = max(0.0, min(1.0, 1.0 - (drift - 1.0)))
        return LayerResult(passed=True, score=score, failure_reason=None, metrics=metrics)


class ResemblyzerSpeakerVerifier(QualityVerifier):
    """
    Feedback-only speaker similarity verifier using Resemblyzer.

    Compares the generated segment's voice embedding to the reference voice
    embedding. Does not hard-fail the segment; metrics feed the feedback loop.
    """

    @property
    def name(self) -> str:
        return "resemblyzer_speaker"

    def _config(self) -> Dict[str, Any]:
        return _layer_thresholds(self.name)

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[VerificationContext] = None,
    ) -> LayerResult:
        cfg = self._config()
        min_similarity = _get_threshold(cfg, "min_similarity", self.name)

        if not reference_voice_path:
            return LayerResult(
                passed=True,
                score=0.0,
                failure_reason=None,
                metrics={
                    "cosine_similarity": None,
                    "min_similarity": min_similarity,
                    "note": "no_reference_voice",
                },
            )

        run_fn, import_error = _load_runner("run_resemblyzer_speaker", "Resemblyzer")
        if run_fn is None:
            return LayerResult(
                passed=True,
                score=0.0,
                failure_reason=None,
                metrics={
                    "cosine_similarity": None,
                    "min_similarity": min_similarity,
                    "error": str(import_error),
                },
            )

        result = run_fn(
            audio_path=audio_path,
            reference_voice_path=reference_voice_path,
        )

        if result is None:
            return LayerResult(
                passed=True,
                score=0.0,
                failure_reason=None,
                metrics={
                    "cosine_similarity": None,
                    "min_similarity": min_similarity,
                    "note": "resemblyzer_unavailable",
                },
            )

        similarity = float(result.get("cosine_similarity", 0.0))
        score = max(0.0, min(1.0, similarity))

        return LayerResult(
            passed=True,
            score=score,
            failure_reason=None,
            metrics={
                "cosine_similarity": similarity,
                "min_similarity": min_similarity,
                "embedding_shape": result.get("embedding_shape"),
                "reference_embedding_shape": result.get("reference_embedding_shape"),
            },
        )


class LibrosaSpectralVerifier(QualityVerifier):
    """
    Feedback-only spectral verifier using Librosa.

    Computes MFCC MSE and spectral contrast ratio between generated segment and
    reference voice. Does not hard-fail the segment; metrics feed the feedback
    loop.
    """

    @property
    def name(self) -> str:
        return "librosa_spectral"

    def _config(self) -> Dict[str, Any]:
        return _layer_thresholds(self.name)

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[VerificationContext] = None,
    ) -> LayerResult:
        cfg = self._config()
        max_mfcc_mse = _get_threshold(cfg, "max_mfcc_mse", self.name)
        min_spectral_contrast = _get_threshold(cfg, "min_spectral_contrast", self.name)

        if not reference_voice_path:
            return LayerResult(
                passed=True,
                score=0.0,
                failure_reason=None,
                metrics={
                    "mfcc_mse": None,
                    "spectral_contrast_ratio": None,
                    "max_mfcc_mse": max_mfcc_mse,
                    "min_spectral_contrast": min_spectral_contrast,
                    "note": "no_reference_voice",
                },
            )

        run_fn, import_error = _load_runner("run_librosa_spectral", "Librosa")
        if run_fn is None:
            return LayerResult(
                passed=True,
                score=0.0,
                failure_reason=None,
                metrics={
                    "mfcc_mse": None,
                    "spectral_contrast_ratio": None,
                    "max_mfcc_mse": max_mfcc_mse,
                    "min_spectral_contrast": min_spectral_contrast,
                    "error": str(import_error),
                },
            )

        result = run_fn(
            audio_path=audio_path,
            reference_voice_path=reference_voice_path,
        )

        if result is None:
            return LayerResult(
                passed=True,
                score=0.0,
                failure_reason=None,
                metrics={
                    "mfcc_mse": None,
                    "spectral_contrast_ratio": None,
                    "max_mfcc_mse": max_mfcc_mse,
                    "min_spectral_contrast": min_spectral_contrast,
                    "note": "librosa_unavailable",
                },
            )

        mfcc_mse = float(result.get("mfcc_mse", 0.0))
        sc_ratio = float(result.get("spectral_contrast_ratio", 0.0))
        score = max(0.0, min(1.0, sc_ratio))

        return LayerResult(
            passed=True,
            score=score,
            failure_reason=None,
            metrics={
                "mfcc_mse": mfcc_mse,
                "spectral_contrast_ratio": sc_ratio,
                "max_mfcc_mse": max_mfcc_mse,
                "min_spectral_contrast": min_spectral_contrast,
                "audio_duration_sec": result.get("audio_duration_sec"),
                "reference_duration_sec": result.get("reference_duration_sec"),
            },
        )
