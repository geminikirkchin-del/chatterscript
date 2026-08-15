import urllib.request
import json
import sys

with open(sys.argv[1], 'r', encoding='utf-8') as f:
    script_text = f.read().strip()

if not script_text:
    print('Empty script')
    sys.exit(1)

payload = {
    'text': script_text,
    'job_name': sys.argv[2] if len(sys.argv) > 2 else 'manual-job',
    'voice_mode': 'predefined',
    'predefined_voice_id': 'Kirk_Zh.wav',
    'temperature': 0.7,
    'exaggeration': 0.6,
    'cfg_weight': 0.5,
    'speed_factor': 1.0,
    'seed': 888,
    'language': 'zh'
}

req = urllib.request.Request(
    'http://localhost:8004/api/tts-pipeline',
    data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    with urllib.request.urlopen(req) as resp:
        print(json.dumps(json.loads(resp.read().decode()), indent=2))
except urllib.error.HTTPError as e:
    print('HTTP', e.code, e.read().decode())
