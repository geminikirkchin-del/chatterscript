import json
import urllib.request
import sys

API = "http://localhost:8004"

with open("outputs/46-script-zh-cn-pure.md", "r", encoding="utf-8") as f:
    text = f.read()

payload = {
    "text": text,
    "voice_mode": "clone",
    "reference_audio_filename": "Kirk_Zh.wav",
    "language": "zh",
    "temperature": 0.8,
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "speed_factor": 1.0,
    "seed": 888,
    "output_format": "wav",
}

req = urllib.request.Request(
    f"{API}/api/tts-pipeline",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        print(json.dumps(result, indent=2, ensure_ascii=False))
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode('utf-8')}", file=sys.stderr)
    sys.exit(1)
