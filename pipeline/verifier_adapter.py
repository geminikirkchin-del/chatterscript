# Adapter that exposes the existing audio verifier as a QualityVerifier layer.

from typing import Any, Dict, Optional

from config import config_manager
from pipeline.quality import LayerResult, QualityVerifier, VerificationContext
from pipeline.verifier import (
    DEFAULT_CLIP_THRESHOLD,
    DEFAULT_MAX_DURATION_DEVIATION,
    DEFAULT_MAX_SILENCE_MS,
    DEFAULT_MIN_RMS,
    VerificationThresholds,
    verify_audio,
)

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
        context: Optional[VerificationContext] = None,
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
        # Legacy thresholds live flat under pipeline.verification; defaults come
        # from DEFAULT_CONFIG in config.py (merged into the loaded config). The
        # pipeline.verifier constants are the fallback of last resort, so no
        # default numbers are duplicated here.
        verification_cfg = config_manager.get("pipeline.verification", {})
        return VerificationThresholds(
            max_silence_ms=float(
                verification_cfg.get("max_silence_ms", DEFAULT_MAX_SILENCE_MS)
            ),
            clip_threshold=float(
                verification_cfg.get("clip_threshold", DEFAULT_CLIP_THRESHOLD)
            ),
            min_rms=float(verification_cfg.get("min_rms", DEFAULT_MIN_RMS)),
            max_duration_deviation=float(
                verification_cfg.get(
                    "max_duration_deviation", DEFAULT_MAX_DURATION_DEVIATION
                )
            ),
        )
