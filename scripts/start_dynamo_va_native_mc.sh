#!/usr/bin/env bash
# Dynamo native on Ascend: frontend + dynamo.vllm BOTH inside container vllm-ascend-wm (0.26).
# P/D disaggregated only. Integrated with Mooncake via MultiConnector:
#   MooncakeConnectorV1  : P/D KV transfer via Transfer Engine
#   AscendStoreConnector : prefix cache node (mooncake store backend;
#                          pool master = mooncake_master started by this script)
#
# Prereqs:
#   - bash scripts/ascend/start_docker_va.sh
#   - bash scripts/ascend/start_etcd.sh   # skip if DISCOVERY=file
#   - host-built runtime under $WM_ROOT/dynamo-ascend (see bringup doc)
#   - model under /data/models/... (mounted via /data)
#   - mooncake_master binary available in container PATH
#
# Mooncake config (mooncake_config.json) is auto-generated inside the container
# at $MOONCAKE_CONFIG_PATH; no pre-made config file is needed. Tune via:
#   MOONCAKE_RPC_PORT / MC_MASTER_ADDRESS / MC_GLOBAL_SEGMENT_SIZE / MC_LOCAL_BUFFER_SIZE
#
# Usage (from host):
#   bash scripts/ascend/start_dynamo_va_native.sh
#   RESTART=1 bash scripts/ascend/start_dynamo_va_native.sh
#   DISCOVERY=file bash scripts/ascend/start_dynamo_va_native.sh
#   ETCD_ENDPOINTS=http://10.x.x.x:2379 bash scripts/ascend/start_dynamo_va_native.sh
#   WM_ROOT=/data/wm bash scripts/ascend/start_dynamo_va_native.sh
#
# P/D layout (8 NPU defaults, override for other shapes):
#   P_NPU / P_TP / P_DP : prefill worker NPUs / TP / DP  (default 0,1,2,3 / 4 / 1)
#   D_NPU / D_TP / D_DP : decode  worker NPUs / TP / DP  (default 4,5,6,7 / 4 / 1)
#   NPU count in P_NPU must equal P_TP*P_DP (same for decode).
#
# Verify:
#   curl -s localhost:8000/v1/models
#
# Logs: $WM_ROOT/dynamo-native-logs/{frontend,worker_prefill,worker_decode,mooncake}.log
# Stop:  bash scripts/ascend/start_dynamo_va_native.sh stop
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WM_ROOT=${WM_ROOT:-/data/wm}

