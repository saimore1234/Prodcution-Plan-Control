#!/usr/bin/env bash
# READ-ONLY look at the job queues: per queue the length, the age of the oldest job and what the jobs are.
# Removes nothing, runs nothing.   usage: deploy/inspect_queues.sh [max-jobs-listed-per-queue, default 15]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_NAME=queue-inspect
. "$HERE/_common.sh"
LIMIT="${1:-15}"

echo "queue redis: port $QUEUE_PORT   $($RQ ping 2>&1)"
echo "registered workers: $($RQ scard rq:workers 2>/dev/null)"
for w in $($RQ smembers rq:workers 2>/dev/null); do echo "  $w  queues=$($RQ hget "$w" queues)  state=$($RQ hget "$w" state)"; done
echo
while read -r key; do
	[ -z "$key" ] && continue
	n="$($RQ llen "$key")"; age="$(oldest_age "$key")"
	echo "== $key: $n job(s), oldest waiting $((age / 3600)) h $(((age % 3600) / 60)) min"
	$RQ lrange "$key" 0 $((LIMIT - 1)) | while read -r id; do
		d="$($RQ hget "rq:job:$id" description | sed -E 's/frappe.utils.background_jobs.execute_job\(.*job_name=//' | cut -c1-120)"
		echo "   $($RQ hget "rq:job:$id" enqueued_at)  $d"
	done
done < <(queue_names)
echo
for reg in $($RQ --scan --pattern 'rq:failed:*' 2>/dev/null); do echo "failed registry $reg: $($RQ zcard "$reg")"; done
for reg in $($RQ --scan --pattern 'rq:wip:*' 2>/dev/null); do echo "running registry $reg: $($RQ zcard "$reg")"; done
