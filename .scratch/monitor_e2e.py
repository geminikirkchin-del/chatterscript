import urllib.request, json, time
from pathlib import Path

job_id = Path('outputs/pipeline_e2e_job_id.txt').read_text().strip()
print(f'Monitoring {job_id}', flush=True)
while True:
    try:
        resp = urllib.request.urlopen(f'http://localhost:8004/api/tts-pipeline/{job_id}', timeout=30)
        job = json.loads(resp.read().decode('utf-8'))
        segs = job.get('segments', [])
        done = sum(1 for s in segs if s['status'] == 'passed')
        failed = sum(1 for s in segs if s['status'] == 'failed')
        cur = next((s for s in segs if s['status'] in ('generating', 'pending')), None)
        cur_txt = f"working on seg {cur['index']}" if cur else "idle"
        print(f"{time.strftime('%H:%M:%S')} status={job.get('status')} passed={done} failed={failed} {cur_txt}", flush=True)
        if job.get('status') in ('done', 'failed'):
            print('FINAL:', job.get('final_audio_path'), flush=True)
            break
    except Exception as e:
        print('poll error:', e, flush=True)
    time.sleep(30)
