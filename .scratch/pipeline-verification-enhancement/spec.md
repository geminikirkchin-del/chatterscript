# Spec: Multi-Layer Pipeline Verification Enhancement

## Problem Statement

The current long-form TTS pipeline already splits scripts into short segments, runs basic audio verification (silence, clipping, RMS, duration deviation), and optionally compares a WhisperX transcription back to the original text. In practice, this is not enough to guarantee stable quality for 10+ minute Chinese educational voice-overs:

- Segments still fail with `duration_deviation` and `long_silence` after repeated retries, because the existing checks only look at the generated audio, not at loudness consistency, word-level alignment, or content accuracy.
- There is no objective measure that all segments sound like the same cloned voice (cross-segment consistency).
- There is no objective measure that the overall long-form output has stable loudness, dynamic range, and spectral character (long-form stability).
- When a segment passes the current checks but sounds wrong, the pipeline has no richer feedback to feed the parameter agent, so the same failure mode can repeat across segments.

The user wants a verification stack that is "quality-first" and produces consistent, reviewable results for Slidev-Kw educational content.

## Solution

Introduce a single `QualityVerifier` seam that runs five verification layers after each segment is generated. The layers are split into **hard-fail** layers (trigger retry / agent adjustment) and **feedback-only** layers (record scores for the feedback loop and future segment tuning):

**Hard-fail layers:**

1. **Audio Metrics (FFmpeg / ffprobe / pydub)** — LUFS, True Peak, RMS, Dynamic Range.
2. **ASR + Alignment (WhisperX)** — word-level timestamps and per-word confidence.
3. **Content Accuracy (jiwer)** — WER / CER between original text and ASR transcription.

**Feedback-only layers:**

4. **Voice Consistency (Resemblyzer)** — speaker embedding cosine similarity against the reference voice.
5. **Spectral Consistency (Librosa)** — MFCC MSE and Spectral Contrast against a reference segment.

All layers run in a **separate verification environment** to avoid dependency conflicts with the TTS runtime (torch 2.5.1+cu121 / numpy 2.2.6). The pipeline invokes them via subprocess, passing audio paths and receiving JSON results.

The parameter agent is extended to react to the new failure modes and feedback scores, so retries are driven by richer diagnostics instead of a single pass/fail bit.

## User Stories

1. As a content creator, I want every generated segment to meet loudness and peak targets, so that the final video does not have sections that are too quiet or distorted.
2. As a content creator, I want the pipeline to detect when a segment is missing words or mispronounced, so that I do not publish inaccurate narration.
3. As a content creator, I want the pipeline to compare the TTS output against my original script character-by-character, so that any content drift is caught automatically.
4. As a content creator, I want all segments in a long video to sound like the same cloned voice, so that the narration feels continuous and professional.
5. As a content creator, I want the pipeline to measure spectral consistency across segments, so that subtle音色 drift over a 15-minute video is flagged.
6. As a developer, I want the verification tools isolated from the TTS environment, so that installing whisperx or resemblyzer does not break the existing CUDA/TTS runtime.
7. As a developer, I want a single `QualityVerifier` interface, so that adding a new verification layer does not require changing the pipeline orchestrator.
8. As a developer, I want hard-fail and feedback-only behaviors configurable per layer, so that I can tune strictness without rewriting code.
9. As a content creator, I want the UI to show per-layer scores and failure reasons for each segment, so that I can understand why a segment was retried or rejected.
10. As a content creator, I want failed segments and intermediate audio files to be preserved, so that I can inspect them and provide feedback to the AI agent.
11. As a developer, I want the AI agent to adjust parameters differently for audio-metric failures vs ASR failures vs content-accuracy failures, so that retries are targeted and do not waste GPU time.
12. As a developer, I want the feedback store to record per-layer scores and the final parameter set, so that the system learns which parameter combinations produce the best quality for each voice and language.
13. As a content creator, I want the composed final audio to be normalized to a target LUFS, so that the whole video has consistent loudness.
14. As a developer, I want verification to be optional and degradable, so that if the verification environment is missing the pipeline can still fall back to the existing simpler checks.
15. As a content creator, I want word-level timestamps from WhisperX to be exposed in the UI, so that I can check whether fast or unclear sections are aligned with the script.
16. As a developer, I want threshold values stored in config, so that users can adjust them without code changes.
17. As a developer, I want unit tests for each verifier adapter, so that I can change thresholds or tools without breaking the pipeline.
18. As a content creator, I want the pipeline to retry with adjusted parameters up to a configurable maximum, so that a single bad segment does not kill the whole job but also does not retry forever.
19. As a developer, I want a clear log entry for every verification layer result, so that debugging a failed segment is straightforward.
20. As a content creator, I want the final composed audio to include only passing segments (or clearly mark failed segments), so that I know whether the output is complete.

## Implementation Decisions

### 1. Single `QualityVerifier` seam

All verification layers are hidden behind one interface:

```python
@dataclass
class QualityVerificationResult:
    passed: bool
    overall_score: float
    failure_reason: Optional[str]
    layer_results: Dict[str, Dict[str, Any]]

class QualityVerifier:
    def verify(
        self,
        audio_path: str,
        original_text: str,
        reference_voice_path: Optional[str],
        language: str,
        expected_duration: float,
    ) -> QualityVerificationResult: ...
```

The pipeline orchestrator (`PipelineService._generate_and_verify_segment`) calls this single method after audio generation. The orchestrator does not know about FFmpeg, whisperx, jiwer, etc.

### 2. Adapter-based layer implementation

Each layer is a small adapter class that implements a common internal protocol:

