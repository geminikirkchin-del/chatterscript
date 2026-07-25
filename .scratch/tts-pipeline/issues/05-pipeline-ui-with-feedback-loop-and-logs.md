# 05 — Pipeline UI with feedback loop and logs

**What to build:** A web UI page where users can submit a long script, monitor pipeline progress segment-by-segment, inspect pipeline logs, provide feedback on individual segments, and download the final composed audio. The feedback is persisted and used by the AI agent to improve future parameter decisions.

**Blocked by:** 01 — Happy-path async TTS pipeline, 04 — AI agent parameter adjustment

**Status:** ready-for-agent

- [ ] A Pipeline page lets the user paste a long script and choose voice mode, voice file, and generation params
- [ ] Submitting a script calls `POST /api/tts-pipeline` and navigates to a job dashboard
- [ ] The dashboard polls job status and shows each segment's status, retry count, audio score, ASR score, and combined score
- [ ] Each segment displays pipeline logs including failure reasons, retry history, and AI agent parameter adjustments
- [ ] Users can play and download individual segment audio files
- [ ] Users can play and download the final composed audio
- [ ] Users can approve, reject, or rate each segment; feedback is written to `outputs/pipeline_jobs/feedback.jsonl`
- [ ] The AI agent references recent feedback when making future parameter adjustments
