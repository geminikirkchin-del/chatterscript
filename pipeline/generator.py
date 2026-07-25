# Segment generator: thin wrapper around the TTS engine seam.

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

SynthesizeFn = Callable[..., Tuple[Optional[np.ndarray], Optional[int]]]


def generate_segment_audio(
    text: str,
    audio_prompt_path: Optional[str],
    gen_params: Dict[str, Any],
    synthesize_fn: SynthesizeFn,
) -> Tuple[Optional[np.ndarray], Optional[int]]:
    """
    Synthesize audio for one pipeline segment.

    Args:
        text: segment text.
        audio_prompt_path: path to the voice file (predefined or reference clone).
        gen_params: generation parameters merged from request + segment overrides.
        synthesize_fn: injectable engine.synthesize callable.

    Returns:
        (audio_array, sample_rate) or (None, None) on failure.
    """
    try:
        audio_tensor_or_array, sr = synthesize_fn(
            text=text,
            audio_prompt_path=audio_prompt_path,
            temperature=gen_params.get("temperature", 0.8),
            exaggeration=gen_params.get("exaggeration", 0.5),
            cfg_weight=gen_params.get("cfg_weight", 0.5),
            seed=gen_params.get("seed", 0),
            language=gen_params.get("language", "en"),
        )
        if audio_tensor_or_array is None or sr is None:
            return None, None
        # Normalize to numpy float32 1-D array regardless of tensor/array input.
        if hasattr(audio_tensor_or_array, "cpu") and hasattr(audio_tensor_or_array, "numpy"):
            audio_np = audio_tensor_or_array.cpu().numpy().squeeze().astype(np.float32)
        else:
            audio_np = np.asarray(audio_tensor_or_array).squeeze().astype(np.float32)
        if audio_np.ndim == 0:
            return None, None
        if audio_np.ndim > 1:
            audio_np = audio_np.reshape(-1)

        speed_factor = gen_params.get("speed_factor", 1.0)
        if speed_factor is not None and speed_factor != 1.0:
            try:
                import utils as _utils

                audio_np = _utils.apply_speed_factor_wsola(audio_np, speed_factor)
                audio_np = audio_np.astype(np.float32)
            except Exception as e:
                logger.warning(f"Failed to apply speed factor {speed_factor}: {e}")

        return audio_np, sr
    except Exception as e:
        logger.error(f"Segment generation failed: {e}", exc_info=True)
        return None, None
