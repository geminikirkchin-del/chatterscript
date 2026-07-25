# 03 — ASR verification via whisperx

**What to build:** Every generated segment is transcribed back to text using a locally running whisperx model. The transcription is normalized and compared against the original segment text. A similarity score is recorded, and segments below the configured threshold are treated as verification failures that trigger retry.

**Blocked by:** 02 — Audio verification and retry

**Status:** ready-for-agent

- [ ] whisperx is installed and added to `requirements.txt`
- [ ] Each segment is transcribed locally using whisperx
- [ ] Original text and transcription are normalized before comparison
- [ ] A similarity score between original text and transcription is computed and stored per segment
- [ ] Segments with similarity below the configured threshold are marked as failed and retried
- [ ] whisperx model download is handled gracefully on first use
