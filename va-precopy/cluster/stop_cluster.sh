#!/usr/bin/env bash
# Stop va-precopy workers (and optionally mooncake_master).
#
#   bash stop_cluster.sh              # workers only
#   STOP_MASTER=1 bash stop_cluster.sh
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
LOGDIR=${LOGDIR:-$ROOT/logs}
STOP_MASTER=${STOP_MASTER:-0}

_kill_pidfile() {
  local name=$1 pidfile=$2
  if [[ ! -f "$pidfile" ]]; then
    echo "[stop] $name: no pidfile"
    return 0
  fi
  local pid
  pid=$(cat "$pidfile" 2>/dev/null || true)
  if [[ -z "${pid:-}" ]]; then
    echo "[stop] $name: empty pidfile"
    rm -f "$pidfile"
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    echo "[stop] $name pid=$pid"
    # Children (EngineCore) first when possible.
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "[stop] $name still alive, SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
      pkill -9 -P "$pid" 2>/dev/null || true
    fi
  else
    echo "[stop] $name pid=$pid already gone"
  fi
  rm -f "$pidfile"
}

_kill_pidfile worker-A "$LOGDIR/worker_A.pid"
_kill_pidfile worker-B "$LOGDIR/worker_B.pid"

if [[ "$STOP_MASTER" == "1" ]]; then
  _kill_pidfile mooncake_master "$LOGDIR/mooncake_master.pid"
fi

echo "[stop] done"
