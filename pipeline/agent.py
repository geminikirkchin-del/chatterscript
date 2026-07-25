# Rule-based AI agent for TTS pipeline parameter adjustment.

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TEMPERATURE_DELTA = -0.2
DEFAULT_CFG_WEIGHT_DELTA = 0.1
DEFAULT_EXAGGERATION_DELTA = -0.1
DEFAULT_SEED_DELTA = 1

# Quality thresholds for the feedback-only speaker / spectral layers.
DEFAULT_SPEAKER_SIMILARITY_THRESHOLD = 0.75
DEFAULT_SPECTRAL_CONTRAST_THRESHOLD = 0.80
DEFAULT_MFCC_MSE_THRESHOLD = 0.05


def _params_signature(params: Dict[str, Any]) -> str:
    """Stable string key for a parameter set (for feedback matching)."""
    return (
        f"t{params.get('temperature', 0.8):.2f}_"
        f"c{params.get('cfg_weight', 0.5):.2f}_"
        f"e{params.get('exaggeration', 0.5):.2f}_"
        f"s{params.get('seed', 0)}"
    )


@dataclass
class AgentDecision:
    """Parameter adjustment decision produced by the AI agent."""

    gen_params: Dict[str, Any]
    reason: str
    deltas: Dict[str, Any]


