# Adapter that exposes the existing audio verifier as a QualityVerifier layer.

from typing import Any, Dict, Optional

from config import config_manager
from pipeline.quality import LayerResult, QualityVerifier
from pipeline.verifier import VerificationThresholds, verify_audio

import soundfile as sf


class BasicAudioVerifierAdapter(QualityVerifier):
    """Wraps the legacy numpy-based audio verification as a quality layer."""

    @property
    def name(self) -> str:
        return "basic_audio"

    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
        context: Optional[Dict[str, Any]] = None,
    ) -> LayerResult:
        audio, sr = sf.read(audio_path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        thresholds = self._load_thresholds()
        result = verify_audio(
            audio=audio,
            sr=sr,
            expected_duration=expected_duration,
            thresholds=thresholds,
        )

        return LayerResult(
            passed=result.passed,
            score=result.score,
            failure_reason=result.failure_reason,
            metrics=result.metrics,
        )

    def _load_thresholds(self) -> VerificationThresholds:
        verification_cfg = config_manager.get("pipeline.verification", {})
        thresholds_cfg = verification_cfg.get("thresholds", {})
        return VerificationThresholds(
            max_silence_ms=float(thresholds_cfg.get("max_silence_ms", 500.0)),
            clip_threshold=float(thresholds_cfg.get("clip_threshold", 0.99)),
            min_rms=float(thresholds_cfg.get("min_rms", 0.01)),
            max_duration_deviation=float(
                thresholds_cfg.get("max_duration_deviation", 0.50)
            ),
        )
