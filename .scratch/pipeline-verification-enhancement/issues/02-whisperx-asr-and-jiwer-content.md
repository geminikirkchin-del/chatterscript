# 02 — WhisperX ASR alignment + jiwer content accuracy hardfail

**What to build:** The pipeline transcribes every generated segment with WhisperX and compares the result to the original script using jiwer. Segments with low word-level confidence or high WER/CER are rejected and retried. The UI shows confidence, WER, and CER per segment.

**Blocked by:** 01 — Bootstrap verification environment + FFmpeg audio metrics hardfail.

**Status:** ready-for-agent

- [ ] Install WhisperX in the isolated verification environment and add a subprocess runner that the main pipeline can call with audio paths and receive JSON results.
- [ ] Implement `WhisperXAlignmentVerifier` that returns transcription, per-word timestamps, and per-word / mean confidence scores.
- [ ] Implement `JiwerContentVerifier` that computes WER and CER between normalized original text and the WhisperX transcription.
- [ ] Add `pipeline.verification.layers.whisperx_alignment` and `pipeline.verification.layers.jiwer_content` config with starter thresholds (mean word confidence > 0.70, text coverage > 0.90, WER < 0.15, CER < 0.10).
- [ ] Wire both verifiers into the pipeline as hard-fail layers with appropriate failure reasons (`whisperx_low_confidence`, `text_coverage_low`, `wer_too_high`, `cer_too_high`).
- [ ] Show WhisperX confidence, WER, and CER in the pipeline UI segment cards.
- [ ] Add unit tests using mocked subprocess responses for WhisperX and real-tool tests for jiwer.
