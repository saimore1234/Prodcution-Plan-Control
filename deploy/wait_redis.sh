#!/usr/bin/env bash
# ExecStartPost of the Redis units: the unit counts as started only when the server answers PING (up to 30 s).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/_common.sh"
case "${1:-queue}" in queue) cmd="$RQ";; cache) cmd="$RC";; *) echo "usage: wait_redis.sh queue|cache" >&2; exit 2;; esac
for _ in $(seq 1 30); do
	[ "$($cmd ping 2>/dev/null)" = PONG ] && exit 0
	sleep 1
done
echo "redis ($1) did not answer PING within 30 s" >&2
exit 1
