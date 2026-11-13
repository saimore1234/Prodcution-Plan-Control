#!/usr/bin/env bash
# ExecStart of the worker units: run the queue guard, and only when it allows, become `bench worker`.
# The guard is part of the MAIN process on purpose: its exit status 3 ("old jobs wait, a person must look") is then covered by
# RestartPreventExitStatus=3, so a refused worker stays stopped instead of being restarted every few seconds.
#   usage: run_worker.sh <bench-binary> <queues>      e.g. run_worker.sh ~/.local/bin/bench short,default
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/queue_guard.sh" || exit $?
exec "$1" worker --queue "$2"
