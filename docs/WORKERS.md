# Background workers, Redis and the scheduler

Automatic MRP is a background job: submitting a Sales Order only *queues* it. If nothing consumes the queue the review
dialog says **Waiting for the background worker** (and offers **Calculate Now**, which does the same calculation in the
browser's own request). This page explains how to keep the workers alive, what was found on this bench, and how to
install the supervision that is prepared in `deploy/`. **Nothing here is installed or started by the app.**

## What was found (read-only investigation, 2026-11-13)

| Finding | Evidence | Consequence |
|---|---|---|
| Nothing supervises the bench. Processes were started by hand / by `bench start` in a terminal. | no Supervisor, no unit files; `bench start` = foreground `honcho` that dies with its terminal or when the WSL VM restarts | after a terminal close or a WSL restart there are **0 workers**, 0 scheduler |
| Workers exit when Redis hiccups and nobody restarts them | `logs/worker.error.log`: `Worker ... Redis connection timeout, quitting` (2026-11-12 14:49 and 14:57) | the worker is gone until someone notices |
| `Procfile` starts **one** worker with no queue list | `worker: bench worker` | a long job blocks short ones; only one process |
| The scheduler of `dev.local` is **off** | `System Settings.enable_scheduler = 0`; `logs/scheduler.log` last written 2026-10-14 | no hourly task of any app runs there, including the new MRP recovery |
| Old jobs sit in the queue | `dev.local` queue (port 11000): 2 jobs queued 2026-11-12 11:27 / 11:46 (a File link clean-up and the website search index), 0 registered workers. The staging queue (11002) holds 176 jobs left by test runs | a worker started now would run them immediately |
| The queue Redis has no persistence setting | `redis_queue.conf` has no `save`/`appendonly`/`maxmemory-policy` | jobs can reappear after a restart from an old snapshot, or be lost; both must be harmless |
| One job crashed in `frappe.init` | `ModuleNotFoundError: No module named 'bundle_weight_calculation'` (2026-11-12 14:43, the app was briefly not importable) | a worker bench-wide fails every job while *any* app in `apps.txt` cannot be imported |
| `gunicorn_workers = 17` in `common_site_config.json` on a 4 GB machine | `free -m`: 3.8 GB total | memory pressure; the proposed web unit uses 2 |

The causes of "Redis and workers stop" are therefore (1) no supervisor, (2) workers quitting on a Redis timeout, (3) WSL
restarting. None is a defect of the MRP code, but a lost job must still not strand a run, which is why the hourly recovery
and the idempotent jobs exist.

## What the app does so a lost job costs nothing

* **Idempotent jobs.** An automatic run has a unique `idempotency_key` (`auto::<doctype>::<name>`); the job, "Calculate Now", the
  retry and the recovery all go through the same locked, re-checked step (`mrp_approval._process_once`): a run that is no longer
  Queued/Failed - or was cancelled - is never calculated again, so a late or duplicate job cannot overwrite a review.
* **Hourly recovery** `tasks.recover_stuck_mrp_runs` (scheduler event *hourly*): an Automatic run that is still **Queued** after
  30 minutes (`constants.MRP_STUCK_AFTER_MINUTES`) is calculated again by one queued recovery job; every attempt raises the run's
  retry count and is audited (*MRP Retry*, `recovered: true`). When *Automatic Retries* (PPC Settings) are used up the run is
  marked **Failed** with "Stuck in Queued ..." and the PPC Managers are notified, so it cannot loop. It never touches a cancelled
  review or a manual run, never creates documents itself, and does nothing while automatic MRP is off or the site is not migrated.
* **Because the scheduler of `dev.local` is off**, the same task can be run by a systemd timer
  (`ppc-bench-mrp-recovery@dev.local.timer`) without switching the scheduler on. Switching it on is a separate decision: it starts
  *every* app's hourly/daily jobs on that site (HR leave allocation, attendance processing, ...).

## The proposed supervision (`deploy/`)

The bench runs on **WSL2 with systemd enabled** (`/etc/wsl.conf`: `systemd=true`) and has no Supervisor, so systemd is used.
`deploy/generate_systemd.sh` writes the unit files (to `deploy/systemd/generated/` by default); it only writes files.

| Unit | Runs | Notes |
|---|---|---|
| `ppc-bench-redis-queue` | `redis-server config/redis_queue.conf` | start only when the Redis answers PING |
| `ppc-bench-redis-cache` | `redis-server config/redis_cache.conf` | |
| `ppc-bench-web` | gunicorn `:8000`, 2 workers | |
| `ppc-bench-socketio` | `apps/frappe/socketio.js` | realtime; the dialog polls without it |
| `ppc-bench-worker-short` | `bench worker --queue short,default` | same queue sets as `bench setup supervisor` |
| `ppc-bench-worker-default` | `bench worker --queue default` | |
| `ppc-bench-worker-long` | `bench worker --queue long,default,short` | stop timeout 26 min (finishes its job) |
| `ppc-bench-scheduler` | `bench schedule` | one per bench |
| `ppc-bench-mrp-recovery@<site>.timer` | hourly `bench --site <site> execute ppc_production_control.tasks.recover_stuck_mrp_runs` | for sites whose scheduler is off |
| `ppc-bench-health.timer` | every 5 minutes `deploy/healthcheck.sh` | read-only, writes `logs/health.log` |
| `ppc-bench.target` | groups all of the above | `systemctl restart ppc-bench.target` |

Behaviour: `Restart=always`, 5 s delay, no start-rate limit (a Redis that is down for ten minutes does not park the workers);
workers are `PartOf` the queue Redis, so restarting Redis restarts them; every stop is written to `logs/service-events.log`
(unit, result, exit code) and each unit has its own `logs/*.log` / `*.error.log`; systemd starts exactly one instance of a
unit, so there are **no duplicate workers** as long as nothing else starts one (see below).

### No accidental processing of old jobs

Each worker is started through `deploy/run_worker.sh`, which runs `deploy/queue_guard.sh` first. If a queue holds jobs older
than 6 hours (`PPC_STALE_HOURS`) the guard **refuses (exit 3, no restart loop)** and logs why to `logs/queue-guard.log`.

1. `deploy/inspect_queues.sh` - read-only list of every queue, the age and description of the jobs, the registered workers.
2. Decide: let them run, or have them removed by someone who knows what they are (the scripts never remove anything).
3. `deploy/queue_ack.sh` - valid for 30 minutes - and start the workers.

`PPC_QUEUE_GUARD=warn` only logs, `off` disables the guard. The recommended `deploy/redis_queue.recommended.conf` (no snapshots, no
append-only file, `noeviction`, keep-alive) makes a restarted queue Redis come back **empty**; that is safe because a lost
automatic job is recovered hourly and every PPC job is idempotent.

### Health check

`deploy/healthcheck.sh` (exit 1 = look) checks: queue and cache Redis answer; the number of registered workers is the expected 3
(fewer = one is missing, more = a duplicate); no queue is older than 6 hours or longer than 200 jobs; every enabled unit is active.

## Installing it (needs your approval - this changes services)

Do these only after reading the impact below. Nothing here was run.

    # 0. stop the hand-started processes (they would collide on ports 8000 / 11000 / 13000 and duplicate the workers)
    #    -> `bench start` terminal: Ctrl+C. Stale queue: run deploy/inspect_queues.sh first.
    cd ~/frappe-bench/apps/ppc_production_control/deploy
    ./generate_systemd.sh /tmp/ppc-units               # review the files
    sudo cp /tmp/ppc-units/ppc-bench* /etc/systemd/system/
    sudo systemctl daemon-reload
    # 1. optional: add deploy/redis_queue.recommended.conf to ~/frappe-bench/config/redis_queue.conf
    # 2. old jobs: inspect (deploy/inspect_queues.sh); then, only if they may run, deploy/queue_ack.sh
    sudo systemctl enable --now ppc-bench.target
    sudo systemctl enable --now ppc-bench-health.timer
    sudo systemctl enable --now ppc-bench-mrp-recovery@dev.local.timer      # one per site that uses MRP
    # check
    systemctl status 'ppc-bench-*' ; deploy/healthcheck.sh ; tail logs/service-events.log

Rollback: `sudo systemctl disable --now ppc-bench.target ppc-bench-health.timer 'ppc-bench-mrp-recovery@*.timer'`, delete the files
from `/etc/systemd/system/`, `daemon-reload`, and start `bench start` again.

**Impact to expect**

* Stopping `bench start` and starting the units **restarts web, Redis and the worker**: a short outage of `dev.local` (users see the site
  down for the seconds the web unit takes to start). The queue Redis restarts empty if the recommended config is applied - the 2 old
  jobs disappear with it; the guard makes this an explicit step instead of a surprise.
* With the scheduler **on** (`enable_scheduler`) every app starts its periodic jobs; with it off they do not. The timer gives MRP
  recovery without that.
* WSL2: services run only while the WSL VM runs. Windows can stop an idle VM; keep a session open or set `vmIdleTimeout=-1` in
  `%UserProfile%\.wslconfig` (then `wsl --shutdown` once). A real server does not have this limitation.
* `bench start` must never be used together with the units (it starts a second set of workers, Redis and web).

## Operating notes

* After deploying new app code: `sudo systemctl restart ppc-bench-worker-short ppc-bench-worker-default ppc-bench-worker-long`
  (a worker keeps the old code in memory until restarted).
* A job that fails inside Frappe: **Error Log** (Desk) and `logs/worker-*.error.log`. A run that fails: status *Failed*, reason on the
  run, audit row *MRP Calculation Failed*, notification to PPC Managers, automatic retries.
* `bench doctor` shows workers online and the jobs waiting per queue.
