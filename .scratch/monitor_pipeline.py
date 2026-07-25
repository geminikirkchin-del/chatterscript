import json
import urllib.request
import sys

job_id = sys.argv[1] if len(sys.argv) > 1 else "pipe-1ad5ea32935e"
API = "http://localhost:8004"

req = urllib.request.Request(f"{API}/api/tts-pipeline/{job_id}")
try:
    with urllib.request.urlopen(req) as resp:
        job = json.loads(resp.read().decode("utf-8"))
        print(f"Job: {job['job_id']}")
        print(f"Status: {job['status']}")
        print(f"Segments: {len(job['segments'])}")
        for seg in job['segments']:
            print(f"  #{seg['index']+1}: {seg['status']} retries={seg['retry_count']} score={seg.get('score')} audio={seg.get('audio_score')} asr={seg.get('asr_score')} fail={seg.get('failure_reason')}")
        if job.get('final_audio_path'):
            print(f"Final: {job['final_audio_path']}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode('utf-8')}", file=sys.stderr)
