# ASR verification for TTS pipeline segments using whisperx.

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Optional whisperx import — pipeline remains functional without it.
try:
    import whisperx

    WHISPERX_AVAILABLE = True
except ImportError:
    whisperx = None
    WHISPERX_AVAILABLE = False

DEFAULT_WHISPER_MODEL = "small"
DEFAULT_SIMILARITY_THRESHOLD = 0.75


@dataclass
class ASRVerificationResult:
    passed: bool
    similarity: float
    transcription: str
    failure_reason: Optional[str]
    metrics: Dict[str, Any]


def normalize_text(text: str, language: str = "en") -> str:
    """
    Normalize text for comparison.

    - Lowercase
    - Remove punctuation
    - Collapse whitespace
    - Strip CJK/full-width punctuation for Chinese
    """
    text = text.lower().strip()
    # Remove common punctuation (Latin and CJK full-width). Keep spaces for now.
    punctuation_pattern = r'[\u3001-\u303f\uff00-\uffef"\'“”‘’.,!?;:@#$%^&*()\[\]{}|\\<>]'
    text = re.sub(punctuation_pattern, "", text)
    if language.lower().startswith("zh"):
        # Strip CJK punctuation and CJK spaces (full-width space).
        cjk_punctuation_pattern = r'[。！？，、；：“”‘’（）【】《》\u3000]'
        text = re.sub(cjk_punctuation_pattern, "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compute_similarity(original: str, transcription: str) -> float:
    """Compute normalized similarity between two strings using difflib."""
    if not original and not transcription:
        return 1.0
    if not original or not transcription:
        return 0.0
    return difflib.SequenceMatcher(None, original, transcription).ratio()


class ASRVerifier:
    """
    Local whisperx-based ASR verifier.

    The model is loaded lazily on first transcription and cached for reuse.
    If whisperx is not installed, transcription is skipped and verification
    is treated as disabled.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_WHISPER_MODEL,
        device: str = "auto",
        compute_type: str = "float16",
    ):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._align_model = None
        self._metadata = None

    def _resolve_device(self) -> str:
        if self.device != "auto":
            return self.device
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def _load_model(self):
        if not WHISPERX_AVAILABLE:
            logger.warning(
                "whisperx is not installed. ASR verification will be skipped. "
                "Install it with: pip install whisperx"
            )
            return None
        if self._model is not None:
            return self._model
        try:
            device = self._resolve_device()
            logger.info(f"Loading whisperx model '{self.model_name}' on {device}...")
            self._model = whisperx.load_model(
                self.model_name,
                device,
                compute_type=self.compute_type,
            )
            logger.info("whisperx model loaded.")
            return self._model
        except Exception as e:
            logger.error(f"Failed to load whisperx model: {e}", exc_info=True)
            return None

    def transcribe(self, audio_path: str, language: str = "en") -> Optional[str]:
        """Transcribe an audio file and return the raw text."""
        model = self._load_model()
        if model is None:
            return None
        try:
            audio = whisperx.load_audio(audio_path)
            result = model.transcribe(audio, language=language)
            segments = result.get("segments", [])
            text = " ".join(seg.get("text", "").strip() for seg in segments)
            return text.strip()
        except Exception as e:
            logger.error(f"whisperx transcription failed for {audio_path}: {e}", exc_info=True)
            return None

    def verify(
        self,
        audio_path: str,
        original_text: str,
        language: str = "en",
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> ASRVerificationResult:
        """
        Transcribe the segment and compare it to the original text.

        If whisperx is unavailable, returns passed=True with similarity 1.0
        so the pipeline can continue without ASR verification.
        """
        if not WHISPERX_AVAILABLE:
            return ASRVerificationResult(
                passed=True,
                similarity=1.0,
                transcription="",
                failure_reason=None,
                metrics={"whisperx_available": False},
            )

        transcription = self.transcribe(audio_path, language=language)
        metrics = {
            "whisperx_available": True,
            "transcription": transcription,
            "original_text": original_text,
        }

        if transcription is None:
            return ASRVerificationResult(
                passed=False,
                similarity=0.0,
                transcription="",
                failure_reason="asr_transcription_failed",
                metrics=metrics,
            )

        norm_original = normalize_text(original_text, language)
        norm_transcription = normalize_text(transcription, language)
        similarity = compute_similarity(norm_original, norm_transcription)
        metrics["normalized_original"] = norm_original
        metrics["normalized_transcription"] = norm_transcription
        metrics["similarity"] = similarity

        passed = similarity >= similarity_threshold
        return ASRVerificationResult(
            passed=passed,
            similarity=similarity,
            transcription=transcription,
            failure_reason=None if passed else "asr_mismatch",
            metrics=metrics,
        )
