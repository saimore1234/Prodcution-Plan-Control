#!/usr/bin/env bash
# Generates (never installs, never starts) systemd units that supervise a Frappe bench:
#   web, short / default / long workers, scheduler, queue Redis, cache Redis, socketio,
#   an hourly MRP-recovery timer per site and a 5-minute health check.
#
#   usage:  deploy/generate_systemd.sh [OUTPUT_DIR]          (default: deploy/systemd/generated)
#   environment (all optional, defaults describe this bench):
#     BENCH_DIR=$HOME/frappe-bench   BENCH_USER=$(id -un)   BENCH_BIN=$HOME/.local/bin/bench
#     WEB_PORT=8000   GUNICORN_WORKERS=2   REDIS_BIN=/usr/bin/redis-server   NODE_BIN=/usr/bin/node
#
# Reading the result changes nothing. Installing it (see docs/WORKERS.md) is a separate, deliberate step.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/systemd/generated}"
BENCH_DIR="${BENCH_DIR:-$HOME/frappe-bench}"
BENCH_USER="${BENCH_USER:-$(id -un)}"
BENCH_BIN="${BENCH_BIN:-$HOME/.local/bin/bench}"
WEB_PORT="${WEB_PORT:-8000}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"
REDIS_BIN="${REDIS_BIN:-/usr/bin/redis-server}"
NODE_BIN="${NODE_BIN:-/usr/bin/node}"
DEPLOY="$HERE"
LOGS="$BENCH_DIR/logs"
ENVPATH="$(dirname "$BENCH_BIN"):/usr/local/bin:/usr/bin:/bin"

mkdir -p "$OUT"
rm -f "$OUT"/ppc-bench*.service "$OUT"/ppc-bench*.timer "$OUT"/ppc-bench*.target

# One [Service] block every long-running bench process shares: always restarted, every stop is written to a log
# (reason, exit code), no start-rate limit (a Redis that is down for ten minutes must not park the workers for good).
common() {
	cat <<EOF
User=$BENCH_USER
Group=$BENCH_USER
WorkingDirectory=$BENCH_DIR
Environment=PATH=$ENVPATH
Restart=always
RestartSec=5
ExecStopPost=$DEPLOY/log_exit.sh %n \$SERVICE_RESULT \$EXIT_CODE \$EXIT_STATUS
EOF
}

unit_header() { # description, extra [Unit] lines...
	local desc="$1"; shift
	printf '[Unit]\nDescription=%s\nPartOf=ppc-bench.target\nStartLimitIntervalSec=0\n' "$desc"
	for l in "$@"; do printf '%s\n' "$l"; done
	printf '\n'
}

install_section() { printf '\n[Install]\nWantedBy=ppc-bench.target\n'; }

# ---- target: start / stop / restart the whole bench with one command
cat > "$OUT/ppc-bench.target" <<EOF
[Unit]
Description=PPC bench ($BENCH_DIR): redis, web, workers, scheduler
Wants=ppc-bench-redis-queue.service ppc-bench-redis-cache.service ppc-bench-web.service ppc-bench-worker-short.service ppc-bench-worker-default.service ppc-bench-worker-long.service ppc-bench-scheduler.service ppc-bench-socketio.service

[Install]
WantedBy=multi-user.target
EOF

# ---- Redis (queue: job data must never be evicted; cache: LRU). Config files are the bench's own (config/redis_*.conf).
for kind in queue cache; do
	{
		unit_header "Bench Redis ($kind)" "After=network.target"
		echo "[Service]"
		echo "Type=simple"
		common
		echo "ExecStart=$REDIS_BIN $BENCH_DIR/config/redis_$kind.conf"
		echo "ExecStartPost=$DEPLOY/wait_redis.sh $kind"
		echo "TimeoutStartSec=60"
		install_section
	} > "$OUT/ppc-bench-redis-$kind.service"
done

# ---- web (gunicorn, like `bench setup production`; two workers suit a 4 GB machine)
{
	unit_header "Bench web (gunicorn :$WEB_PORT)" "After=network.target mariadb.service ppc-bench-redis-cache.service ppc-bench-redis-queue.service" "Wants=ppc-bench-redis-cache.service ppc-bench-redis-queue.service"
	echo "[Service]"
	echo "Type=simple"
	common
	echo "ExecStart=$BENCH_DIR/env/bin/gunicorn -b 127.0.0.1:$WEB_PORT -w $GUNICORN_WORKERS -t 120 --max-requests 5000 --max-requests-jitter 500 --preload frappe.app:application"
	echo "WorkingDirectory=$BENCH_DIR/sites"
	echo "StandardOutput=append:$LOGS/web.log"
	echo "StandardError=append:$LOGS/web.error.log"
	echo "TimeoutStopSec=60"
	install_section
} > "$OUT/ppc-bench-web.service"

