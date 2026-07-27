---
id: tts-pipeline
status: ready-for-agent
labels: ready-for-agent
---

# Spec: Long-Form TTS Pipeline with Sentence-Level Verification

## Problem Statement

Generating long-form narration (>1 minute) from a single `/tts` call produces unstable output across all supported languages, models, and voice modes including voice cloning and predefined voices: silent gaps, mispronunciations, and inconsistent pacing appear as duration grows. Short clips (15–30s) work well, but quality degrades dramatically once a passage exceeds roughly one minute. Users need a reliable pipeline that generates long scripts sentence-by-sentence, verifies each segment, retries failed segments intelligently, and composes the final audio with natural pauses.

## Solution

Introduce an asynchronous TTS pipeline (`/api/tts-pipeline`) that:

1. Splits a long script into short segments targeting ~50 seconds of audio each.
2. Generates audio for each segment independently using the existing TTS engine.
3. Verifies every segment with two layers:
   - Audio quality checks (silence, clipping, RMS, duration) via ffmpeg/librosa.
   - ASR back-verification via whisperx, comparing transcribed text against the input segment.
4. Retries failed segments up to 3 times, then invokes a local AI agent to adjust generation parameters based on the failure type.
5. Scores each segment and exposes per-segment status in the API and UI.
6. Composes **all** segments into one final WAV/MP3 in sentence order with 100–200ms natural pauses between segments using ffmpeg — including failed segments via their best-effort take (see ADR-0001).
7. Keeps intermediate segment files for human review and accepts feedback to improve future AI agent decisions.

The pipeline must work entirely offline using local models and tools.

## User Stories

1. As a content creator, I want to submit a long script via a dedicated API and receive a job ID, so that I can fetch results later without blocking the client.
2. As a content creator, I want the pipeline to split my script into ~50-second audio segments, so that each piece stays within the quality window of the TTS model.
3. As a content creator, I want short sentences to be merged into one segment when possible, so that the final audio does not sound choppy.
4. As a content creator, I want each segment to be checked for long silences, clipping, low volume, and unexpected duration, so that obvious audio defects are caught automatically.
5. As a content creator, I want each segment to be transcribed back to text and compared with the original, so that I can be confident the pronunciation and content are correct.
6. As a content creator, I want failed segments to be retried automatically up to 3 times, so that transient failures do not abort the whole job.
7. As a content creator, I want an AI agent to adjust parameters (temperature, exaggeration, cfg_weight, seed, voice) after repeated failures, so that the pipeline can recover without manual intervention.
8. As a content creator, I want to see a quality score for every segment, so that I can prioritize which segments to review.
9. As a content creator, I want to listen to individual segment files before final composition, so that I can replace or re-generate bad ones.
10. As a content creator, I want the final output to concatenate segments with natural 100–200ms pauses, so that the long narration flows like a single recording.
11. As a content creator, I want the final output to follow the configured audio format and sample rate, so that it integrates with the rest of the project.
12. As a content creator, I want intermediate segment files to be preserved, so that I can inspect or reuse them outside the pipeline.
13. As a content creator, I want to submit feedback (approve/reject/score) on segments, so that the AI agent learns my quality preferences over time.
14. As a UI user, I want a pipeline submission page where I can paste a long script and choose voice/params, so that I do not need to use curl.
15. As a UI user, I want a progress dashboard showing segment-level status, scores, and retry counts, so that I can monitor long jobs.
16. As a UI user, I want to play and download the final composed audio from the dashboard, so that I can use it in Slidev-Kw.
17. As a UI user, I want to see per-segment failure reasons, so that I understand why a segment was rejected.
18. As a project maintainer, I want the pipeline to be offline-only, so that it does not depend on external APIs.
19. As a project maintainer, I want the pipeline to reuse the existing `engine.synthesize()` seam, so that model loading and device handling stay centralized.
20. As a project maintainer, I want the pipeline job state to be persisted to disk, so that server restarts do not lose in-progress jobs.

## Implementation Decisions

### Module boundaries

- **`pipeline/`** — new package containing orchestration, segmentation, verification, composition, and AI agent logic. It sits behind a small interface consumed by the API layer.
- **`server.py`** — adds new `/api/tts-pipeline/*` routes and delegates all business logic to the pipeline module. Keeps route handlers thin.
- **`models.py`** — adds Pydantic request/response models for pipeline jobs and segments.
- **`ui/`** — adds a new page/section for pipeline submission and monitoring, reusing existing fetch helpers.
- **`config.py`** — adds optional pipeline defaults (max segment audio duration, retry count, pause length, verification thresholds).

### Pipeline interface

The primary seam is a single function:

```python
def submit_pipeline_job(text: str, voice_config: dict, gen_params: dict) -> str: ...
def get_pipeline_job(job_id: str) -> PipelineJob: ...
def list_pipeline_jobs() -> list[PipelineJobSummary]: ...
```

All other operations (segment, generate, verify, retry, compose) are internal to the pipeline module.

### Segmentation

- Reuse the existing sentence splitter (`utils.split_into_sentences`) to produce sentence boundaries.
- Greedily group consecutive short sentences until the estimated audio duration reaches the configured max (default ~50s).
- Estimated duration is computed from character/word counts using language-specific rates derived from Edu-content-prep length targets:
  - zh-cn: ~5.5 characters / second
  - en-us / en-gb: ~3 words / second
- Long single sentences that exceed the target form their own segment rather than being truncated.

