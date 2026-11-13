#!/usr/bin/env bash
# ExecStopPost of every bench unit: one line per stop in logs/service-events.log (unit, result, exit code, status).
# result is "success" for a requested stop and e.g. "exit-code", "signal", "timeout", "oom-kill" for a failure.
LOGS="${PPC_LOG_DIR:-${BENCH_DIR:-$HOME/frappe-bench}/logs}"
mkdir -p "$LOGS" 2>/dev/null
echo "$(date -Is) unit=${1:-?} result=${2:-?} exit_code=${3:-?} exit_status=${4:-?}" >> "$LOGS/service-events.log"
exit 0
