#!/usr/bin/env bash
# Store-only pre-copy demo (no vLLM, no NPU). Needs mooncake wheel + master.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
mkdir -p "$LOGDIR"

MC_PORT=${MC_PORT:-50088}
MC_MASTER=${MC_MASTER:-127.0.0.1:$MC_PORT}
PROTOCOL=${MOONCAKE_PROTOCOL:-${PROTOCOL:-tcp}}
SKIP_MASTER=${SKIP_MASTER:-0}

port_open() {
  python3 - "$1" "$2" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(); s.settimeout(0.5)
try:
    s.connect((host, port)); raise SystemExit(0)
except Exception:
    raise SystemExit(1)
finally:
    s.close()
PY
}

host=${MC_MASTER%:*}
port=${MC_MASTER##*:}

if [[ "$SKIP_MASTER" != "1" ]]; then
  if ! port_open "$host" "$port"; then
    echo "[run] starting master on :$port"
    if mooncake_master --help 2>&1 | grep -q -- '--port'; then
      MC_PORT=$port bash "$SCRIPT_DIR/start_master.sh"
    else
      nohup mooncake_master --rpc_port "$port" \
        --eviction_high_watermark_ratio 0.9 --rpc_thread_num 8 \
        >>"$LOGDIR/mooncake_master.log" 2>&1 &
      echo $! >"$LOGDIR/mooncake_master.pid"
      sleep 1
    fi
  else
    echo "[run] master already up at $MC_MASTER"
  fi
fi

export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
exec python3 "$SCRIPT_DIR/store_demo.py" --master "$MC_MASTER" --protocol "$PROTOCOL"
