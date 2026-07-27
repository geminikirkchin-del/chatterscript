import sys, json
sys.path.insert(0, '.')
import numpy as np
import soundfile as sf
from pathlib import Path
from pipeline.verification_wrappers.runner import run_whisperx_align

def words_of(result):
    ws = []
    for seg in result.get('aligned_segments', []):
        for w in seg.get('words', []):
            if isinstance(w.get('start'), (int, float)) and isinstance(w.get('end'), (int, float)):
                ws.append(w)
    return ws

def tempo_drift(words):
    if len(words) < 4:
        return None, []
    t0, t1 = words[0]['start'], words[-1]['end']
    span = t1 - t0
    if span < 2.0:
        return None, []
    n_win = max(2, int(span / 2.0))
    rates = []
    for k in range(n_win):
        a = t0 + span * k / n_win
        b = t0 + span * (k + 1) / n_win
        chars = sum(len(w['word']) for w in words if w['start'] >= a and w['start'] < b)
        dur = b - a
        if chars >= 2:
            rates.append(chars / dur)
    if len(rates) < 2:
        return None, rates
    r = sorted(rates)
    p10 = r[max(0, int(len(r) * 0.1) - 1)] if len(r) > 2 else r[0]
    p90 = r[min(len(r) - 1, int(len(r) * 0.9))]
    return (p90 / max(p10, 1e-6)), rates

def trailing(words, audio_path):
    if not words:
        return None, None
    a, sr = sf.read(audio_path, dtype='float32')
    dur = len(a) / sr
    last_end = words[-1]['end']
    tail = dur - last_end
    tail_audio = a[int(last_end * sr):]
    tail_rms = 20 * np.log10(max(float(np.sqrt(np.mean(tail_audio.astype(np.float64) ** 2))), 1e-10)) if len(tail_audio) else None
    return round(tail, 2), round(tail_rms, 1) if tail_rms is not None else None

jobs = [
    ('v4-good', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments', [0, 5, 10, 20, 50, 85]),
    ('v2-bad', 'outputs/pipeline_jobs/pipe-14aff16de931/segments', [3]),
]
for label, base, idxs in jobs:
    for i in idxs:
        p = f'{base}/{i}.wav'
        txt = Path(f'{base}/{i}.wav')
        r = run_whisperx_align(audio_path=p, reference_text='參考', language='zh', model_name='small', device='auto')
        if not r:
            print(f'{label} seg{i}: whisperx failed'); continue
        ws = words_of(r)
        drift, rates = tempo_drift(ws)
        tail, tail_rms = trailing(ws, p)
        rates_str = ' '.join(f'{x:.1f}' for x in rates) if rates else '-'
        print(f'{label} seg{i}: words={len(ws)} drift={drift and round(drift,2)} rates=[{rates_str}] tail={tail}s tail_rms={tail_rms}')
