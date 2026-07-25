# 01 — Bootstrap verification environment + FFmpeg audio metrics hardfail

**What to build:** A separate verification environment exists and the pipeline can reject segments that violate loudness / peak / dynamic-range thresholds using FFmpeg. The UI shows LUFS, True Peak, RMS, and Dynamic Range for every segment.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Create a setup script that builds an isolated verification environment with the required Python packages (whisperx, resemblyzer, and their torch/numpy dependencies) without touching the main TTS runtime.
- [ ] Define the `QualityVerifier` interface and `QualityVerificationResult` dataclass that all future verifiers will implement.
- [ ] Implement `FFmpegAudioMetricsVerifier` that computes integrated LUFS, True Peak, RMS, and Dynamic Range via FFmpeg / ffprobe / pydub.
- [ ] Add `pipeline.verification.layers.audio_metrics` config with starter thresholds (target LUFS -16 ± 2, True Peak < -1 dBTP, min RMS 0.01, min dynamic range 10 dB).
- [ ] Wire the verifier into the pipeline so a hard-fail on audio metrics triggers retry and is recorded in the segment verification log.
- [ ] Show audio metric scores in the pipeline UI segment cards.
- [ ] Add unit tests for the FFmpeg verifier adapter and the `QualityVerifier` orchestration.
