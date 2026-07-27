import sys
sys.path.insert(0, '.')
import numpy as np
import soundfile as sf

def energy_peaks(a, sr, frame_ms=20, min_sep_ms=100):
    frame = int(sr * frame_ms / 1000)
    n = len(a) // frame
    e = np.sqrt(np.mean(a[:n*frame].astype(np.float64).reshape(n, frame) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(e, 1e-10))
    floor = np.percentile(db, 20)
    thresh = max(floor + 8, -45)
    min_sep = int(min_sep_ms / frame_ms)
    peaks = []
    i = 1
    while i < n - 1:
        if db[i] > thresh and db[i] >= db[i-1] and db[i] > db[i+1]:
            if not peaks or i - peaks[-1] >= min_sep:
                peaks.append(i)
        i += 1
    return np.array(peaks) * frame_ms / 1000.0, db

def drift_and_activity(path):
    a, sr = sf.read(path, dtype='float32')
    peaks, db = energy_peaks(a, sr)
    if len(peaks) < 6:
        return None
    t0, t1 = peaks[0], peaks[-1]
    mid = (t0 + t1) / 2
    r1 = len([p for p in peaks if p < mid]) / max(mid - t0, 0.1)
    r2 = len([p for p in peaks if p >= mid]) / max(t1 - mid, 0.1)
    drift = max(r1, r2) / max(min(r1, r2), 0.1)
    thresh = max(np.percentile(db, 20) + 8, -45)
    active = (db > thresh).mean()
    return round(drift, 2), round(r1, 1), round(r2, 1), round(active, 2), len(peaks)

samples = [
    ('good0', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/0.wav'),
    ('good5', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/5.wav'),
    ('good10', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/10.wav'),
    ('good20', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/20.wav'),
    ('good50', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/50.wav'),
    ('bad3-speedup+garbage', 'outputs/pipeline_jobs/pipe-14aff16de931/segments/3.wav'),
]
for label, p in samples:
    print(label, drift_and_activity(p))
