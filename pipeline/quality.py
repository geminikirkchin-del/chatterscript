# Multi-layer quality verification interface for the TTS pipeline.

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class QualityVerificationResult:
    """Aggregated result from all verification layers for one segment."""

    passed: bool
    overall_score: float
    failure_reason: Optional[str]
    layer_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class QualityVerifier(ABC):
    """Abstract interface for a single verification layer."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique layer name, used as a key in layer_results."""
        ...

    @abstractmethod
    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Run the verification layer and return a dictionary with at least:

        - passed (bool)
        - score (float)
        - failure_reason (Optional[str])
        - metrics (Dict[str, Any])

        The returned dictionary is stored under layer_results[self.name].

        Args:
            context: Optional shared dict that layers can read/write to avoid
                re-running expensive analysis (e.g. WhisperX transcription).
        """
        ...


class PipelineQualityVerifier:
    """
    Orchestrator that runs configured verification layers and produces a single
    QualityVerificationResult.

    Layers marked as hard-fail determine whether the segment passes. Layers
    marked as feedback-only contribute metrics and scores but never cause failure.
    """

    def __init__(
        self,
        layers: List[QualityVerifier],
        layer_config: Dict[str, Dict[str, Any]],
    ):
        self.layers = layers
        self.layer_config = layer_config

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]] = None) -> "PipelineQualityVerifier":
        """Build a verifier from pipeline.verification config."""
        if config is None:
            from config import config_manager

            config = config_manager.get("pipeline.verification", {})

        layer_config = config.get("layers", {})
        layers: List[QualityVerifier] = []

        # Basic audio verification is always enabled and is treated as a hard-fail layer.
        from pipeline.verifier_adapter import BasicAudioVerifierAdapter

        layers.append(BasicAudioVerifierAdapter())

        # FFmpeg audio metrics layer.
        if layer_config.get("audio_metrics", {}).get("enabled", True):
            from pipeline.quality_layers import FFmpegAudioMetricsVerifier

            layers.append(FFmpegAudioMetricsVerifier())

        # WhisperX alignment layer: word-level confidence + text coverage.
        if layer_config.get("whisperx_alignment", {}).get("enabled", True):
            from pipeline.quality_layers import WhisperXAlignmentVerifier

            layers.append(WhisperXAlignmentVerifier())

        # jiwer content layer: WER / CER against original text.
        if layer_config.get("jiwer_content", {}).get("enabled", True):
            from pipeline.quality_layers import JiwerContentVerifier

            layers.append(JiwerContentVerifier())

        # Speaker + spectral feedback layers are added by later tickets.

        return cls(layers=layers, layer_config=layer_config)

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
    ) -> QualityVerificationResult:
        """Run all configured layers and aggregate the result."""
        layer_results: Dict[str, Dict[str, Any]] = {}
        shared_context: Dict[str, Any] = {}
        overall_passed = True
        failure_reason: Optional[str] = None
        scores: List[float] = []

        for layer in self.layers:
            try:
                result = layer.verify(
                    audio_path=audio_path,
                    original_text=original_text,
                    reference_voice_path=reference_voice_path,
                    language=language,
                    expected_duration=expected_duration,
                    context=shared_context,
                )
            except Exception as e:
                logger.warning(f"Verification layer '{layer.name}' failed: {e}")
                result = {
                    "passed": False,
                    "score": 0.0,
                    "failure_reason": f"{layer.name}_exception",
                    "metrics": {"error": str(e)},
                }

            layer_results[layer.name] = result
            scores.append(float(result.get("score", 0.0)))

            layer_cfg = self.layer_config.get(layer.name, {})
            is_hardfail = layer_cfg.get("hardfail", True)

            if is_hardfail and not result.get("passed", False):
                overall_passed = False
                if failure_reason is None:
                    failure_reason = result.get("failure_reason")

        overall_score = sum(scores) / len(scores) if scores else 0.0
        return QualityVerificationResult(
            passed=overall_passed,
            overall_score=overall_score,
            failure_reason=failure_reason,
            layer_results=layer_results,
        )