class ParameterAgent:
    """
    Local rule-based agent that adjusts generation parameters based on failure type.

    This is intentionally simple: it maps failure categories to deterministic
    parameter deltas. It also reads recent feedback to avoid recently rejected
    parameter combinations.
    """

    def __init__(
        self,
        temperature_delta: float = DEFAULT_TEMPERATURE_DELTA,
        cfg_weight_delta: float = DEFAULT_CFG_WEIGHT_DELTA,
        exaggeration_delta: float = DEFAULT_EXAGGERATION_DELTA,
        seed_delta: int = DEFAULT_SEED_DELTA,
        feedback_store=None,
    ):
        self.temperature_delta = temperature_delta
        self.cfg_weight_delta = cfg_weight_delta
        self.exaggeration_delta = exaggeration_delta
        self.seed_delta = seed_delta
        self.feedback_store = feedback_store

    def _recently_rejected_params(self, params: Dict[str, Any]) -> bool:
        """Check if a parameter set was recently rejected by the user."""
        if self.feedback_store is None:
            return False
        try:
            recent = self.feedback_store.read_recent(limit=200)
            sig = _params_signature(params)
            for fb in recent:
                rejected_params = fb.extra.get("gen_params") or fb.extra.get("params", {})
                if fb.rating == "reject" and _params_signature(rejected_params) == sig:
                    return True
        except Exception as e:
            logger.warning(f"Failed to read feedback for agent: {e}")
        return False

    def _feedback_metrics_for_params(
        self, params: Dict[str, Any], limit: int = 200
    ) -> Tuple[float, float, float, int]:
        """
        Read recent metrics entries for the same parameter signature.

        Returns average (speaker_similarity, spectral_contrast_ratio, mfcc_mse, count).
        Missing values are ignored for their respective averages.
        """
        if self.feedback_store is None:
            return 0.0, 0.0, 0.0, 0
        try:
            metrics_entries = self.feedback_store.read_recent_metrics(limit=limit)
        except Exception as e:
            logger.warning(f"Failed to read metrics feedback for agent: {e}")
            return 0.0, 0.0, 0.0, 0

        sig = _params_signature(params)
        similarities: List[float] = []
        contrast_ratios: List[float] = []
        mfcc_mses: List[float] = []

        for fb in metrics_entries:
            if _params_signature(fb.extra.get("gen_params", {})) != sig:
                continue
            layer_results = fb.extra.get("layer_results", {})
            speaker = layer_results.get("resemblyzer_speaker", {}).get("metrics", {}).get("cosine_similarity")
            spectral = layer_results.get("librosa_spectral", {}).get("metrics", {}).get("spectral_contrast_ratio")
            mfcc = layer_results.get("librosa_spectral", {}).get("metrics", {}).get("mfcc_mse")
            if isinstance(speaker, (int, float)):
                similarities.append(float(speaker))
            if isinstance(spectral, (int, float)):
                contrast_ratios.append(float(spectral))
            if isinstance(mfcc, (int, float)):
                mfcc_mses.append(float(mfcc))

        avg_similarity = sum(similarities) / len(similarities) if similarities else 0.0
        avg_contrast = sum(contrast_ratios) / len(contrast_ratios) if contrast_ratios else 0.0
        avg_mfcc_mse = sum(mfcc_mses) / len(mfcc_mses) if mfcc_mses else 0.0
        count = len(similarities)
        return avg_similarity, avg_contrast, avg_mfcc_mse, count

    def _adjust_for_feedback(
        self,
        params: Dict[str, Any],
        deltas: Dict[str, Any],
        reasons: List[str],
        layer_results: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Apply feedback-based adjustments using the latest segment metrics and the
        historical average for the same parameter set.

        This is intentionally conservative: it nudges parameters only when the
        feedback-only quality layers show a clear trend below their targets.
        """
        layer_results = layer_results or {}

        # Latest segment metrics.
        speaker_metrics = layer_results.get("resemblyzer_speaker", {}).get("metrics", {})
        spectral_metrics = layer_results.get("librosa_spectral", {}).get("metrics", {})
        latest_similarity = speaker_metrics.get("cosine_similarity")
        latest_contrast = spectral_metrics.get("spectral_contrast_ratio")
        latest_mfcc_mse = spectral_metrics.get("mfcc_mse")

        # Historical averages for the same parameter signature.
        avg_similarity, avg_contrast, avg_mfcc_mse, count = self._feedback_metrics_for_params(params)

        # Decide which signal to act on: prefer the latest segment, fall back to
        # the historical average when we have enough samples.
        similarity_low = False
        contrast_low = False
        mfcc_mse_high = False

        if isinstance(latest_similarity, (int, float)):
            similarity_low = float(latest_similarity) < DEFAULT_SPEAKER_SIMILARITY_THRESHOLD
        elif count >= 2 and avg_similarity > 0:
            similarity_low = avg_similarity < DEFAULT_SPEAKER_SIMILARITY_THRESHOLD

        if isinstance(latest_contrast, (int, float)):
            contrast_low = float(latest_contrast) < DEFAULT_SPECTRAL_CONTRAST_THRESHOLD
        elif count >= 2 and avg_contrast > 0:
            contrast_low = avg_contrast < DEFAULT_SPECTRAL_CONTRAST_THRESHOLD

        if isinstance(latest_mfcc_mse, (int, float)):
            mfcc_mse_high = float(latest_mfcc_mse) > DEFAULT_MFCC_MSE_THRESHOLD
        elif count >= 2 and avg_mfcc_mse > 0:
            mfcc_mse_high = avg_mfcc_mse > DEFAULT_MFCC_MSE_THRESHOLD

        if similarity_low:
            old_temp = params.get("temperature", 0.8)
            new_temp = max(0.1, old_temp + self.temperature_delta)
            params["temperature"] = round(new_temp, 2)
            deltas["temperature"] = deltas.get("temperature", 0) + (params["temperature"] - old_temp)

            old_cfg = params.get("cfg_weight", 0.5)
            new_cfg = min(1.0, old_cfg + self.cfg_weight_delta)
            params["cfg_weight"] = round(new_cfg, 2)
            deltas["cfg_weight"] = deltas.get("cfg_weight", 0) + (params["cfg_weight"] - old_cfg)

            old_seed = params.get("seed", 0)
            params["seed"] = old_seed + self.seed_delta
            deltas["seed"] = deltas.get("seed", 0) + self.seed_delta
            reasons.append(
                f"speaker_similarity_low: lower temp, raise cfg_weight, change seed "
                f"(latest={latest_similarity}, avg={avg_similarity:.3f})"
            )

        if contrast_low or mfcc_mse_high:
            old_exag = params.get("exaggeration", 0.5)
            new_exag = max(0.25, old_exag + self.exaggeration_delta)
            params["exaggeration"] = round(new_exag, 2)
            deltas["exaggeration"] = deltas.get("exaggeration", 0) + (params["exaggeration"] - old_exag)

            old_seed = params.get("seed", 0)
            params["seed"] = old_seed + self.seed_delta
            deltas["seed"] = deltas.get("seed", 0) + self.seed_delta
            reason_parts = ["spectral_drift"]
            if contrast_low:
                reason_parts.append(f"contrast_low (latest={latest_contrast}, avg={avg_contrast:.3f})")
            if mfcc_mse_high:
                reason_parts.append(f"mfcc_mse_high (latest={latest_mfcc_mse}, avg={avg_mfcc_mse:.3f})")
            reasons.append(f"{', '.join(reason_parts)}: lower exaggeration, change seed")

    def decide(
        self,
        base_params: Dict[str, Any],
        audio_failure: Optional[str],
        asr_failure: Optional[str],
        audio_metrics: Optional[Dict[str, Any]] = None,
        layer_results: Optional[Dict[str, Any]] = None,
    ) -> AgentDecision:
        """
        Inspect failure metadata and return adjusted generation parameters.

        Args:
            base_params: current generation parameters.
            audio_failure: audio verification failure reason (or None).
            asr_failure: ASR verification failure reason (or None).
            audio_metrics: optional basic audio metrics from the last attempt.
            layer_results: optional per-layer quality results from the last attempt.

        Returns:
            AgentDecision with adjusted parameters and explanation.
        """
        params = dict(base_params)
        deltas: Dict[str, Any] = {}
        reasons: list[str] = []

        # ASR / content mismatch: lower temperature (more deterministic), raise cfg_weight.
        content_failures = (
            "asr_mismatch",
            "whisperx_low_confidence",
            "text_coverage_low",
            "wer_too_high",
            "cer_too_high",
        )
        if asr_failure in content_failures:
            old_temp = params.get("temperature", 0.8)
            new_temp = max(0.1, old_temp + self.temperature_delta)
            params["temperature"] = round(new_temp, 2)
            deltas["temperature"] = params["temperature"] - old_temp

            old_cfg = params.get("cfg_weight", 0.5)
            new_cfg = min(1.0, old_cfg + self.cfg_weight_delta)
            params["cfg_weight"] = round(new_cfg, 2)
            deltas["cfg_weight"] = params["cfg_weight"] - old_cfg

            reasons.append(f"{asr_failure}: lower temperature, raise cfg_weight")

        # Audio silence / low RMS: lower exaggeration.
        if audio_failure in ("long_silence", "low_rms"):
            old_exag = params.get("exaggeration", 0.5)
            new_exag = max(0.25, old_exag + self.exaggeration_delta)
            params["exaggeration"] = round(new_exag, 2)
            deltas["exaggeration"] = params["exaggeration"] - old_exag
            reasons.append(f"{audio_failure}: lower exaggeration")

        # Clipping: lower exaggeration.
        if audio_failure == "clipping":
            old_exag = params.get("exaggeration", 0.5)
            new_exag = max(0.25, old_exag + self.exaggeration_delta)
            params["exaggeration"] = round(new_exag, 2)
            deltas["exaggeration"] = params["exaggeration"] - old_exag
            reasons.append("clipping: lower exaggeration")

        # Duration deviation: adjust speed_factor toward the expected duration.
        if audio_failure == "duration_deviation":
            metrics = audio_metrics or {}
            actual_duration = metrics.get("actual_duration")
            expected_duration = metrics.get("expected_duration")
            if actual_duration is not None and expected_duration and expected_duration > 0:
                old_speed = params.get("speed_factor", 1.0)
                ratio = actual_duration / expected_duration
                # If audio is too long, raise speed; if too short, lower speed.
                new_speed = round(max(0.25, min(4.0, old_speed * ratio)), 2)
                params["speed_factor"] = new_speed
                deltas["speed_factor"] = new_speed - old_speed
                reasons.append(f"duration_deviation: speed {old_speed} -> {new_speed}")
            else:
                # Fallback: nudge speed up slightly.
                old_speed = params.get("speed_factor", 1.0)
                new_speed = round(min(4.0, old_speed + 0.05), 2)
                params["speed_factor"] = new_speed
                deltas["speed_factor"] = new_speed - old_speed
                reasons.append("duration_deviation: nudge speed_factor up")

        # Repeated combined failures: also change seed for variety.
        if audio_failure and asr_failure:
            old_seed = params.get("seed", 0)
            params["seed"] = old_seed + self.seed_delta
            deltas["seed"] = self.seed_delta
            reasons.append("combined failures: change seed")
        elif asr_failure or audio_failure:
            # Single failure type: still nudge seed on the first agent attempt.
            old_seed = params.get("seed", 0)
            params["seed"] = old_seed + self.seed_delta
            deltas["seed"] = self.seed_delta
            reasons.append("change seed")
        else:
            # Generation itself failed (no audio produced). Change seed and lower temp slightly.
            old_seed = params.get("seed", 0)
            params["seed"] = old_seed + self.seed_delta
            deltas["seed"] = self.seed_delta
            old_temp = params.get("temperature", 0.8)
            new_temp = max(0.1, old_temp + self.temperature_delta)
            params["temperature"] = round(new_temp, 2)
            deltas["temperature"] = params["temperature"] - old_temp
            reasons.append("generation failure: change seed, lower temperature")

        # Feedback-only layer adjustments (speaker similarity / spectral drift).
        # These layers don't hard-fail segments, but their metrics guide the agent
        # toward parameter combinations that keep the cloned voice stable across
        # a long script.
        self._adjust_for_feedback(params, deltas, reasons, layer_results)

        # Avoid recently rejected parameter combinations by bumping seed.
        if self._recently_rejected_params(params):
            old_seed = params.get("seed", 0)
            params["seed"] = old_seed + self.seed_delta
            deltas["seed"] = deltas.get("seed", 0) + self.seed_delta
            reasons.append("avoid recently rejected params: change seed")

        reason = "; ".join(reasons) if reasons else "no adjustment"
        logger.info(f"AI agent decision: {reason}; deltas={deltas}")
        return AgentDecision(gen_params=params, reason=reason, deltas=deltas)
