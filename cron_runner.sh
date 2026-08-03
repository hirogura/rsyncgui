#!/bin/bash
PAIR_ID="$1"
LOCK_FILE="/tmp/rsyncgui_cron_lock_${PAIR_ID}"
LOG_DIR="/opt/rsyncgui/logs"
LOG_FILE="$LOG_DIR/pair_${PAIR_ID}.json"
TIMESTAMP=$(date +%s)
TIME_STR=$(date '+%Y-%m-%d %H:%M:%S')
LOG_TMP=$(mktemp /tmp/rsyncgui_cron_log_XXXXXX)

if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "{\"timestamp\":$TIMESTAMP,\"time\":\"$TIME_STR\",\"exitCode\":-1,\"command\":\"skipped\",\"log\":\"Previous run still active (PID $LOCK_PID), skipping.\"}" >> /tmp/rsyncgui_cron_skip_${PAIR_ID}.log
        rm -f "$LOG_TMP"
        exit 0
    fi
    rm -f "$LOCK_FILE"
fi

echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

shift

"$@" > "$LOG_TMP" 2>&1
EXIT_CODE=$?

mkdir -p "$LOG_DIR"

python3 << PYEOF
import json, sys, os

log_file = "$LOG_FILE"
log_tmp = "$LOG_TMP"
timestamp = $TIMESTAMP
time_str = "$TIME_STR"
exit_code = $EXIT_CODE
cmd = " ".join(sys.argv[1:])

try:
    with open(log_tmp, "r") as f:
        log_output = f.read()
except:
    log_output = ""

try:
    with open(log_file, "r") as f:
        logs = json.load(f)
except:
    logs = []

logs.append({
    "timestamp": timestamp,
    "time": time_str,
    "exitCode": exit_code,
    "command": cmd,
    "log": log_output
})

cutoff = timestamp - 30 * 86400
logs = [l for l in logs if l.get("timestamp", 0) > cutoff]

with open(log_file, "w") as f:
    json.dump(logs, f, ensure_ascii=False)
PYEOF

rm -f "$LOG_TMP"
exit $EXIT_CODE
