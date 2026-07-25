# Feedback persistence for the TTS pipeline.

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SegmentFeedback:
    job_id: str
    segment_index: int
    rating: str  # "approve"/"reject" for user feedback; "metrics" kept for backward compat
    comment: Optional[str] = None
    timestamp: Optional[float] = None
    kind: str = "feedback"  # "feedback" (user ratings) or "metrics"
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "segment_index": self.segment_index,
            "rating": self.rating,
            "comment": self.comment,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SegmentFeedback":
        rating = data["rating"]
        # Migrate legacy entries: rating == "metrics" with no kind means a
        # metrics entry; legacy extra key "params" is renamed to "gen_params".
        kind = data.get("kind")
        if not kind:
            kind = "metrics" if rating == "metrics" else "feedback"
        extra = dict(data.get("extra", {}))
        if "params" in extra and "gen_params" not in extra:
            extra["gen_params"] = extra.pop("params")
        return cls(
            job_id=data["job_id"],
            segment_index=data["segment_index"],
            rating=rating,
            comment=data.get("comment"),
            timestamp=data.get("timestamp"),
            kind=kind,
            extra=extra,
        )


class FeedbackStore:
    """Append-only JSONL store for segment feedback."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, feedback: SegmentFeedback) -> bool:
        """Append a feedback entry to the JSONL file."""
        try:
            feedback.timestamp = time.time()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(feedback.to_dict(), ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            logger.error(f"Failed to append feedback: {e}", exc_info=True)
            return False

    def read_recent(self, limit: int = 100) -> List[SegmentFeedback]:
        """Read the most recent feedback entries."""
        entries: List[SegmentFeedback] = []
        if not self.path.exists():
            return entries
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entries.append(SegmentFeedback.from_dict(data))
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Failed to read feedback: {e}", exc_info=True)
        return entries[-limit:]

    def get_ratings_for_segment(
        self, job_id: str, segment_index: int
    ) -> List[SegmentFeedback]:
        """Return all feedback entries for a specific segment."""
        return [
            fb
            for fb in self.read_recent(limit=10000)
            if fb.job_id == job_id and fb.segment_index == segment_index
        ]

    def append_metrics(
        self,
        job_id: str,
        segment_index: int,
        layer_results: Dict[str, Any],
        gen_params: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Persist quality verification metrics for a segment.

        These entries have kind="metrics" (rating="metrics" is kept for backward
        compatibility) and store the per-layer results so the agent can learn
        which parameter combinations produce good speaker / spectral similarity.
        """
        feedback = SegmentFeedback(
            job_id=job_id,
            segment_index=segment_index,
            rating="metrics",
            comment=None,
            kind="metrics",
            extra={
                "layer_results": layer_results,
                "gen_params": gen_params or {},
            },
        )
        return self.append(feedback)

    def append_user_feedback(
        self,
        job_id: str,
        segment_index: int,
        rating: str,
        comment: Optional[str],
        gen_params: Optional[Dict[str, Any]],
        segment_text: Optional[str],
        score: Optional[float],
        failure_reason: Optional[str],
    ) -> bool:
        """
        Persist a user rating (approve/reject) for a segment.

        The store owns the schema: callers pass plain fields and the entry is
        built here with kind="feedback" and a normalized extra dict.
        """
        feedback = SegmentFeedback(
            job_id=job_id,
            segment_index=segment_index,
            rating=rating,
            comment=comment,
            kind="feedback",
            extra={
                "gen_params": gen_params or {},
                "segment_text": segment_text,
                "score": score,
                "failure_reason": failure_reason,
            },
        )
        return self.append(feedback)

    def read_recent_metrics(self, limit: int = 200) -> List[SegmentFeedback]:
        """Return the most recent metrics-only feedback entries."""
        return [
            fb
            for fb in self.read_recent(limit=limit)
            if fb.kind == "metrics" or fb.rating == "metrics"
        ]
