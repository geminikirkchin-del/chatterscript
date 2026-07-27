#!/bin/bash
JOB_ID="pipe-746b812f23c2"
API="http://localhost:8004"
LOG="outputs/pipeline_monitor.log"
echo "Monitoring $JOB_ID starting $(date)" > "$LOG"
while true; do
  python_embedded/python.exe .scratch/monitor_pipeline.py "$JOB_ID" >> "$LOG" 2>&1
  echo "--- $(date) ---" >> "$LOG"
  STATUS=$(python_embedded/python.exe -c "
import json, urllib.request
with urllib.request.urlopen('$API/api/tts-pipeline/$JOB_ID') as r:
    print(json.loads(r.read())['status'])
" 2>/dev/null)
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    echo "Job finished with status: $STATUS at $(date)" >> "$LOG"
    break
  fi
  sleep 60
done
