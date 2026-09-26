# ADR-0002: Jobs orphaned on restart — no resume/watchdog on boot

**Status:** Superseded by [ADR-0003](0003-resume-worker-idempotency.md) (2026-09-26)

**_Note (2026-09-26):** ADR-0003 implements the automatic resume this ADR
declined — a single serial worker, boot-time `reconcile_orphans()`, and
per-segment idempotent execution. The decision below is preserved for history
and for the failure-mode analysis it records.

**Status (original):** Accepted (2026-09-25)

## Context

A pipeline job is executed by a **fire-and-forget background task**: the
`submit` endpoint calls `background_tasks.add_task(_run_pipeline_job, ...)`,
which runs `pipeline_service.run_job_sync()` synchronously on one event-loop
background thread (`server.py:1612`, `pipeline/jobs.py:136`). There is no
central queue, worker pool, or persistent scheduling boundary — the job's
execution lifetime is exactly the lifetime of that single in-memory thread.

When the server process dies or restarts while a job is mid-generation (a
crash, a manual `uvicorn` restart, a redeploy), the thread vanishes, but the
job's persisted state in `outputs/pipeline_jobs/<job_id>/job.json` is left as
`RUNNING` with partial `segments`. Nothing on subsequent boots scans for these
stuck `RUNNING`/`PENDING` jobs and resumes or fails them — `run_job_sync` only
advances a job when it is already `PENDING`/`RUNNING`, and it is only ever
invoked by the original HTTP background task, which no longer exists.

Observed consequence: the job list accumulates **orphaned "running" jobs** that
will never finish (19 at the time of writing, the oldest from 2026-08-16).
They do not consume GPU (their worker threads are gone), but they pollute the
API/dashboard state with permanently-in-flight entries, and a long job loses
all progress on a mid-run restart. Symptomatically this can look like "engine
degradation" on long sessions when in reality the outage was a restart.

## Decision

For now, **do not implement automatic resume on boot**. Document the hazard and
keep the recovery path manual and deterministic:

1. On server start, the job store is loaded but **no** job is auto-resumed or
   auto-failed; orphaned `RUNNING` jobs stay as-is so no partial work is ever
   rendered into a misleading `DONE`/`FAILED` state behind the caller's back.
2. Recovery is explicit and operator-driven:
   - inspect `GET /api/tts-pipeline` for stale `running` jobs, and
   - `POST /api/tts-pipeline/{job_id}/retry-failed` on a known-good job id, or
     archive the job folder.
3. A future change may add a boot-time reconciliation job (mark RUNNING jobs
   older than a threshold as FAILED/orphaned, with an explicit manual resume
   button). That is a separate, larger decision.

Rationale for deferring automatic resume: resuming a half-finished job without
an idempotency contract (which segments are truly complete vs merely recorded)
risks shipping a `final` audio with silent gaps or duplicated segments. The
correct fix needs a resume-queue / watchdog and per-segment idempotency, which
is out of scope for an immediate patch.

## Consequences

- Operators can rely on the invariant: a job only reaches `DONE`/`FAILED`
  through its own background task within a single server lifetime; a restart
  never fabricates completion.
- The dashboard may show stale `running` jobs after a restart — the operator
  script/documentation must account for this (check job age/created_at).
- No automatic recovery means a long job interrupted by a restart requires a
  re-submit; `retry-failed` from a clean slate remains the cheapest path.
- This ADR captures the exact failure mode so the eventual resume feature can
  be designed against a recorded contract rather than reverse-engineered.