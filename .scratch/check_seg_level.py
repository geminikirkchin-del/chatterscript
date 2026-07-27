import sys
sys.path.insert(0, '.')
from pipeline.verification_wrappers.runner import run_whisperx_align
import soundfile as sf

for label, p in [
    ('good0', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/0.wav'),
    ('good5', 'outputs/pipeline_jobs/pipe-3374eccaed9b/segments/5.wav'),
    ('bad3', 'outputs/pipeline_jobs/pipe-14aff16de931/segments/3.wav'),
]:
    r = run_whisperx_align(audio_path=p, reference_text='參考', language='zh', model_name='small', device='auto')
    a, sr = sf.read(p, dtype='float32')
    dur = len(a)/sr
    segs = r.get('aligned_segments', [])
    print(f'{label} (audio {dur:.1f}s):')
    for s in segs:
        print(f'  seg start={s.get("start"):.2f} end={s.get("end"):.2f} text={s.get("text","")[:24]}')
