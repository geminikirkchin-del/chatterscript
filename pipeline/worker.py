# Single serial pipeline worker.
#
# The worker is the ONE place a job's `run_job_sync` may execute. This
# replaces the old fire-and-forget `background_tasks.add_task(...)` path, which
# let N submits run N jobs concurrently on one GPU (heavy contention ->
# "generation returned no audio" on many segments) and orphaned any in-flight
# job when the server died.
#
# Invariant: there is exactly one worker thread per process, so at most one job
# generates TTS at a time, and any RUNNING job found on disk at boot is by
# definition orphaned (the sole worker was not alive), which
# `PipelineService.reconcile_orphans()` resets to PENDING.

import logging
import threading
import time
from typing import Optional

from config import get_pipeline_pause_ms
from pipeline.jobs import PipelineService
from pipeline.models import PipelineJob, PipelineJobStatus

logger = logging.getLogger(__name__)


class PipelineWorker:
    """Run queued pipeline jobs serially on a dedicated daemon thread."""

    def __init__(
        self,
        service: PipelineService,
        poll_interval: float = 2.0,
        claim_grace_sec: float = 0.5,
        pause_ms: Optional[int] = None,
    ):
        self.service = service
        self.poll_interval = poll_interval
        # Avoid claiming a job in the same instant its submit call is still
        # returning (submit persists PENDING before the endpoint responds).
        self.claim_grace_sec = claim_grace_sec
        self.pause_ms = pause_ms if pause_ms is not None else get_pipeline_pause_ms()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="pipeline-worker", daemon=True
        )
        self._thread.start()
        logger.info("Pipeline worker started (serial, poll=%ss)", self.poll_interval)

    def stop(self, timeout: float = 30.0) -> None:
        """Signal shutdown and wait for the current job to finish a segment.

        The stop flag is honoured between segments (see _run_loop), so an
        in-flight segment completes, then the loop exits. A hard kill mid-
        segment leaves a RUNNING job that `reconcile_orphans` reclaims next
        boot.
        """
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Pipeline worker did not stop within %ss", timeout)

    # -- loop ---------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            job = self._claim_next()
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            try:
                logger.info("Worker claimed job %s", job.job_id)
                self.service.run_job_sync(job.job_id, pause_ms=self.pause_ms)
            except Exception as e:
                logger.error(f"Worker failed job {job.job_id}: {e}", exc_info=True)
            # Loop continues; a reclaimed job is PENDING again and re-picked.

    def _claim_next(self) -> Optional[PipelineJob]:
        """Claim the oldest PENDING job, transitioning it to RUNNING atomically.

        Returns the job if claimed, None otherwise. Single-worker + single-
        claim means no lease is required; the store.save transition to RUNNING
        is the claim.
        """
        job = self.service.next_pending_job(older_than=self.claim_grace_sec)
        if job is None:
            return None
        # Re-read and re-check status: another path (e.g. a retry reset racing
        # in) may have moved it. Under the single worker this is defensive.
        latest = self.service.get_job(job.job_id)
        if latest is None or latest.status != PipelineJobStatus.PENDING:
            return None
        latest.status = PipelineJobStatus.RUNNING
        self.service.store.save(latest)
        return latest