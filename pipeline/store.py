# Job persistence: atomic JSON read/write for pipeline jobs.

import json
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

from pipeline.models import PipelineJob, PipelineJobSummary

logger = logging.getLogger(__name__)


class JobStore:
    """Persists pipeline jobs to disk as atomic JSON writes."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _job_dir_for(self, job: PipelineJob) -> Path:
        return self.base_dir / (job.folder_name or job.job_id)

    def _job_dir(self, job_id: str) -> Path:
        return self.base_dir / job_id

    def _job_file(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _find_job_dir(self, job_id: str) -> Optional[Path]:
        """Locate the job directory by id or by the folder_name prefix."""
        direct = self._job_dir(job_id)
        if direct.is_dir() and (direct / "job.json").is_file():
            return direct
        if not self.base_dir.exists():
            return None
        for item in self.base_dir.iterdir():
            if item.is_dir() and item.name.startswith(f"{job_id}_"):
                if (item / "job.json").is_file():
                    return item
        return None

    def save(self, job: PipelineJob) -> bool:
        """Atomically write the job JSON."""
        job_dir = self._job_dir_for(job)
        job_dir.mkdir(parents=True, exist_ok=True)
        job_file = job_dir / "job.json"
        temp_file = job_file.with_suffix(".tmp")
        try:
            job.updated_at = time.time()
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(job.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(temp_file, job_file)
            return True
        except Exception as e:
            logger.error(f"Failed to save job {job.job_id}: {e}", exc_info=True)
            return False

    def load(self, job_id: str) -> Optional[PipelineJob]:
        job_dir = self._find_job_dir(job_id)
        if job_dir is None:
            return None
        job_file = job_dir / "job.json"
        try:
            with open(job_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return PipelineJob.from_dict(data)
        except Exception as e:
            logger.error(f"Failed to load job {job_id}: {e}", exc_info=True)
            return None

    def list_jobs(self) -> List[PipelineJobSummary]:
        summaries: List[PipelineJobSummary] = []
        if not self.base_dir.exists():
            return summaries
        for item in sorted(self.base_dir.iterdir()):
            if not item.is_dir():
                continue
            job_file = item / "job.json"
            if not job_file.exists():
                continue
            try:
                with open(job_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                job = PipelineJob.from_dict(data)
            except Exception as e:
                logger.error(f"Failed to list job from {job_file}: {e}", exc_info=True)
                continue
            summaries.append(
                PipelineJobSummary(
                    job_id=job.job_id,
                    status=job.status.value,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                    segment_count=len(job.segments),
                    final_audio_path=job.final_audio_path,
                    job_name=job.job_name,
                )
            )
        return sorted(summaries, key=lambda s: s.job_id)
