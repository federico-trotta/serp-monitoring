#!/usr/bin/env bash
# Cron wrapper for the SERP monitor.
#  - changes into the repository root (paths are relative to the working dir)
#  - calls venv/bin/python directly (no activated shell needed)
#  - holds a process lock for the whole run; skips if a previous run is active
#  - appends stdout/stderr to logs/monitor.log
#
# Example crontab entry (daily at 06:00):
#   0 6 * * * /absolute/path/to/serp-monitor/scripts/run_monitor.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
mkdir -p logs

LOCK_FILE="$REPO_DIR/.serp-monitor.lock"
LOG_FILE="$REPO_DIR/logs/monitor.log"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) previous run still active; skipping" >> "$LOG_FILE"
    exit 0
fi

echo "$(date -u +%FT%TZ) starting SERP monitor" >> "$LOG_FILE"
set +e
"$REPO_DIR/venv/bin/python" main.py >> "$LOG_FILE" 2>&1
status=$?
set -e
echo "$(date -u +%FT%TZ) finished with exit code $status" >> "$LOG_FILE"
exit "$status"
