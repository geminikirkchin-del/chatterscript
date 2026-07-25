# 03 — Speaker consistency + spectral consistency feedback loop

**What to build:** The pipeline records Resemblyzer speaker-embedding similarity and Librosa spectral/MFCC consistency for every segment. These layers are feedback-only: they never fail a segment, but their scores are stored in the verification log and displayed in the UI for cross-segment consistency monitoring.

**Blocked by:** 01 — Bootstrap verification environment + FFmpeg audio metrics hardfail.

**Status:** ready-for-agent

- [ ] Install Resemblyzer in the isolated verification environment and add a subprocess runner for speaker embedding extraction.
- [ ] Implement `ResemblyzerSpeakerVerifier` that computes cosine similarity between the generated segment and the reference voice file.
- [ ] Implement `LibrosaSpectralVerifier` that computes MFCC MSE and Spectral Contrast against the first passing segment of the job (the spectral reference).
- [ ] Add `pipeline.verification.layers.resemblyzer_speaker` and `pipeline.verification.layers.librosa_spectral` config with `hardfail: false` and starter thresholds.
- [ ] Wire both verifiers into the pipeline as feedback-only layers so they contribute `layer_results` without affecting `passed`.
- [ ] Show speaker similarity and spectral consistency scores in the pipeline UI segment cards.
- [ ] Add unit tests using mocked subprocess responses for Resemblyzer and real-tool tests for Librosa.
