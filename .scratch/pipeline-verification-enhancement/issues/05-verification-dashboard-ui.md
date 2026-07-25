# 05 — Verification dashboard UI

**What to build:** The pipeline tab in the web UI shows a complete verification dashboard with per-layer scores, failure reasons, a word-level confidence heatmap, and a detailed segment view.

**Blocked by:** 01 — Bootstrap verification environment + FFmpeg audio metrics hardfail; 02 — WhisperX ASR alignment + jiwer content accuracy hardfail; 03 — Speaker consistency + spectral consistency feedback loop; 04 — AI agent parameter adjustment + final loudness normalization.

**Status:** ready-for-agent

- [ ] Add `GET /api/tts-pipeline/{job_id}/segment/{index}` endpoint that returns the full verification details for a segment, including word-level timestamps.
- [ ] Update the pipeline job list / detail API responses to include per-layer scores.
- [ ] Build a verification dashboard panel in the pipeline UI that shows: LUFS/True Peak/RMS/Dynamic Range, WhisperX confidence/WER/CER, speaker similarity, spectral consistency, failure reason, and agent retry decision.
- [ ] Implement a word-level confidence heatmap / timeline for segments that have WhisperX alignment data.
- [ ] Preserve and link to intermediate audio files for human review.
- [ ] Add frontend and API tests for the dashboard.
