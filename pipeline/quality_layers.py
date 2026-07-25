# Concrete quality verifier layers.

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import soundfile as sf

from config import config_manager
from pipeline.quality import LayerResult, QualityVerifier

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
        context: Optional[Dict[str, Any]] = None,
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
        # legacy basic verifier behaviour) before LUFS can fail.
        # A threshold of None means the key is not configured; that check is skipped.
        if min_rms is not None and basic["rms"] < min_rms:
            failure_reason = "low_rms"
        elif (
            target_lufs is not None
            and lufs_tolerance is not None
            and abs(input_lufs - target_lufs) > lufs_tolerance
        ):
            failure_reason = "lufs_out_of_range"
        elif min_dynamic_range is not None and dynamic_range_db < min_dynamic_range:
            failure_reason = "dynamic_range_low"
        # Note: true_peak is recorded in metrics but is not a segment hard-fail.
        # Raw TTS output often sits near 0 dBFS; final compose applies loudnorm to
        # enforce the broadcast true-peak target (Ticket 04).

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

    run_fn, import_error = _load_runner("run_whisperx_align", "WhisperX")
    if run_fn is None:
        return None

    result = run_fn(
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
        return _layer_thresholds(self.name)

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[Dict[str, Any]] = None,
    ) -> LayerResult:
        cfg = self._config()
        min_confidence = _get_threshold(cfg, "min_mean_word_confidence", self.name)
        min_coverage = _get_threshold(cfg, "min_text_coverage_ratio", self.name)

        result = _run_whisperx_with_cache(audio_path, original_text, language, context)

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
        context: Optional[Dict[str, Any]] = None,
    ) -> LayerResult:
        cfg = self._config()
        max_wer = _get_threshold(cfg, "max_wer", self.name)
        max_cer = _get_threshold(cfg, "max_cer", self.name)

        result = _run_whisperx_with_cache(audio_path, original_text, language, context)

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

        metrics = {
            "wer": round(wer, 4),
            "cer": round(cer, 4),
            "max_wer": max_wer,
            "max_cer": max_cer,
            "reference": reference,
            "hypothesis": hypothesis,
        }

        failure_reason: Optional[str] = None
        if max_wer is not None and wer > max_wer:
            failure_reason = "wer_too_high"
        elif max_cer is not None and cer > max_cer:
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
        context: Optional[Dict[str, Any]] = None,
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
        context: Optional[Dict[str, Any]] = None,
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
