# 02 — Audio verification and retry

**What to build:** After each segment is generated, the pipeline runs audio quality checks using ffmpeg/librosa. Segments that fail are automatically retried up to 3 times. Failure metadata is recorded per segment, and segments that remain failed after retries are skipped during final composition but kept on disk for human review.

**Blocked by:** 01 — Happy-path async TTS pipeline

**Status:** ready-for-agent

- [ ] Each generated segment is checked for long internal silence (>300ms)
- [ ] Each segment is checked for clipping (samples near full scale)
- [ ] Each segment is checked for abnormally low RMS (near-silent output)
- [ ] Each segment is checked for duration deviation >30% from the estimate
- [ ] Failed segments are retried automatically up to 3 times with the original parameters
- [ ] Each verification attempt records the failure type and measured metrics
- [ ] Final composition excludes segments still marked as failed but preserves their files
