# ADR-0003: Single serial worker + boot reconcile + idempotent resume

**Status:** Accepted (2026-09-26)

## Context

ADR-0002 documented that pipeline jobs ran on **fire-and-forget**
`background_tasks.add_task(...)` threads with no central queue and no resume on
boot. Its two concrete costs materialised:

1. **GPU contention** — multiple jobs (and the WebUI's per-tones, per-clone
   TTS) could synthesize concurrently on one GPU, causing the 
   `whisperx_unavailable` / `whisperx_low_confidence` churn and long-tail
   failures measured across long sessions.
2. **Orphaned RUNNING jobs** — a mid-job restart left `job.json` as `RUNNING`
   with partial segments, never resumed or failed (19 orphans at the time of
   ADR-0002).

ADR-0002 deliberately did *not* auto-resume, because there was no idempotency
contract: we could not tell which segments were truly complete vs merely
recorded, so blind resume risked silent gaps or duplicates.

## Decision

Replace fire-and-forget background threads with **one serial worker** and make
the job/segment lifecycle **idempotent** so a resume is safe:

1. **Single serial worker.** A module-level `PipelineWorker` daemon thread
   claims the **oldest PENDING job**, marks it `RUNNING`, and runs it to
   completion before claiming the next. Exactly one job is in-flight at any
   time — GPU contention between pipeline jobs is structurally impossible.
2. **Split at submit time.** `submit_job` performs the text→segment split and
   persists the full segment list up front (`job.json` seed), so the worker
   executes against a durable, known segment list rather than re-deriving it
   inside `run_job_sync`.
3. **Idempotent segment execution.** `run_job_sync` iterates the persisted
   segments, **skips segments already `PASSED`**, resets interrupted-state
   segments (`GENERATING`/pending with partial audio) back to `PENDING`, and
   regenerates only the not-yet-passed ones. Re-running a job re-does only the
   unfinished work.
4. **Boot reconciliation.** On startup, `reconcile_orphans()` scans the store
   and resets every `RUNNING` job to `PENDING` (preserving already-`PASSED`
   segments), so the worker picks it up and finishes it on the next boot.
   A job only reaches `DONE`/`FAILED` through the worker within a single server
   lifetime.
5. **Retry-failed is idempotent.** `POST .../retry-failed` resets only `FAILED`
   segments to `PENDING` and re-enqueues the job for the worker; `PASSED`
   segments keep their verified audio (per ADR-0001's compose-everything rule).

### Mechanism notes

- Worker liveness is bounded by `poll_interval_sec` (default `2.0`), enabled in
  `config.py` under `pipeline.worker.{enabled,poll_interval_sec}`.
- The claim transition (`PENDING → RUNNING`) is the serialisation point; the
  worker never claims a job another worker already holds because only one
  worker exists and `RUNNING` jobs are not claimable.
- The worker daemon runs on the same process, so a full process kill still
  interrupts a job — but recovery is now automatic on the next boot via
  reconcile, instead of being a permanent orphan.

## Consequences

- **No more orphaned RUNNING jobs** on restart: the next boot reconciles them
  to `PENDING` and the worker resumes the unfinished segments. Idempotency +
  reconcile make this safe.
- **No more concurrent pipeline generation**: serial worker bounds peak GPU
  load to one job. Per-segment verification churn from GPU contention should
  drop correspondingly.
- **Resume preserves progress**: a long job restarted mid-run re-does only the
  segments that had not yet passed verification.
- `retry-failed` and the WebUI now enqueue for the worker instead of spawning a
  thread; the submit endpoint returns `pending` immediately.
- **Trade-off**: serial processing serialises jobs — a second long job waits for
  the first. For the current single-operator / single-GPU use case this is the
  correct price to pay for determinism and low failure rate; if a future
  workload needs true parallelism, the worker can be widened to N>1 with the
  existing claim/`RUNNING` mechanism as the boundary.
- **Remaining hazard**: a process-level crash still loses GPU work for the
  segment in-flight (sub-`poll-internal` granularity); a mid-segment restart
  re-runs that one segment. This is acceptable and bounded.