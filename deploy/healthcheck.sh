#!/usr/bin/env bash
# Read-only health check of a bench. Exit 0 = healthy, 1 = something needs attention (details in logs/health.log).
# It restarts nothing: the units' Restart=always does that; this tells a person what the units cannot (queue too old, a worker
# missing, a second worker that should not exist).
#   PPC_EXPECTED_WORKERS=3   PPC_STALE_HOURS=6   PPC_QUEUE_LIMIT=200
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_NAME=health
. "$HERE/_common.sh"

EXPECTED="${PPC_EXPECTED_WORKERS:-3}"
STALE=$(( ${PPC_STALE_HOURS:-6} * 3600 ))
LIMIT="${PPC_QUEUE_LIMIT:-200}"
bad=0
fail() { log "UNHEALTHY: $*"; bad=1; }

[ "$($RQ ping 2>/dev/null)" = PONG ] || fail "queue redis (port $QUEUE_PORT) does not answer"
[ "$($RC ping 2>/dev/null)" = PONG ] || fail "cache redis (port $CACHE_PORT) does not answer"

if [ "$bad" = 0 ]; then
	workers="$($RQ scard rq:workers)"
	[ "$workers" -lt "$EXPECTED" ] && fail "$workers worker(s) registered, $EXPECTED expected"
	[ "$workers" -gt "$EXPECTED" ] && fail "$workers workers registered, only $EXPECTED expected - a duplicate (a second 'bench start' or a hand-started worker?)"
	while read -r key; do
		[ -z "$key" ] && continue
		n="$($RQ llen "$key")"; age="$(oldest_age "$key")"
		[ "$age" -gt "$STALE" ] && fail "$key: oldest job waits $((age / 3600)) h ($n queued) - workers stopped or jobs not consumed"
		[ "$n" -gt "$LIMIT" ] && fail "$key holds $n jobs (limit $LIMIT)"
	done < <(queue_names)
fi

if command -v systemctl >/dev/null 2>&1; then
	for u in redis-queue redis-cache web worker-short worker-default worker-long scheduler; do
		systemctl list-unit-files "ppc-bench-$u.service" >/dev/null 2>&1 || continue
		systemctl is-enabled --quiet "ppc-bench-$u.service" 2>/dev/null || continue
		systemctl is-active --quiet "ppc-bench-$u.service" || fail "ppc-bench-$u.service is not active"
	done
fi

[ "$bad" = 0 ] && log "healthy: queue and cache redis answer, $($RQ scard rq:workers) worker(s) registered"
exit "$bad"
