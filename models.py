# File: models.py
# Pydantic models for API request and response validation.

from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field


class GenerationParams(BaseModel):
    """Common parameters for TTS generation."""

    temperature: Optional[float] = Field(
        None,  # Defaulting to None means server will use config default if not provided
        ge=0.0,
        le=1.5,  # Based on Chatterbox Gradio app for temperature
        description="Controls randomness. Lower is more deterministic. (Range: 0.0-1.5)",
    )
    exaggeration: Optional[float] = Field(
        None,
        ge=0.25,  # Based on Chatterbox Gradio app
        le=2.0,  # Based on Chatterbox Gradio app
        description="Controls expressiveness/exaggeration. (Range: 0.25-2.0)",
    )
    cfg_weight: Optional[float] = Field(
        None,
        ge=0.2,  # Based on Chatterbox Gradio app
        le=1.0,  # Based on Chatterbox Gradio app
        description="Classifier-Free Guidance weight. Influences adherence to prompt/style and pacing. (Range: 0.2-1.0)",
    )
    seed: Optional[int] = Field(
        None,
        ge=0,  # Seed should be non-negative, 0 often implies random.
        description="Seed for generation. 0 may indicate random behavior based on engine.",
    )
    speed_factor: Optional[float] = Field(
        None,
        ge=0.25,
        le=4.0,
        description="Speed factor for the generated audio. 1.0 is normal speed. Applied post-generation.",
    )
    language: Optional[str] = Field(
        None,
        description="Language of the text. (Primarily for UI, actual engine may infer)",
    )


class CustomTTSRequest(BaseModel):
    """Request model for the custom /tts endpoint."""

    text: str = Field(..., min_length=1, description="Text to be synthesized.")

    voice_mode: Literal["predefined", "clone"] = Field(
        "predefined",  # Default voice mode
        description="Voice mode: 'predefined' for a built-in voice, 'clone' for voice cloning using a reference audio.",
    )
    predefined_voice_id: Optional[str] = Field(
        None,
        description="Filename of the predefined voice to use (e.g., 'default_sample.wav'). Required if voice_mode is 'predefined'.",
    )
    reference_audio_filename: Optional[str] = Field(
        None,
        description="Filename of a user-uploaded reference audio for voice cloning. Required if voice_mode is 'clone'.",
    )

    output_format: Optional[Literal["wav", "opus", "mp3"]] = Field(  # Added "mp3"
        "wav", description="Desired audio output format."  # Default output format
    )

    split_text: Optional[bool] = Field(
        True,  # Default to splitting enabled
        description="Whether to automatically split long text into chunks for processing.",
    )
    chunk_size: Optional[int] = Field(
        120,  # Default target chunk size from config
        ge=50,  # Minimum reasonable chunk size
        le=500,  # Maximum reasonable chunk size
        description="Approximate target character length for text chunks when splitting is enabled (50-500).",
    )

    # Embed generation parameters directly
    temperature: Optional[float] = Field(
        None, description="Overrides default temperature if provided."
    )
    exaggeration: Optional[float] = Field(
        None, description="Overrides default exaggeration if provided."
    )
    cfg_weight: Optional[float] = Field(
        None, description="Overrides default CFG weight if provided."
    )
    seed: Optional[int] = Field(None, description="Overrides default seed if provided.")
    speed_factor: Optional[float] = Field(
        None, description="Overrides default speed factor if provided."
    )
    language: Optional[str] = Field(
        None, description="Overrides default language if provided."
    )

    stream: bool = Field(
        False,
        description="If true, returns a StreamingResponse with WAV audio yielded as each chunk is synthesized. output_format is ignored when streaming.",
    )


class ErrorResponse(BaseModel):
    """Standard error response model for API errors."""

    detail: str = Field(..., description="A human-readable explanation of the error.")


class UpdateStatusResponse(BaseModel):
    """Response model for status updates, e.g., after saving settings."""

    message: str = Field(
        ..., description="A message describing the result of the operation."
    )
    restart_needed: Optional[bool] = Field(
        False,
        description="Indicates if a server restart is recommended or required for changes to take full effect.",
    )


class PipelineSubmitRequest(BaseModel):
    """Request model for submitting a long-form TTS pipeline job."""

    text: Optional[str] = Field(
        None,
        min_length=1,
        description="Long text to synthesize. Required unless script_filename is provided.",
    )
    script_filename: Optional[str] = Field(
        None,
        description="Filename of a .md script under the input/ folder. If provided, its contents are used as the text.",
    )
    job_name: Optional[str] = Field(
        None,
        description="Optional human-readable name for the job and final audio file.",
    )
    voice_mode: Literal["predefined", "clone"] = Field(
        "predefined",
        description="Voice mode: 'predefined' or 'clone'.",
    )
    predefined_voice_id: Optional[str] = Field(
        None,
        description="Filename of the predefined voice (required for predefined mode).",
    )
    reference_audio_filename: Optional[str] = Field(
        None,
        description="Filename of the reference audio (required for clone mode).",
    )
    temperature: Optional[float] = Field(None, description="Overrides default temperature.")
    exaggeration: Optional[float] = Field(None, description="Overrides default exaggeration.")
    cfg_weight: Optional[float] = Field(None, description="Overrides default CFG weight.")
    seed: Optional[int] = Field(None, description="Overrides default seed.")
    speed_factor: Optional[float] = Field(None, description="Overrides default speed factor.")
    language: Optional[str] = Field(None, description="Overrides default language.")
    output_format: Optional[Literal["wav", "mp3", "opus"]] = Field(
        None,
        description="Output format for the final composed audio (defaults to config).",
    )
    max_segment_duration_sec: Optional[float] = Field(
        None,
        ge=5.0,
        le=120.0,
        description="Optional override for the per-segment audio duration target.",
    )
    pause_ms: Optional[int] = Field(
        None,
        ge=0,
        le=1000,
        description="Optional override for the inter-segment pause in milliseconds.",
    )


class PipelineJobResponse(BaseModel):
    """Response model for a pipeline job status query."""

    job_id: str
    status: str
    text: str
    segments: List[Dict[str, Any]]
    final_audio_path: Optional[str]
    srt_path: Optional[str] = None
    created_at: Optional[float]
    updated_at: Optional[float]
    job_name: Optional[str] = None
    total_generation_time_sec: Optional[float] = None
    total_verification_time_sec: Optional[float] = None


class PipelineJobSummaryResponse(BaseModel):
    """Response model for a pipeline job list entry."""

    job_id: str
    status: str
    segment_count: int
    final_audio_path: Optional[str]
    created_at: Optional[float]
    updated_at: Optional[float]
    job_name: Optional[str] = None


class PipelineJobListResponse(BaseModel):
    """Response model for listing pipeline jobs."""

    jobs: List[PipelineJobSummaryResponse]


class PipelineSubmitResponse(BaseModel):
    """Response model after submitting a pipeline job."""

    job_id: str
    status: str


class PipelineScriptListResponse(BaseModel):
    """Response model listing available .md scripts in the input folder."""

    scripts: List[str]


class PipelineFeedbackRequest(BaseModel):
    """Request model for submitting feedback on a pipeline segment."""

    segment_index: int = Field(..., ge=0, description="Index of the segment being rated.")
    rating: str = Field(
        ...,
        description="User rating: 'approve', 'reject', or a numeric score string.",
    )
    comment: Optional[str] = Field(None, description="Optional free-text comment.")


class PipelineFeedbackResponse(BaseModel):
    """Response model after submitting feedback."""

    message: str
