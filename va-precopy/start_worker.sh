#!/usr/bin/env bash
# Start one vllm-ascend OpenAI server with AscendStoreConnector + Mooncake only.
#
# Usage:
#   ROLE=A PORT=8001 LOOKUP_ID=0 ASCEND_RT_VISIBLE_DEVICES=0 bash start_worker.sh
#   ROLE=B PORT=8002 LOOKUP_ID=1 ASCEND_RT_VISIBLE_DEVICES=1 bash start_worker.sh
#
# DRY_RUN=1 prints the command and exits (no NPU required).
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
CONF_DIR=${CONF_DIR:-$SCRIPT_DIR/conf}
mkdir -p "$LOGDIR" "$CONF_DIR"

ROLE=${ROLE:-A}
PORT=${PORT:-8001}
LOOKUP_ID=${LOOKUP_ID:-0}
MODEL=${MODEL:-/data/models/Qwen2.5-7B-Instruct}
SERVED_NAME=${SERVED_NAME:-qwen}
TP=${TP:-1}
DP=${DP:-1}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-8192}
MC_PORT=${MC_PORT:-50088}
MC_MASTER=${MC_MASTER:-127.0.0.1:$MC_PORT}
MOONCAKE_CONFIG_PATH=${MOONCAKE_CONFIG_PATH:-$CONF_DIR/mooncake.${ROLE}.json}
PROTOCOL=${MOONCAKE_PROTOCOL:-ascend}
GLOBAL_SEGMENT_SIZE=${GLOBAL_SEGMENT_SIZE:-1GB}
LOCAL_BUFFER_SIZE=${LOCAL_BUFFER_SIZE:-1GB}
DRY_RUN=${DRY_RUN:-0}
USE_LAYERWISE=${USE_LAYERWISE:-false}

cat >"$MOONCAKE_CONFIG_PATH" <<EOF
{
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "$PROTOCOL",
  "device_name": "",
  "master_server_address": "$MC_MASTER",
  "global_segment_size": "$GLOBAL_SEGMENT_SIZE",
  "local_buffer_size": "$LOCAL_BUFFER_SIZE",
  "preferred_segment": false,
  "prefer_alloc_in_same_node": true,
  "enable_ssd_offload": false
}
EOF

KV_CONFIG=$(cat <<EOF
{
  "kv_connector": "AscendStoreConnector",
  "kv_role": "kv_both",
  "kv_connector_extra_config": {
    "backend": "mooncake",
    "lookup_rpc_port": "$LOOKUP_ID",
    "use_layerwise": $USE_LAYERWISE
  }
}
EOF
)

export PYTHONHASHSEED=${PYTHONHASHSEED:-0}
export MOONCAKE_CONFIG_PATH
export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/mooncake:${LD_LIBRARY_PATH:-}}

CMD=(
  python3 -m vllm.entrypoints.openai.api_server
  --model "$MODEL"
  --served-model-name "$SERVED_NAME"
  --trust-remote-code
  --enforce-eager
  --tensor-parallel-size "$TP"
  --data-parallel-size "$DP"
  --port "$PORT"
  --max-model-len "$MAX_MODEL_LEN"
  --gpu-memory-utilization 0.9
  --enable-prefix-caching
  --kv-transfer-config "$KV_CONFIG"
)

echo "[worker-$ROLE] MOONCAKE_CONFIG_PATH=$MOONCAKE_CONFIG_PATH"
echo "[worker-$ROLE] ASCEND_RT_VISIBLE_DEVICES=${ASCEND_RT_VISIBLE_DEVICES:-<unset>}"
echo "[worker-$ROLE] kv_connector=AscendStoreConnector backend=mooncake lookup_id=$LOOKUP_ID"
printf '[worker-%s] cmd:' "$ROLE"
printf ' %q' "${CMD[@]}"
printf '\n'

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[worker-$ROLE] DRY_RUN=1 - not starting"
  exit 0
fi

nohup "${CMD[@]}" >"$LOGDIR/worker_${ROLE}.log" 2>&1 &
echo $! >"$LOGDIR/worker_${ROLE}.pid"
echo "[worker-$ROLE] pid=$(cat "$LOGDIR/worker_${ROLE}.pid") log=$LOGDIR/worker_${ROLE}.log"
echo "[worker-$ROLE] tip: find local_seg in Mooncake setup logs for precopy.py --target"
