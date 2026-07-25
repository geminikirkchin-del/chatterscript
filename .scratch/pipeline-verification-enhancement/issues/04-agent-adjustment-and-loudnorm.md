# 04 — AI agent parameter adjustment + final loudness normalization

**What to build:** The rule-based parameter agent uses the richer verification layer results to make targeted parameter adjustments for retries, and the final composed audio is normalized to a target LUFS / True Peak using FFmpeg loudnorm.

**Blocked by:** 01 — Bootstrap verification environment + FFmpeg audio metrics hardfail; 02 — WhisperX ASR alignment + jiwer content accuracy hardfail.

**Status:** ready-for-agent

- [ ] Extend `ParameterAgent.decide()` to accept the full `QualityVerificationResult.layer_results` instead of only `audio_failure` / `asr_failure`.
- [ ] Add parameter adjustment rules for new failure reasons: `lufs_out_of_range`, `true_peak_exceeded`, `dynamic_range_low`, `whisperx_low_confidence`, `text_coverage_low`, `wer_too_high`, `cer_too_high`.
- [ ] Ensure duration deviation and long silence logic continues to work using the new audio-metrics layer output.
- [ ] Apply FFmpeg loudnorm to the final composed audio so the whole video has consistent loudness (target -16 LUFS, True Peak -1 dBTP).
- [ ] Display the agent decision and retry reason in the pipeline UI segment cards.
- [ ] Add unit tests for the extended parameter agent and final loudness normalization.
