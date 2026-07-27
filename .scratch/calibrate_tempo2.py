import sys
sys.path.insert(0, '.')
import numpy as np
from pipeline.verification_wrappers.runner import run_whisperx_align

def words_of(result):
    ws = []
    for seg in result.get('aligned_segments', []):
        for w in seg.get('words', []):
            if isinstance(w.get('start'), (int, float)) and isinstance(w.get('end'), (int, float)):
                ws.append(w)
    return ws

def drift_pairs(words, max_gap=0.5):
    rates = []
    for a, b in zip(words, words[1:]):
        dur = b['start'] - a['start']
        if dur <= 0.01 or dur > max_gap:
            continue
        rates.append(len(b['word']) / dur)
    if len(rates) < 3:
        return None, rates
    r = sorted(rates)
    p10 = r[int(len(r) * 0.1)]
    p90 = r[min(len(r) - 1, int(len(r) * 0.9))]
    return round(p90 / max(p10, 1e-6), 2), rates

samples = [
    ('good0', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/0.wav'),
    ('good5', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/5.wav'),
    ('good10', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/10.wav'),
    ('good20', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/20.wav'),
    ('good50', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/50.wav'),
    ('bad3', 'outputs/pipeline_jobs/pipe-14aff16de931/segments/3.wav'),
]
for label, p in samples:
    r = run_whisperx_align(audio_path=p, reference_text='參考', language='zh', model_name='small', device='auto')
    if not r:
        print(f'{label}: whisperx failed'); continue
    d, rates = drift_pairs(words_of(r))
    rr = ' '.join(f'{x:.1f}' for x in sorted(rates))
    print(f'{label}: drift={d} pairs={len(rates)} rates=[{rr}]')
