# 01 — Happy-path async TTS pipeline

**What to build:** A user can submit a long script via `POST /api/tts-pipeline` and receive a job ID. The pipeline splits the script into ~50-second segments, generates each segment with the existing TTS engine, and composes a final audio file with natural pauses. The job status and final audio are retrievable through the API. Job state and intermediate segment files are persisted to disk.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] `POST /api/tts-pipeline` accepts long text, voice config, and generation params and returns a `job_id`
- [ ] `GET /api/tts-pipeline/{job_id}` returns job status, segment list, and final output path
- [ ] `GET /api/tts-pipeline/{job_id}/final` downloads the composed audio
- [ ] Script is split into segments targeting ~50 seconds of audio each, merging short sentences where possible
- [ ] Inter-segment silence of 100–200ms is inserted during composition
- [ ] Job state is persisted to `outputs/pipeline_jobs/{job_id}/job.json` after every state change
- [ ] Intermediate segment WAV files are preserved under `outputs/pipeline_jobs/{job_id}/segments/`
- [ ] Output format and sample rate follow the existing config (`audio_output.format`, `audio_output.sample_rate`)
