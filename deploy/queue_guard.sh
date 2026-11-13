#!/usr/bin/env bash
# ExecStartPre of every worker unit: do not let a worker start on a queue that holds OLD jobs until a person has looked.
#
# A queue that survived an outage may hold jobs nobody wants any more (or that were already done before a Redis snapshot was
# restored). Starting a worker would run them at once. This guard looks - read-only, it never removes or runs anything:
#   exit 0  start the worker  (queue empty, jobs younger than the limit, mode warn/off, or a recent acknowledgement)
#   exit 3  refuse            (the unit is configured not to retry on 3, so it does not spin; see logs/queue-guard.log)
#
# Look at what waits:      deploy/inspect_queues.sh
# Accept it and start:     deploy/queue_ack.sh        (valid for 30 minutes, covers all three workers)
#
# PPC_QUEUE_GUARD=block (default) | warn | off      PPC_STALE_HOURS=6 (jobs older than this are "old")
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_NAME=queue-guard
. "$HERE/_common.sh"

MODE="${PPC_QUEUE_GUARD:-block}"
STALE=$(( ${PPC_STALE_HOURS:-6} * 3600 ))
ACK="$LOGS/.ppc-queue-ack"
[ "$MODE" = off ] && exit 0

if ! $RQ ping >/dev/null 2>&1; then
	log "queue redis on port $QUEUE_PORT does not answer - starting anyway; the worker keeps retrying until it does"
	exit 0
fi

stale=0
while read -r key; do
	[ -z "$key" ] && continue
	n="$($RQ llen "$key")"; age="$(oldest_age "$key")"
	if [ "$n" -gt 0 ] && [ "$age" -gt "$STALE" ]; then
		log "STALE: $key holds $n job(s), the oldest waiting $((age / 3600)) h $(((age % 3600) / 60)) min"
		stale=1
	fi
done < <(queue_names)

[ "$stale" = 0 ] && exit 0
if [ -f "$ACK" ] && [ $(( $(date +%s) - $(stat -c %Y "$ACK") )) -lt 1800 ]; then
	log "stale jobs present but acknowledged ($(stat -c %y "$ACK")) - starting"
	exit 0
fi
if [ "$MODE" = warn ]; then
	log "stale jobs present - mode warn, starting"
	exit 0
fi
log "REFUSING to start a worker: old jobs wait in the queue. Inspect with deploy/inspect_queues.sh, then acknowledge with deploy/queue_ack.sh"
exit 3