### Segment generation

- Each segment calls `engine.synthesize()` with the same voice and params as the original request.
- Generation runs in the async pipeline worker so the API remains responsive.
- Output is saved as an individual WAV file under `outputs/pipeline_jobs/{job_id}/segments/{seg_index}.wav`.

### Verification

Two layers run in sequence:

1. **Audio verification (ffmpeg / librosa)**
   - Detect long internal silence (>300ms).
   - Detect clipping (samples above 0.99).
   - Reject if RMS is below a floor (near-silent segment).
   - Reject if actual duration deviates more than 30% from the estimate.

2. **ASR verification (whisperx)**
   - Transcribe the segment audio locally.
   - Normalize both original text and transcription.
   - Compute similarity (e.g., difflib SequenceMatcher or rapidfuzz).
   - Reject if similarity falls below threshold (default 0.75).

Failure metadata (type, metrics, retry count) is recorded on the segment.

### Retry and AI agent

- A segment failing verification is retried up to 3 times with the original parameters.
- After 3 failures, a local rule-based AI agent inspects the failure metadata and mutates generation parameters:
  - ASR mismatch → lower temperature, raise cfg_weight, change seed.
  - Silence / low RMS → lower exaggeration, change seed.
  - Clipping → lower exaggeration.
  - Repeated ASR + audio failures → suggest switching reference voice if alternatives exist.
- The agent records the parameter deltas and the outcome for the feedback loop.
- If the agent-adjusted run also fails, the segment is marked `failed` and the pipeline continues. The final composition **still includes the failed segment in its original position**, using its last polished attempt on disk (best-effort take) — every sentence carries meaning and a missing one breaks the narration's logic (see ADR-0001). The segment's failed status and verification logs stay visible for review and retry.

### Composition

- Concatenate segments **in sentence order**; the final must contain every segment — verified takes for passed segments, best-effort takes for failed ones. Only a segment with no audio at all (generation or write failure) may be skipped.
- Insert 100–200ms of silence between segments (configurable, default 150ms).
- Output follows `config.audio_output.format` and `config.audio_output.sample_rate`.
- Final file saved as `outputs/pipeline_jobs/{job_id}/final.{format}`.

### Job persistence

- Jobs are stored as JSON files under `outputs/pipeline_jobs/{job_id}/job.json`.
- The worker writes the file atomically after every state change.
- On server startup, unfinished jobs can be resumed or marked `failed` depending on policy.

### API contract

```
POST   /api/tts-pipeline
GET    /api/tts-pipeline
GET    /api/tts-pipeline/{job_id}
GET    /api/tts-pipeline/{job_id}/segments/{seg_id}/audio
GET    /api/tts-pipeline/{job_id}/final
POST   /api/tts-pipeline/{job_id}/feedback
```

`POST /api/tts-pipeline` accepts the long text, voice mode, voice ID / reference filename, generation params, and optional pipeline overrides; returns `{ job_id }`.

`GET /api/tts-pipeline/{job_id}` returns job status, segment list with scores/status/failure reasons, and final output path when ready.

### UI integration

- Add a new "Pipeline" tab or page in the existing UI.
- Reuse the config form for voice/params and add a large text area for the script.
- Poll `/api/tts-pipeline/{job_id}` every few seconds to show progress.
- Render a table of segments with status badges, scores, play buttons, and failure reasons.
- Show the final audio player and download link when status is `done`.
- Provide approve/reject/score buttons per segment for the feedback loop.

### AI feedback loop

- Store user feedback and per-segment outcomes in `outputs/pipeline_jobs/feedback.jsonl`.
- The AI agent reads recent feedback when choosing parameter adjustments.
- Initially the agent is rule-based; the feedback file enables future statistical preference learning.

## Testing Decisions

- Test at the pipeline seam, not inside whisperx or the TTS model.
- Mock `engine.synthesize()` and the whisperx verifier in unit tests to control success/failure/retry paths.
- Add integration tests that exercise the full state machine end-to-end with synthetic audio files.
- Segmenter tests cover Chinese and English grouping behavior and respect the ~50s target.
- Audio verifier tests use synthetic audio: pure silence, clipping, normal speech-shaped noise, and files with inserted gaps.
- ASR verifier tests mock the whisperx result and compare text similarity.
- Composer tests verify that concatenated output duration equals sum of segment durations plus inter-segment pauses.
- Prior art: `test_srt_feature.py` already exercises audio utilities and timeline assembly; pipeline tests can follow its pattern of synthetic numpy arrays and assertions.

## Out of Scope

- Distributed worker queues or Celery/RabbitMQ (single-process async is sufficient).
- Real-time streaming pipeline output.
- Cloud storage or remote model serving.
- Automatic voice language matching (the user must still pick a suitable reference voice).
- Model fine-tuning or custom voice training.
- Automatic slide timing / SRT generation for the composed audio (can be added later).

## Further Notes

- The pipeline is designed for the Edu-content-prep output format: pure narration scripts with no markup.
- Target production time is 15–45 minutes for a 15-minute narration, running locally on CUDA.
- whisperx will download its model on first use; the environment must allow this once, or the model can be pre-cached.
- The `spacy_pkuseg` fix for Chinese tokenization is a prerequisite for good Chinese results; the pipeline itself is language-agnostic.
- All external dependencies introduced for the pipeline (whisperx, rapidfuzz if used) must be added to `requirements.txt`.
