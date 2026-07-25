# Dataclasses and enums for the TTS pipeline.

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from pipeline.quality import QualityVerificationResult


class PipelineJobStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SegmentStatus(enum.Enum):
    PENDING = "pending"
    GENERATING = "generating"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class Segment:
    index: int
    text: str
    status: SegmentStatus = SegmentStatus.PENDING
    audio_path: Optional[str] = None
    score: Optional[float] = None
    audio_score: Optional[float] = None
    asr_score: Optional[float] = None
    failure_reason: Optional[str] = None
    retry_count: int = 0
    gen_params: Dict[str, Any] = field(default_factory=dict)
    verification_log: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "status": self.status.value,
            "audio_path": self.audio_path,
            "score": self.score,
            "audio_score": self.audio_score,
            "asr_score": self.asr_score,
            "failure_reason": self.failure_reason,
            "retry_count": self.retry_count,
            "gen_params": self.gen_params,
            "verification_log": self.verification_log,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Segment":
        return cls(
            index=data["index"],
            text=data["text"],
            status=SegmentStatus(data.get("status", "pending")),
            audio_path=data.get("audio_path"),
            score=data.get("score"),
            audio_score=data.get("audio_score"),
            asr_score=data.get("asr_score"),
            failure_reason=data.get("failure_reason"),
            retry_count=data.get("retry_count", 0),
            gen_params=data.get("gen_params", {}),
            verification_log=data.get("verification_log", []),
        )


@dataclass
class PipelineJob:
    job_id: str
    text: str
    voice_config: Dict[str, Any]
    gen_params: Dict[str, Any]
    status: PipelineJobStatus
    segments: List[Segment] = field(default_factory=list)
    final_audio_path: Optional[str] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    pipeline_config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "text": self.text,
            "voice_config": self.voice_config,
            "gen_params": self.gen_params,
            "status": self.status.value,
            "segments": [seg.to_dict() for seg in self.segments],
            "final_audio_path": self.final_audio_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pipeline_config": self.pipeline_config,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineJob":
        raw_segments = data.get("segments", [])
        segments = [Segment.from_dict(s) if isinstance(s, dict) else s for s in raw_segments]
        return cls(
            job_id=data["job_id"],
            text=data["text"],
            voice_config=data.get("voice_config", {}),
            gen_params=data.get("gen_params", {}),
            status=PipelineJobStatus(data.get("status", "pending")),
            segments=segments,
            final_audio_path=data.get("final_audio_path"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            pipeline_config=data.get("pipeline_config", {}),
        )


@dataclass
class AttemptResult:
    """Result of a single generation + verification attempt for a segment."""

    passed: bool
    quality_result: "QualityVerificationResult"
    audio_metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineJobSummary:
    job_id: str
    status: str
    created_at: Optional[float]
    updated_at: Optional[float]
    segment_count: int
    final_audio_path: Optional[str]
