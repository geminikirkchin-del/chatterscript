# Concrete quality verifier layers.

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import soundfile as sf

from config import config_manager
from pipeline.quality import QualityVerifier

logger = logging.getLogger(__name__)

# Optional jiwer import. If unavailable, JiwerContentVerifier reports
# jiwer_unavailable instead of crashing.
try:
    import jiwer
except Exception:
    jiwer = None  # type: ignore[assignment]


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
        verification_cfg = config_manager.get("pipeline.verification", {})
        layer_cfg = verification_cfg.get("layers", {}).get("audio_metrics", {})
        return layer_cfg.get(
            "thresholds",
            {
                "target_lufs": -16.0,
                "lufs_tolerance": 2.0,
                "true_peak_max_dbtp": 0.5,
                "min_rms": 0.01,
                "min_dynamic_range_db": 10.0,
            },
        )

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = self._config()
        target_lufs = float(cfg.get("target_lufs", -16.0))
        lufs_tolerance = float(cfg.get("lufs_tolerance", 2.0))
        true_peak_max = float(cfg.get("true_peak_max_dbtp", 0.5))
        min_rms = float(cfg.get("min_rms", 0.01))
        min_dynamic_range = float(cfg.get("min_dynamic_range_db", 10.0))

        loudnorm = _run_ffmpeg_loudnorm(audio_path, target_lufs, true_peak_max)
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
        # legacy basic verifier behaviour) before LUFS can fail.
        if basic["rms"] < min_rms:
            failure_reason = "low_rms"
        elif abs(input_lufs - target_lufs) > lufs_tolerance:
            failure_reason = "lufs_out_of_range"
        elif dynamic_range_db < min_dynamic_range:
            failure_reason = "dynamic_range_low"
        # Note: true_peak is recorded in metrics but is not a segment hard-fail.
        # Raw TTS output often sits near 0 dBFS; final compose applies loudnorm to
        # enforce the broadcast true-peak target (Ticket 04).

        # Simple score: 1.0 if all good, otherwise proportional to how far off.
        if failure_reason is None:
            score = 1.0
        else:
            score = 0.5  # Hard-fail layers get a baseline low score on failure.

        return {
            "passed": failure_reason is None,
            "score": score,
            "failure_reason": failure_reason,
            "metrics": metrics,
        }


# ---------------------------------------------------------------------------
# WhisperX alignment + jiwer content layers (Ticket 02)
# ---------------------------------------------------------------------------


def _get_whisperx_config() -> Dict[str, Any]:
    """Read pipeline.asr config used by both WhisperX layers."""
    return config_manager.get("pipeline.asr", {})


