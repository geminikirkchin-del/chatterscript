# ADR-0001: Final composition always includes every segment, failed ones best-effort

**Status:** Accepted (2026-07-26)

## Context

The long-form pipeline verifies every segment and originally composed the final
audio from **passed segments only**. On the first full run (116 zh segments),
25+ segments failed verification — and the final silently dropped them, leaving
narrative gaps mid-sentence-flow.

For educational narration (the Slidev-Kw use case), every sentence carries
meaning. A missing sentence breaks the logic of the explanation — a flawed take
is far less damaging than a gap. Note the failure modes here are mostly
*borderline* (WER slightly over threshold, one long pause): the best failed
take is usually perfectly listenable.

## Decision

The final composition concatenates **all** segments in sentence order:

- Passed segments ship their verified audio.
- Failed segments ship their **best-effort take** — the last polished attempt
  left on disk (`segments/{index}.wav`), which has already been through the
  audio-polish step (denoise + pause normalization).
- Only a segment with **no audio at all** (generation or write failure) may be
  skipped.

Verification status is **not** hidden: failed segments keep their FAILED status,
failure reasons, and logs in the API/dashboard, and can be regenerated later
with `POST /api/tts-pipeline/{job_id}/retry-failed`.

## Consequences

- The final is always complete and in order; listeners never hit a logic gap.
- The shipped audio may contain unverified takes — this is deliberate, and the
  failure information stays visible so humans can review and repair.
- `retry_failed_segments` recomposes with the same rule, so a retry round that
  improves some segments still ships the rest best-effort.
- Verification's role shifts from "gate what ships" to "grade what ships" —
  it drives retries, repair, and feedback rather than silently dropping content.