NAME=${NAME:-vllm-ascend-wm}
PORT=${PORT:-8000}
MODEL=${MODEL:-/data/models/Qwen3.8-27B}
SERVED_NAME=${SERVED_NAME:-qwen}
# P/D layout
P_NPU=${P_NPU:-0,1,2,3}
P_TP=${P_TP:-4}
P_DP=${P_DP:-1}
D_NPU=${D_NPU:-4,5,6,7}
D_TP=${D_TP:-4}
D_DP=${D_DP:-1}
DISCOVERY=${DISCOVERY:-etcd}   # etcd | file
ETCD_ENDPOINTS=${ETCD_ENDPOINTS:-http://127.0.0.1:2379}
STORE=${STORE:-$WM_ROOT/dynamo_store_kv}
LOGDIR=${LOGDIR:-$WM_ROOT/dynamo-native-logs}
SITE_PACKAGES=/usr/local/python3.12.13/lib/python3.12/site-packages
RUNTIME_SRC=${RUNTIME_SRC:-$WM_ROOT/dynamo-ascend/lib/bindings/python/src}
COMPONENTS_SRC=${COMPONENTS_SRC:-$WM_ROOT/dynamo-ascend/components/src}
RESTART=${RESTART:-0}

# Mooncake Configuration
MOONCAKE_RPC_PORT=${MOONCAKE_RPC_PORT:-50058}
MOONCAKE_CONFIG_PATH=${MOONCAKE_CONFIG_PATH:-$WM_ROOT/mooncake_conf/mooncake_config.json}
MC_MASTER_ADDRESS=${MC_MASTER_ADDRESS:-127.0.0.1}
MC_GLOBAL_SEGMENT_SIZE=${MC_GLOBAL_SEGMENT_SIZE:-4294967296}
MC_LOCAL_BUFFER_SIZE=${MC_LOCAL_BUFFER_SIZE:-4294967296}
# vLLM-Ascend kv_pool sample: PYTHONHASHSEED=0 + mooncake libs on LD_LIBRARY_PATH
export PYTHONHASHSEED=0
MC_LIB_DIR=${MC_LIB_DIR:-/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/mooncake}
KV_PORT_PREFILL=${KV_PORT_PREFILL:-20001}
KV_PORT_DECODE=${KV_PORT_DECODE:-20002}

ensure_container() {
  if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "container $NAME not running; starting via start_docker_va.sh"
    bash "$SCRIPT_DIR/start_docker_va.sh"
  fi
}

ensure_etcd() {
  if [ "$DISCOVERY" = "file" ]; then
    return 0
  fi
  bash "$SCRIPT_DIR/start_etcd.sh"
}

ensure_pth() {
  docker exec "$NAME" bash -lc "
    set -e
    sp='$SITE_PACKAGES'
    echo '$RUNTIME_SRC' > \"\$sp/dynamo_ascend_runtime.pth\"
    echo '$COMPONENTS_SRC' > \"\$sp/dynamo_ascend_components.pth\"
    python3 -c 'import dynamo._core, dynamo.vllm; print(\"dynamo ok\", dynamo._core.__file__)'
  "
}

stop_inside() {
  docker exec "$NAME" bash -lc '
    set +e
    pkill -f "python3 -m dynamo.frontend" 2>/dev/null || true
    pkill -f "python3 -m dynamo.vllm" 2>/dev/null || true
    pkill -f "mooncake_master" 2>/dev/null || true
    # orphans keep holding NPU after parent dies
    pkill -9 -f "VLLM::" 2>/dev/null || true
    sleep 2
    pgrep -af "dynamo.frontend|dynamo.vllm|VLLM::|mooncake" | grep -v pgrep || echo "stopped"
  ' || true
}

if [ "${1:-}" = "stop" ]; then
  ensure_container
  stop_inside
  exit 0
fi

if [ "$DISCOVERY" != "etcd" ] && [ "$DISCOVERY" != "file" ]; then
  echo "DISCOVERY must be etcd or file, got: $DISCOVERY" >&2
  exit 2
fi

p_n=$(echo "$P_NPU" | tr ',' '\n' | wc -l)
d_n=$(echo "$D_NPU" | tr ',' '\n' | wc -l)
if [ "$p_n" -ne $((P_TP * P_DP)) ] || [ "$d_n" -ne $((D_TP * D_DP)) ]; then
  echo "P/D NPU list length mismatch: P_NPU($p_n) != P_TP*P_DP($((P_TP*P_DP))) or D_NPU($d_n) != D_TP*D_DP($((D_TP*D_DP)))" >&2
  exit 2
fi

ensure_container
ensure_etcd
ensure_pth

if [ "$RESTART" = "1" ]; then
  stop_inside
fi

mkdir -p "$STORE" "$LOGDIR"

# Generate mooncake_config.json inside the container so this script is self-contained.
MC_CONF_DIR=$(dirname "$MOONCAKE_CONFIG_PATH")
docker exec "$NAME" bash -lc "
set -e
mkdir -p '$MC_CONF_DIR'
cat > '$MOONCAKE_CONFIG_PATH' <<'EOF'
{
  \"protocol\": \"ascend\",
  \"use_ascend_direct\": true,
  \"master_server_address\": \"$MC_MASTER_ADDRESS:$MOONCAKE_RPC_PORT\",
  \"global_segment_size\": $MC_GLOBAL_SEGMENT_SIZE,
  \"local_buffer_size\": $MC_LOCAL_BUFFER_SIZE,
  \"metadata_server\": \"\"
}
EOF
echo \"generated mooncake config at '$MOONCAKE_CONFIG_PATH':\"
cat '$MOONCAKE_CONFIG_PATH'
"

: > "$LOGDIR/frontend.log"
: > "$LOGDIR/worker_prefill.log"
: > "$LOGDIR/worker_decode.log"
: > "$LOGDIR/mooncake.log"

if [ ! -d "$STORE" ]; then
  sysctl -w fs.inotify.max_user_watches=524288 >/dev/null 2>&1 || true
fi

if [ "$DISCOVERY" = "etcd" ]; then
  DISCOVERY_ENV="export DYN_DISCOVERY_BACKEND=etcd ETCD_ENDPOINTS='$ETCD_ENDPOINTS'"
else
  DISCOVERY_ENV="export DYN_DISCOVERY_BACKEND=file DYN_FILE_KV='$STORE'"
fi

docker exec -d "$NAME" bash -lc "
set -e
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1

# --- Mooncake Integration Start ---
export MOONCAKE_CONFIG_PATH='$MOONCAKE_CONFIG_PATH'
export LD_LIBRARY_PATH='$MC_LIB_DIR':\$LD_LIBRARY_PATH

if ! ss -tlnp | grep -q \":$MOONCAKE_RPC_PORT \"; then
  echo \"[\$(date '+%Y-%m-%d %H:%M:%S')] Starting mooncake_master on port $MOONCAKE_RPC_PORT...\" >> '$LOGDIR/mooncake.log'
  nohup mooncake_master \
    --rpc_port $MOONCAKE_RPC_PORT \
    --eviction_high_watermark_ratio 0.9 \
    --rpc_thread_num 32 \
    >> '$LOGDIR/mooncake.log' 2>&1 &
  MC_PID=\$!
  echo \$MC_PID > '$LOGDIR/mooncake.pid'
  sleep 2
  if kill -0 \$MC_PID 2>/dev/null; then
    echo \"[\$(date '+%Y-%m-%d %H:%M:%S')] mooncake_master started successfully (PID: \$MC_PID)\" >> '$LOGDIR/mooncake.log'
  else
    echo \"[\$(date '+%Y-%m-%d %H:%M:%S')] ERROR: mooncake_master failed to start, check log above\" >> '$LOGDIR/mooncake.log'
    exit 1
  fi
else
  EXISTING_PID=\$(ss -tlnp | grep \":$MOONCAKE_RPC_PORT \" | grep -oP 'pid=\\K[0-9]+' | head -1)
  echo \"[\$(date '+%Y-%m-%d %H:%M:%S')] mooncake_master already running on port $MOONCAKE_RPC_PORT (PID: \$EXISTING_PID), skipping startup\" >> '$LOGDIR/mooncake.log'
fi
# --- Mooncake Integration End ---

$DISCOVERY_ENV
cd /tmp

nohup python3 -m dynamo.frontend \
  --http-port $PORT \
  --discovery-backend $DISCOVERY \
  --request-plane tcp \
  --response-plane tcp \
  > '$LOGDIR/frontend.log' 2>&1 &
echo \$! > '$LOGDIR/frontend.pid'

sleep 2

# P/D disaggregated: prefill worker first (it registers as component 'prefill'),
# then decode worker (component 'backend'); frontend routes prefill->decode.
export ASCEND_RT_VISIBLE_DEVICES='$P_NPU'
nohup python3 -m dynamo.vllm \
  --model '$MODEL' \
  --served-model-name '$SERVED_NAME' \
  --tensor-parallel-size $P_TP \
  --data-parallel-size $P_DP \
  --discovery-backend $DISCOVERY \
  --request-plane tcp \
  --response-plane tcp \
  --disaggregation-mode prefill \
  --trust-remote-code \
  --gpu-memory-utilization 0.9 \
  --max-model-len 32768 \
  --max-num-seqs 64 \
  --enable-prefix-caching \
  --dyn-tool-call-parser qwen3_coder \
  --kv-transfer-config '{\"kv_connector\": \"MultiConnector\", \"kv_role\": \"kv_producer\", \"kv_connector_extra_config\": {\"connectors\": [{\"kv_connector\": \"MooncakeConnectorV1\", \"kv_role\": \"kv_producer\", \"kv_port\": \"$KV_PORT_PREFILL\", \"kv_connector_extra_config\": {\"prefill\": {\"dp_size\": $P_DP, \"tp_size\": $P_TP}, \"decode\": {\"dp_size\": $D_DP, \"tp_size\": $D_TP}}}, {\"kv_connector\": \"AscendStoreConnector\", \"kv_role\": \"kv_producer\", \"kv_connector_extra_config\": {\"backend\": \"mooncake\", \"lookup_rpc_port\": \"0\"}}]}}' \
  > '$LOGDIR/worker_prefill.log' 2>&1 &
echo \$! > '$LOGDIR/worker_prefill.pid'

sleep 5

export ASCEND_RT_VISIBLE_DEVICES='$D_NPU'
nohup python3 -m dynamo.vllm \
  --model '$MODEL' \
  --served-model-name '$SERVED_NAME' \
  --tensor-parallel-size $D_TP \
  --data-parallel-size $D_DP \
  --discovery-backend $DISCOVERY \
  --request-plane tcp \
  --response-plane tcp \
  --disaggregation-mode decode \
  --trust-remote-code \
  --gpu-memory-utilization 0.9 \
  --max-model-len 32768 \
  --max-num-seqs 64 \
  --enable-prefix-caching \
  --dyn-tool-call-parser qwen3_coder \
  --kv-transfer-config '{\"kv_connector\": \"MultiConnector\", \"kv_role\": \"kv_consumer\", \"kv_connector_extra_config\": {\"connectors\": [{\"kv_connector\": \"MooncakeConnectorV1\", \"kv_role\": \"kv_consumer\", \"kv_port\": \"$KV_PORT_DECODE\", \"kv_connector_extra_config\": {\"prefill\": {\"dp_size\": $P_DP, \"tp_size\": $P_TP}, \"decode\": {\"dp_size\": $D_DP, \"tp_size\": $D_TP}}}, {\"kv_connector\": \"AscendStoreConnector\", \"kv_role\": \"kv_consumer\", \"kv_connector_extra_config\": {\"backend\": \"mooncake\", \"lookup_rpc_port\": \"1\"}}]}}' \
  > '$LOGDIR/worker_decode.log' 2>&1 &
echo \$! > '$LOGDIR/worker_decode.pid'
"

echo "started inside $NAME (FE :$PORT, MODE=pd, prefill TP${P_TP}xDP${P_DP} on [$P_NPU], decode TP${D_TP}xDP${D_DP} on [$D_NPU], model=$SERVED_NAME, discovery=$DISCOVERY, mooncake=$MOONCAKE_RPC_PORT)"
if [ "$DISCOVERY" = "etcd" ]; then
  echo "ETCD_ENDPOINTS=$ETCD_ENDPOINTS"
fi
echo "waiting for model registry (worker load can take several minutes)..."

ok=0
for i in $(seq 1 180); do
  if curl -sf "localhost:$PORT/v1/models" 2>/dev/null | grep -q "\"$SERVED_NAME\""; then
    ok=1
    break
  fi
  if ! curl -sf "localhost:$PORT/health" >/dev/null 2>&1 && ! curl -sf "localhost:$PORT/v1/models" >/dev/null 2>&1; then
    if [ "$i" -ge 6 ]; then
      echo "frontend not responding; last frontend log:"
      tail -40 "$LOGDIR/frontend.log" || true
      exit 1
    fi
  fi
  sleep 5
done

if [ "$ok" = "1" ]; then
  curl -s "localhost:$PORT/v1/models"; echo
  echo "OK  frontend.pid=$(cat "$LOGDIR/frontend.pid" 2>/dev/null) prefill.pid=$(cat "$LOGDIR/worker_prefill.pid" 2>/dev/null) decode.pid=$(cat "$LOGDIR/worker_decode.pid" 2>/dev/null) mooncake.pid=$(cat "$LOGDIR/mooncake.pid" 2>/dev/null)"
  echo "logs: $LOGDIR/"
else
  echo "TIMEOUT waiting for $SERVED_NAME on :$PORT"
  docker exec "$NAME" bash -lc "pgrep -af 'dynamo.frontend|dynamo.vllm' | grep -v pgrep || true"
  tail -40 "$LOGDIR/frontend.log" || true
  echo "--- prefill log ---"
  tail -40 "$LOGDIR/worker_prefill.log" || true
  echo "--- decode log ---"
  tail -40 "$LOGDIR/worker_decode.log" || true
  echo "--- mooncake log ---"
  tail -40 "$LOGDIR/mooncake.log" || true
  exit 1
fi
