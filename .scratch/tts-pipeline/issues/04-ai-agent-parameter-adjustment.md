# 04 — AI agent parameter adjustment

**What to build:** When a segment fails all 3 base retries, a local rule-based AI agent inspects the failure metadata and adjusts generation parameters before another attempt. The agent records the parameter deltas and outcomes. Segments that still fail after agent-adjusted attempts are marked failed and the pipeline continues. A combined quality score is computed for every segment.

**Blocked by:** 02 — Audio verification and retry, 03 — ASR verification via whisperx

**Status:** ready-for-agent

- [ ] After 3 base retries, the AI agent selects new parameters based on failure type
- [ ] ASR mismatch failures lower temperature and raise cfg_weight
- [ ] Silence or low-RMS failures lower exaggeration and change seed
- [ ] Clipping failures lower exaggeration
- [ ] Agent parameter adjustments and outcomes are recorded in the segment log
- [ ] Segments that fail after agent adjustment are marked `failed` and skipped from final composition
- [ ] Each segment gets a combined quality score derived from audio and ASR verification results
