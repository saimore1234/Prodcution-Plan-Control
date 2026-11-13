#!/usr/bin/env bash
# Tell the queue guard that you have looked at the old jobs (deploy/inspect_queues.sh) and want the workers to start.
# Valid for 30 minutes, then the guard is strict again. Does not touch the queue.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_NAME=queue-guard
. "$HERE/_common.sh"
touch "$LOGS/.ppc-queue-ack"
log "stale queue acknowledged by $(id -un): workers may start for the next 30 minutes"
