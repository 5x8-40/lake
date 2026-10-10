#!/usr/bin/env bash
# Start mooncake_master for vllm-ascend AscendStoreConnector.
# Docs: https://docs.vllm.ai/projects/ascend/en/v0.26.0rc1/user_guide/feature_guide/kv_pool.html
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
LOGDIR=${LOGDIR:-$ROOT/logs}
mkdir -p "$LOGDIR"

MC_PORT=${MC_PORT:-50088}
MC_BIN=${MC_BIN:-mooncake_master}

if pgrep -f "$MC_BIN.*${MC_PORT}" >/dev/null 2>&1; then
  echo "[master] already running on :$MC_PORT"
  exit 0
fi

# Recent mooncake_master prefers --rpc_port; --port is deprecated alias.
PORT_FLAG=--rpc_port
if ! "$MC_BIN" --help 2>&1 | grep -q -- '--rpc_port'; then
  PORT_FLAG=--port
fi

echo "[master] starting $MC_BIN $PORT_FLAG $MC_PORT"
nohup "$MC_BIN" \
  "$PORT_FLAG" "$MC_PORT" \
  --eviction_high_watermark_ratio 0.9 \
  --eviction_ratio 0.1 \
  --default_kv_lease_ttl 11000 \
  --enable_offload=false \
  --client_ttl=120 \
  >>"$LOGDIR/mooncake_master.log" 2>&1 &
echo $! >"$LOGDIR/mooncake_master.pid"
sleep 1
echo "[master] pid=$(cat "$LOGDIR/mooncake_master.pid") log=$LOGDIR/mooncake_master.log"
