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
    rating: str  # "approve", "reject", or numeric score string
    comment: Optional[str] = None
    timestamp: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "segment_index": self.segment_index,
            "rating": self.rating,
            "comment": self.comment,
            "timestamp": self.timestamp,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SegmentFeedback":
        return cls(
            job_id=data["job_id"],
            segment_index=data["segment_index"],
            rating=data["rating"],
            comment=data.get("comment"),
            timestamp=data.get("timestamp"),
            extra=data.get("extra", {}),
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