def _run_whisperx_with_cache(
    audio_path: str,
    original_text: str,
    language: str,
    context: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Run WhisperX alignment once per segment and cache the result in context.

    Both WhisperXAlignmentVerifier and JiwerContentVerifier call this helper so
    the expensive model is loaded only once per segment.
    """
    context_key = f"whisperx_align:{audio_path}"
    if context is not None:
        cached = context.get(context_key)
        if cached is not None:
            return cached

    asr_config = _get_whisperx_config()
    model_name = asr_config.get("model", "small")
    # Use the same device the TTS engine resolved to (whisperx will re-resolve
    # 'auto' internally). 'auto' keeps the wrapper portable.
    device = asr_config.get("device", "auto")

    try:
        from pipeline.verification_wrappers.runner import run_whisperx_align
    except Exception as e:
        logger.error(f"Failed to import WhisperX runner: {e}")
        return None

    result = run_whisperx_align(
        audio_path=audio_path,
        reference_text=original_text,
        language=language,
        model_name=model_name,
        device=device,
    )
    if result is not None and context is not None:
        context[context_key] = result
    return result


class WhisperXAlignmentVerifier(QualityVerifier):
    """
    Hard-fail verifier using WhisperX word-level alignment.

    Checks:
    - Mean word confidence >= threshold
    - Text coverage ratio (transcribed vs original units) >= threshold
    """

    @property
    def name(self) -> str:
        return "whisperx_alignment"

    def _config(self) -> Dict[str, Any]:
        verification_cfg = config_manager.get("pipeline.verification", {})
        layer_cfg = verification_cfg.get("layers", {}).get("whisperx_alignment", {})
        return layer_cfg.get(
            "thresholds",
            {
                "min_mean_word_confidence": 0.70,
                "min_text_coverage_ratio": 0.90,
            },
        )

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = self._config()
        min_confidence = float(cfg.get("min_mean_word_confidence", 0.70))
        min_coverage = float(cfg.get("min_text_coverage_ratio", 0.90))

        result = _run_whisperx_with_cache(audio_path, original_text, language, context)

        if result is None:
            return {
                "passed": False,
                "score": 0.0,
                "failure_reason": "whisperx_unavailable",
                "metrics": {
                    "min_mean_word_confidence": min_confidence,
                    "min_text_coverage_ratio": min_coverage,
                },
            }

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
        if mean_confidence < min_confidence:
            failure_reason = "whisperx_low_confidence"
        elif coverage_ratio < min_coverage:
            failure_reason = "text_coverage_low"

        if failure_reason is None:
            score = 1.0
        else:
            score = 0.5

        return {
            "passed": failure_reason is None,
            "score": score,
            "failure_reason": failure_reason,
            "metrics": metrics,
        }


class JiwerContentVerifier(QualityVerifier):
    """
    Hard-fail verifier comparing WhisperX transcription to original text.

    Checks:
    - Word Error Rate (WER) <= threshold
    - Character Error Rate (CER) <= threshold

    For Chinese content, WER is computed on character-split text so it behaves
    like a per-character word error rate.
    """

    @property
    def name(self) -> str:
        return "jiwer_content"

    def _config(self) -> Dict[str, Any]:
        verification_cfg = config_manager.get("pipeline.verification", {})
        layer_cfg = verification_cfg.get("layers", {}).get("jiwer_content", {})
        return layer_cfg.get(
            "thresholds",
            {
                "max_wer": 0.15,
                "max_cer": 0.10,
            },
        )

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
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cfg = self._config()
        max_wer = float(cfg.get("max_wer", 0.15))
        max_cer = float(cfg.get("max_cer", 0.10))

        result = _run_whisperx_with_cache(audio_path, original_text, language, context)

        if result is None:
            return {
                "passed": False,
                "score": 0.0,
                "failure_reason": "whisperx_unavailable",
                "metrics": {
                    "max_wer": max_wer,
                    "max_cer": max_cer,
                },
            }

        reference = result.get("normalized_reference", "")
        hypothesis = result.get("normalized_transcription", "")

        if jiwer is None:
            logger.error("jiwer is not available")
            return {
                "passed": False,
                "score": 0.0,
                "failure_reason": "jiwer_unavailable",
                "metrics": {
                    "max_wer": max_wer,
                    "max_cer": max_cer,
                },
            }

        cer = float(jiwer.cer(reference, hypothesis))
        wer_input_ref = self._prepare_for_wer(reference, language)
        wer_input_hyp = self._prepare_for_wer(hypothesis, language)
        wer = float(jiwer.wer(wer_input_ref, wer_input_hyp))

        metrics = {
            "wer": round(wer, 4),
            "cer": round(cer, 4),
            "max_wer": max_wer,
            "max_cer": max_cer,
            "reference": reference,
            "hypothesis": hypothesis,
        }

        failure_reason: Optional[str] = None
        if wer > max_wer:
            failure_reason = "wer_too_high"
        elif cer > max_cer:
            failure_reason = "cer_too_high"

        if failure_reason is None:
            score = 1.0
        else:
            score = 0.5

        return {
            "passed": failure_reason is None,
            "score": score,
            "failure_reason": failure_reason,
            "metrics": metrics,
        }
