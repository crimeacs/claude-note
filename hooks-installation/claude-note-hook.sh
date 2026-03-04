#!/bin/bash
# claude-note hook script — posts events and uploads transcripts on Stop.
API_URL="${CLAUDE_NOTE_API:-http://localhost:8000}"
INPUT=$(cat)

# Post the event (fast, 2s timeout)
echo "$INPUT" | curl -sf --max-time 2 -X POST "$API_URL/events" \
  -H 'Content-Type: application/json' -d @- || true

# On Stop: upload the transcript file
EVENT=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('hook_event_name',''))" 2>/dev/null)
if [ "$EVENT" = "Stop" ]; then
  TPATH=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null)
  SID=$(echo "$INPUT" | python3 -c "import sys,json;print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
  if [ -n "$TPATH" ] && [ -f "$TPATH" ] && [ -n "$SID" ]; then
    curl -sf --max-time 15 -X POST "$API_URL/transcripts/$SID" \
      -H 'Content-Type: application/octet-stream' \
      --data-binary "@$TPATH" || true
  fi
fi
