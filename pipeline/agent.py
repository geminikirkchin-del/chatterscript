# Rule-based AI agent for TTS pipeline parameter adjustment.

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TEMPERATURE_DELTA = -0.2
DEFAULT_CFG_WEIGHT_DELTA = 0.1
DEFAULT_EXAGGERATION_DELTA = -0.1
DEFAULT_SEED_DELTA = 1


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
                if fb.rating == "reject" and _params_signature(fb.extra.get("params", {})) == sig:
                    return True
        except Exception as e:
            logger.warning(f"Failed to read feedback for agent: {e}")
        return False

    def decide(
        self,
        base_params: Dict[str, Any],
        audio_failure: Optional[str],
        asr_failure: Optional[str],
    ) -> AgentDecision:
        """
        Inspect failure metadata and return adjusted generation parameters.

        Args:
            base_params: current generation parameters.
            audio_failure: audio verification failure reason (or None).
            asr_failure: ASR verification failure reason (or None).

        Returns:
            AgentDecision with adjusted parameters and explanation.
        """
        params = dict(base_params)
        deltas: Dict[str, Any] = {}
        reasons: list[str] = []

        # ASR mismatch: lower temperature (more deterministic), raise cfg_weight.
        if asr_failure == "asr_mismatch":
            old_temp = params.get("temperature", 0.8)
            new_temp = max(0.1, old_temp + self.temperature_delta)
            params["temperature"] = round(new_temp, 2)
            deltas["temperature"] = params["temperature"] - old_temp

            old_cfg = params.get("cfg_weight", 0.5)
            new_cfg = min(1.0, old_cfg + self.cfg_weight_delta)
            params["cfg_weight"] = round(new_cfg, 2)
            deltas["cfg_weight"] = params["cfg_weight"] - old_cfg

            reasons.append("asr_mismatch: lower temperature, raise cfg_weight")

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

        # Avoid recently rejected parameter combinations by bumping seed.
        if self._recently_rejected_params(params):
            old_seed = params.get("seed", 0)
            params["seed"] = old_seed + self.seed_delta
            deltas["seed"] = deltas.get("seed", 0) + self.seed_delta
            reasons.append("avoid recently rejected params: change seed")

        reason = "; ".join(reasons) if reasons else "no adjustment"
        logger.info(f"AI agent decision: {reason}; deltas={deltas}")
        return AgentDecision(gen_params=params, reason=reason, deltas=deltas)