- `FFmpegAudioMetricsVerifier` — computes LUFS, True Peak, RMS, Dynamic Range via `ffmpeg` / `ffprobe`. Runs in the main Python process because ffmpeg is an external binary with no Python dependency conflicts.
- `WhisperXAlignmentVerifier` — runs WhisperX in the separate verification environment via subprocess; returns transcription, word-level timestamps, and per-word confidence.
- `JiwerContentVerifier` — computes WER and CER between normalized original text and WhisperX transcription. Runs in the main process (jiwer has low conflict risk).
- `ResemblyzerSpeakerVerifier` — computes speaker embedding cosine similarity against the reference voice. Runs in the separate verification environment via subprocess.
- `LibrosaSpectralVerifier` — computes MFCC MSE and Spectral Contrast against a reference segment (the first passing segment of the job, or the reference voice file). Runs in the main process (librosa is already available).

### 3. Separate verification environment

A dedicated virtual environment or conda environment (`.verification_venv` or similar) holds:

- `whisperx`
- `resemblyzer`
- `torch` / `torchaudio` versions compatible with those tools
- `numpy<2` if required by whisperx

The main TTS environment is untouched. The pipeline invokes verification subprocesses with JSON input / JSON output. If the environment is missing, the pipeline logs a warning and falls back to the existing `verify_audio` + `ASRVerifier` behavior.

### 4. Hard-fail vs feedback-only configuration

Config shape (added under `pipeline.verification`):

```yaml
pipeline:
  verification:
    layers:
      audio_metrics:
        enabled: true
        hardfail: true
        thresholds:
          target_lufs: -16.0
          lufs_tolerance: 2.0
          true_peak_max_dbtp: -1.0
          min_rms: 0.01
          min_dynamic_range_db: 10.0
      whisperx_alignment:
        enabled: true
        hardfail: true
        thresholds:
          min_mean_word_confidence: 0.70
          min_text_coverage_ratio: 0.90
      jiwer_content:
        enabled: true
        hardfail: true
        thresholds:
          max_wer: 0.15
          max_cer: 0.10
      resemblyzer_speaker:
        enabled: true
        hardfail: false
        thresholds:
          min_similarity: 0.75
      librosa_spectral:
        enabled: true
        hardfail: false
        thresholds:
          max_mfcc_mse: 0.05
          min_spectral_contrast: 0.80
```

The `QualityVerifier` reads this config. Hard-fail layers determine `passed`; feedback-only layers always contribute metrics but never cause failure.

### 5. AI agent extension

The `ParameterAgent` receives the full `QualityVerificationResult.layer_results` instead of just `audio_failure` / `asr_failure`. New failure categories drive new deltas:

- `lufs_out_of_range` / `true_peak_exceeded` / `dynamic_range_low` → adjust exaggeration and cfg_weight.
- `whisperx_low_confidence` / `text_coverage_low` → lower temperature, raise cfg_weight.
- `wer_too_high` / `cer_too_high` → lower temperature, consider speed_factor adjustment if words are dropped at the end.
- Existing `duration_deviation` / `long_silence` / `clipping` logic remains, driven by the audio-metrics layer output.

### 6. Reference segment for spectral consistency

The first segment that passes all hard-fail layers becomes the spectral reference for the rest of the job. If it later fails, the reference is frozen. This keeps cross-segment spectral comparison meaningful.

### 7. Final compose normalization

After composing all passing segments, run FFmpeg loudnorm to target `-16 LUFS` with `-1 dBTP` true peak. This guarantees long-form loudness consistency even if individual segments drift slightly.

### 8. UI/API surface

- Extend `Segment.to_dict()` to include `layer_results` and the new `failure_reason` values.
- Add `GET /api/tts-pipeline/{job_id}/segment/{index}` to fetch detailed verification data for one segment, including word-level timestamps.
- Pipeline UI segment cards show: LUFS, True Peak, WER/CER, speaker similarity, spectral contrast score, and a per-word confidence heatmap.

### 9. Degradation path

If the verification environment is unavailable:

- WhisperX-based layers are skipped with `passed=True` and a `skipped` metric.
- Resemblyzer is skipped similarly.
- FFmpeg / jiwer / librosa remain available in the main environment and continue to run.

## Testing Decisions

- **External behavior only**: tests assert that `QualityVerifier.verify()` returns the correct `passed`/`failure_reason`/`layer_results` for given audio files and text, without inspecting how tools are called.
- **Seam tests**: mock the subprocess invocations for WhisperX and Resemblyzer so tests do not require the heavy verification environment.
- **Real-tool smoke tests**: one optional integration test that runs FFmpeg and jiwer on a real generated segment to catch environment regressions.
- **Agent tests**: extend `test_pipeline_agent.py` to verify new failure-category handling.
- **Prior art**: `tests/test_pipeline_verifier.py` already tests `verify_audio`; `tests/test_pipeline_asr.py` already tests ASR verification. These patterns are reused for the new verifier adapters.

## Out of Scope

- Real-time/streaming verification.
- Training custom ASR or speaker-embedding models.
- Automatic re-segmentation of failed segments (splitting a failed segment into smaller pieces).
- Video generation or subtitle burning.
- Distributed/cloud verification workers.

## Further Notes

- The separate verification environment should be created by a setup script (`scripts/setup_verification_env.py` or similar) and documented in `README.md`.
- Thresholds are intentionally conservative starter values. The first few real jobs will produce data to tune them.
- The current running job (`pipe-746b812f23c2`) is already showing `duration_deviation` and `long_silence` failures; this spec directly addresses those failure modes through richer audio metrics and better agent decisions.
- Future work: once Resemblyzer/Librosa feedback data is collected, consider promoting them to hard-fail with learned thresholds per voice.
