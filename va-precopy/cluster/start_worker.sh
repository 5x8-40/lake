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
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
LOGDIR=${LOGDIR:-$ROOT/logs}
CONF_DIR=${CONF_DIR:-$ROOT/conf}
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
# Heterogeneous TP (AscendStore): peer side TP for tp_mismatch (non-MLA, non-layerwise).
# kv_consumer reads peer prefill_tp_size; kv_producer/both reads decode_tp_size.
PREFILL_TP_SIZE=${PREFILL_TP_SIZE:-}
DECODE_TP_SIZE=${DECODE_TP_SIZE:-}

cat >"$MOONCAKE_CONFIG_PATH" <<EOF
{
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "$PROTOCOL",
  "device_name": "",
  "master_server_address": "$MC_MASTER",
  "global_segment_size": "$GLOBAL_SEGMENT_SIZE",
  "local_buffer_size": "$LOCAL_BUFFER_SIZE",
  "preferred_segment": true,
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
# Optional hetero TP: peer TP size so both sides address pool at effective_tp=max(local,peer).
if [[ -n "$PREFILL_TP_SIZE" || -n "$DECODE_TP_SIZE" ]]; then
  KV_CONFIG=$(KV_CONFIG="$KV_CONFIG" PREFILL_TP_SIZE="$PREFILL_TP_SIZE" DECODE_TP_SIZE="$DECODE_TP_SIZE" python3 - <<'PY'
import json, os
cfg = json.loads(os.environ["KV_CONFIG"])
extra = cfg["kv_connector_extra_config"]
if os.environ.get("PREFILL_TP_SIZE"):
    extra["prefill_tp_size"] = int(os.environ["PREFILL_TP_SIZE"])
if os.environ.get("DECODE_TP_SIZE"):
    extra["decode_tp_size"] = int(os.environ["DECODE_TP_SIZE"])
if os.environ.get("LOAD_ASYNC"):
    extra["load_async"] = True
print(json.dumps(cfg))
PY
)
fi

# A2 RoCE: required for cross-card create_copy_task (HcclBatchPut). See env_ascend_a2.sh.
ENABLE_ASCEND_A2=${ENABLE_ASCEND_A2:-1}
if [[ "$ENABLE_ASCEND_A2" == "1" && "$PROTOCOL" == "ascend" ]]; then
  # shellcheck source=env_ascend_a2.sh
  source "$SCRIPT_DIR/env_ascend_a2.sh"
fi

export PYTHONHASHSEED=${PYTHONHASHSEED:-0}
export MOONCAKE_CONFIG_PATH
# Prefer installed mooncake package dir (vllm-ascend image); fall back to toolkit path.
if [[ -z "${MC_PY:-}" ]]; then
  MC_PY=$(python3 -c 'import mooncake, os; print(os.path.dirname(mooncake.__file__))' 2>/dev/null || true)
fi
MC_PY=${MC_PY:-/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/mooncake}
export LD_LIBRARY_PATH="${MC_PY}:${LD_LIBRARY_PATH:-}"
# Distinct HCCL socket ranges per role when multiple workers share one host.
if [[ -n "${HCCL_NPU_SOCKET_PORT_RANGE:-}" ]]; then
  export HCCL_NPU_SOCKET_PORT_RANGE
elif [[ "$ROLE" == "A" ]]; then
  export HCCL_NPU_SOCKET_PORT_RANGE=${HCCL_NPU_SOCKET_PORT_RANGE_A:-26000-26099}
else
  export HCCL_NPU_SOCKET_PORT_RANGE=${HCCL_NPU_SOCKET_PORT_RANGE_B:-26100-26199}
fi

if [[ "${TP}" != "1" ]]; then
  echo "[worker-$ROLE] WARN: TP=$TP not validated; TP>1 saw TRANSFER_FAIL / HcclBatchPut=4 on A2. Prefer TP=1." >&2
fi

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
echo "[worker-$ROLE] HCCL_INTRA_ROCE_ENABLE=${HCCL_INTRA_ROCE_ENABLE:-<unset>} HCCL_IF_IP=${HCCL_IF_IP:-<unset>}"
echo "[worker-$ROLE] TP=$TP DP=$DP kv_connector=AscendStoreConnector backend=mooncake lookup_id=$LOOKUP_ID"
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
echo "[worker-$ROLE] tip: rank<->seg 由 precopy/resolve.py 解析（master admin :9003 + pidfile/ss，不读日志）"
