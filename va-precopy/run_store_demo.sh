#!/usr/bin/env bash
# Store-only pre-copy demo (no vLLM).
# Stock Mooncake: PROTOCOL=tcp
# vllm-ascend image: PROTOCOL=ascend + free NPUs (default 4/5/6)
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
mkdir -p "$LOGDIR"

MC_PORT=${MC_PORT:-50088}
MC_MASTER=${MC_MASTER:-127.0.0.1:$MC_PORT}
PROTOCOL=${MOONCAKE_PROTOCOL:-${PROTOCOL:-tcp}}
SKIP_MASTER=${SKIP_MASTER:-0}

# Ascend Mooncake .so must precede other libs; set_env may already be sourced.
if [[ -z "${MC_PY:-}" ]]; then
  MC_PY=$(python3 -c 'import mooncake, os; print(os.path.dirname(mooncake.__file__))' 2>/dev/null || true)
fi
if [[ -n "${MC_PY}" ]]; then
  export LD_LIBRARY_PATH="${MC_PY}:${LD_LIBRARY_PATH:-}"
fi

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
    # Prefer rpc_port (port is deprecated on recent mooncake_master).
    if mooncake_master --help 2>&1 | grep -q -- '--rpc_port'; then
      nohup mooncake_master --rpc_port "$port" \
        --eviction_high_watermark_ratio 0.9 --rpc_thread_num 8 \
        >>"$LOGDIR/mooncake_master.log" 2>&1 &
      echo $! >"$LOGDIR/mooncake_master.pid"
      sleep 1
    else
      MC_PORT=$port bash "$SCRIPT_DIR/start_master.sh"
    fi
  else
    echo "[run] master already up at $MC_MASTER"
  fi
fi

if [[ "$PROTOCOL" == "ascend" ]]; then
  export SOURCE_DEVICE=${SOURCE_DEVICE:-4}
  export TARGET_DEVICE=${TARGET_DEVICE:-5}
  export COORD_DEVICE=${COORD_DEVICE:-6}
  export HOST_IP=${HOST_IP:-$(hostname -I | awk '{print $1}')}
  echo "[run] ascend mode HOST_IP=$HOST_IP devices=$SOURCE_DEVICE/$TARGET_DEVICE/$COORD_DEVICE"
fi

export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
exec python3 "$SCRIPT_DIR/store_demo.py" --master "$MC_MASTER" --protocol "$PROTOCOL"
