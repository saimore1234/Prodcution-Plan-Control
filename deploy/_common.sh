#!/usr/bin/env bash
# Shared by the deploy scripts: where the bench is and how to reach its two Redis servers. Read-only helpers.
BENCH_DIR="${BENCH_DIR:-$HOME/frappe-bench}"
LOGS="${PPC_LOG_DIR:-$BENCH_DIR/logs}"
mkdir -p "$LOGS" 2>/dev/null || true

# "redis://127.0.0.1:11000" in common_site_config.json -> host / port
cfg_url() { # key
	python3 - "$BENCH_DIR/sites/common_site_config.json" "$1" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
PY
}
url_port() { echo "${1##*:}" | tr -d '/'; }

QUEUE_PORT="${QUEUE_PORT:-$(url_port "$(cfg_url redis_queue)")}"
CACHE_PORT="${CACHE_PORT:-$(url_port "$(cfg_url redis_cache)")}"
RQ="redis-cli -h 127.0.0.1 -p ${QUEUE_PORT:-11000}"
RC="redis-cli -h 127.0.0.1 -p ${CACHE_PORT:-13000}"
# rq's queue prefix is "<bench folder, dashes>"; read it from the keys instead of guessing
queue_names() { $RQ --scan --pattern 'rq:queue:*' 2>/dev/null | sort; }

log() { echo "$(date -Is) $*" | tee -a "$LOGS/${LOG_NAME:-ppc-deploy}.log"; }

# age in seconds of the oldest job waiting in a queue (empty queue -> 0)
oldest_age() { # queue key
	local id ts
	id="$($RQ lindex "$1" 0)"
	[ -z "$id" ] && { echo 0; return; }
	ts="$($RQ hget "rq:job:$id" enqueued_at)"
	[ -z "$ts" ] && { echo 0; return; }
	echo $(( $(date +%s) - $(date -d "$ts" +%s 2>/dev/null || date +%s) ))
}