# ---- socketio (realtime; the review dialog also polls, so this only makes it faster)
{
	unit_header "Bench socketio (realtime)" "After=ppc-bench-redis-cache.service" "Wants=ppc-bench-redis-cache.service"
	echo "[Service]"
	echo "Type=simple"
	common
	echo "ExecStart=$NODE_BIN $BENCH_DIR/apps/frappe/socketio.js"
	echo "StandardOutput=append:$LOGS/socketio.log"
	echo "StandardError=append:$LOGS/socketio.error.log"
	install_section
} > "$OUT/ppc-bench-socketio.service"

# ---- scheduler (one process per bench)
{
	unit_header "Bench scheduler" "After=mariadb.service ppc-bench-redis-queue.service" "Wants=ppc-bench-redis-queue.service" "PartOf=ppc-bench-redis-queue.service"
	echo "[Service]"
	echo "Type=simple"
	common
	echo "ExecStart=$BENCH_BIN schedule"
	echo "StandardOutput=append:$LOGS/schedule.log"
	echo "StandardError=append:$LOGS/schedule.error.log"
	install_section
} > "$OUT/ppc-bench-scheduler.service"

# ---- workers: exactly the queue sets `bench setup supervisor` uses. A worker finishes the job it is running on a stop
# (KillMode=mixed: SIGTERM to the worker only, the job process is killed only after TimeoutStopSec). The queue guard
# (run by run_worker.sh) refuses to start a worker on a queue full of old jobs until a person has looked at them
# (exit 3 = do not retry).
worker() { # name queues stop_timeout
	local name="$1" queues="$2" stop="$3"
	{
		unit_header "Bench worker: $name ($queues)" "After=mariadb.service ppc-bench-redis-queue.service" "Wants=ppc-bench-redis-queue.service" "PartOf=ppc-bench-redis-queue.service"
		echo "[Service]"
		echo "Type=simple"
		common
		echo "ExecStart=$DEPLOY/run_worker.sh $BENCH_BIN $queues"
		echo "RestartPreventExitStatus=3"
		echo "KillMode=mixed"
		echo "TimeoutStopSec=$stop"
		echo "StandardOutput=append:$LOGS/worker-$name.log"
		echo "StandardError=append:$LOGS/worker-$name.error.log"
		install_section
	} > "$OUT/ppc-bench-worker-$name.service"
}
worker short   "short,default"        360
worker default "default"              360
worker long    "long,default,short"   1560

# ---- hourly recovery of MRP runs stuck in Queued, per site (instance name = site). Needed on a site whose scheduler is
# switched off (enable_scheduler = 0): this calls the very task the scheduler would call, and it is idempotent.
cat > "$OUT/ppc-bench-mrp-recovery@.service" <<EOF
[Unit]
Description=Recover PPC MRP runs stuck in Queued (site %I)
After=mariadb.service ppc-bench-redis-queue.service

[Service]
Type=oneshot
User=$BENCH_USER
Group=$BENCH_USER
WorkingDirectory=$BENCH_DIR
Environment=PATH=$ENVPATH
ExecStart=$BENCH_BIN --site %I execute ppc_production_control.tasks.recover_stuck_mrp_runs
StandardOutput=append:$LOGS/mrp-recovery.log
StandardError=append:$LOGS/mrp-recovery.log
TimeoutStartSec=600
EOF
cat > "$OUT/ppc-bench-mrp-recovery@.timer" <<EOF
[Unit]
Description=Hourly recovery of PPC MRP runs stuck in Queued (site %I)

[Timer]
OnCalendar=hourly
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ---- health check every 5 minutes (read-only; a failing check marks the unit failed and writes logs/health.log)
cat > "$OUT/ppc-bench-health.service" <<EOF
[Unit]
Description=Bench health check (redis, workers, queue age)

[Service]
Type=oneshot
User=$BENCH_USER
Group=$BENCH_USER
WorkingDirectory=$BENCH_DIR
Environment=PATH=$ENVPATH
ExecStart=$DEPLOY/healthcheck.sh
EOF
cat > "$OUT/ppc-bench-health.timer" <<EOF
[Unit]
Description=Bench health check every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

echo "Generated in $OUT:"; ls -1 "$OUT"
