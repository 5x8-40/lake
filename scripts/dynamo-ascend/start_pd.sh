#!/usr/bin/env bash
# Single-host Ascend PD: FE + Prefill + Decode inside the vllm-ascend container.
# Source: 5x8-40/dynamo-ascend (MooncakeConnectorV1 protocol lives there, not a lake patch).
# MultiConnector children (parallel, not a tiered hierarchy — see docs/pd-mooncake.md):
#   MooncakeConnectorV1  — P↔D KV transfer
#   AscendStoreConnector — prefix Store (requires mooncake_master)
#   AscendSimpleCPUOffloadConnector — NPU→CPU offload (ENABLE_KV_OFFLOAD=1 default)
# Default ROUTER_MODE=kv with kv-events ZMQ (ROUTER_MODE=round-robin to disable).
#
# Prereqs: prepare_src.sh → start_docker.sh → build_install.sh → start_etcd.sh
#   RESTART=1 bash scripts/dynamo-ascend/start_pd.sh
#   bash scripts/dynamo-ascend/start_pd.sh stop
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAKE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

NAME=${NAME:-vllm-ascend-lake-test}
PORT=${PORT:-8000}
MODEL=${MODEL:-/data/models/Qwen3.8-27B}
SERVED_NAME=${SERVED_NAME:-qwen}
P_NPU=${P_NPU:-0,1,2,3}
P_TP=${P_TP:-4}
P_DP=${P_DP:-1}
D_NPU=${D_NPU:-4,5,6,7}
D_TP=${D_TP:-4}
D_DP=${D_DP:-1}
DISCOVERY=${DISCOVERY:-etcd}
ETCD_ENDPOINTS=${ETCD_ENDPOINTS:-http://127.0.0.1:2379}
STORE=${STORE:-$SCRIPT_DIR/store_kv}
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
RESTART=${RESTART:-0}

MOONCAKE_RPC_PORT=${MOONCAKE_RPC_PORT:-50058}
MOONCAKE_CONFIG_PATH=${MOONCAKE_CONFIG_PATH:-$SCRIPT_DIR/mooncake_conf/mooncake_config.json}
MC_MASTER_ADDRESS=${MC_MASTER_ADDRESS:-127.0.0.1}
MC_GLOBAL_SEGMENT_SIZE=${MC_GLOBAL_SEGMENT_SIZE:-4294967296}
MC_LOCAL_BUFFER_SIZE=${MC_LOCAL_BUFFER_SIZE:-4294967296}
export PYTHONHASHSEED=0
MC_LIB_DIR=${MC_LIB_DIR:-/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/mooncake}
KV_PORT_PREFILL=${KV_PORT_PREFILL:-20001}
KV_PORT_DECODE=${KV_PORT_DECODE:-20002}

# MultiConnector children:
#   1) MooncakeConnectorV1 — P↔D KV transfer (only PD-capable child)
#   2) AscendStoreConnector — prefix Store; requires mooncake_master (started below)
#   3) OffloadingConnector — NPU→CPU KV offload (optional; ENABLE_KV_OFFLOAD=1)
ENABLE_KV_OFFLOAD=${ENABLE_KV_OFFLOAD:-1}
# Image-matched path: AscendSimpleCPUOffloadConnector (ships in v0.26.0rc1).
# Newer AscendOffloadingConnector+NPUOffloadingSpec needs newer vllm pairing.
OFFLOAD_CPU_BYTES=${OFFLOAD_CPU_BYTES:-8589934592}
OFFLOAD_CONNECTOR=${OFFLOAD_CONNECTOR:-AscendSimpleCPUOffloadConnector}
OFFLOAD_CONNECTOR_MODULE=${OFFLOAD_CONNECTOR_MODULE:-vllm_ascend.distributed.kv_transfer.kv_pool.simple_cpu_offload.simple_cpu_offload_connector}

ROUTER_MODE=${ROUTER_MODE:-kv}
P_SYSTEM_PORT=${P_SYSTEM_PORT:-8782}
D_SYSTEM_PORT=${D_SYSTEM_PORT:-8783}
P_KV_EVENT_PORT=${P_KV_EVENT_PORT:-20082}
D_KV_EVENT_PORT=${D_KV_EVENT_PORT:-20083}
P_KV_EVENTS_CONFIG=${P_KV_EVENTS_CONFIG:-"{\"publisher\":\"zmq\",\"topic\":\"kv-events\",\"endpoint\":\"tcp://*:${P_KV_EVENT_PORT}\",\"enable_kv_cache_events\":true}"}
D_KV_EVENTS_CONFIG=${D_KV_EVENTS_CONFIG:-"{\"publisher\":\"zmq\",\"topic\":\"kv-events\",\"endpoint\":\"tcp://*:${D_KV_EVENT_PORT}\",\"enable_kv_cache_events\":true}"}

# role=kv_producer|kv_consumer  kv_port=...  lookup_id=...
# lookup_id feeds AscendStoreConnector.lookup_rpc_port — engine field name is
# historical; value is an IPC path suffix (lookup_rpc_port_{id}_dp_rank), NOT a TCP port.
# Prefill uses 0, Decode uses 1 so local lookup endpoints do not collide.
kv_transfer_config_json() {
  local role=$1 kv_port=$2 lookup_id=$3
  local offload=""
  if [[ "${ENABLE_KV_OFFLOAD}" == "1" ]]; then
    offload=",{\"kv_connector\":\"${OFFLOAD_CONNECTOR}\",\"kv_connector_module_path\":\"${OFFLOAD_CONNECTOR_MODULE}\",\"kv_role\":\"kv_both\",\"kv_connector_extra_config\":{\"cpu_bytes_to_use\":${OFFLOAD_CPU_BYTES}}}"
  fi
  printf '{"kv_connector":"MultiConnector","kv_role":"%s","kv_connector_extra_config":{"connectors":[{"kv_connector":"MooncakeConnectorV1","kv_role":"%s","kv_port":"%s","kv_connector_extra_config":{"prefill":{"dp_size":%s,"tp_size":%s},"decode":{"dp_size":%s,"tp_size":%s}}},{"kv_connector":"AscendStoreConnector","kv_role":"%s","kv_connector_extra_config":{"backend":"mooncake","lookup_rpc_port":"%s"}}%s]}}' \
    "$role" "$role" "$kv_port" "$P_DP" "$P_TP" "$D_DP" "$D_TP" "$role" "$lookup_id" "$offload"
}

ensure_container() {
  if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "container $NAME not running; starting via start_docker.sh"
    bash "$SCRIPT_DIR/start_docker.sh"
  fi
}

ensure_etcd() {
  [[ "$DISCOVERY" == "file" ]] && return 0
  bash "$SCRIPT_DIR/start_etcd.sh"
}

stop_inside() {
  docker exec -i "$NAME" /bin/bash <<'EOS' || true
set +e
python3 - <<'PY'
import os, signal, time
needles = ("dynamo.frontend", "dynamo.vllm", "mooncake_master", "VLLM::", "VLLMWorker", "EngineCor")
me, parent = os.getpid(), os.getppid()
killed = []
for _ in range(3):
    for pid in list(os.listdir("/proc")):
        if not pid.isdigit():
            continue
        ipid = int(pid)
        if ipid in (me, parent):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        if any(n in cmd for n in needles):
            try:
                os.kill(ipid, signal.SIGKILL)
                killed.append(ipid)
            except Exception:
                pass
    time.sleep(2)
print(f"killed {len(set(killed))}: {sorted(set(killed))}")
PY
EOS
}

if [[ "${1:-}" == "stop" ]]; then
  ensure_container
  stop_inside
  exit 0
fi

[[ "$DISCOVERY" == "etcd" || "$DISCOVERY" == "file" ]] || {
  echo "DISCOVERY must be etcd or file" >&2; exit 2
}
p_n=$(echo "$P_NPU" | tr ',' '\n' | wc -l)
d_n=$(echo "$D_NPU" | tr ',' '\n' | wc -l)
if [[ "$p_n" -ne $((P_TP * P_DP)) || "$d_n" -ne $((D_TP * D_DP)) ]]; then
  echo "NPU count mismatch vs TP*DP" >&2; exit 2
fi

ensure_container
ensure_etcd
# Assert protocol is visible from the installed dynamo-ascend tree
SRC=${SRC:-$LAKE_ROOT/3rdparty/dynamo-ascend} bash "$SCRIPT_DIR/verify_protocol.sh"

if [[ "$RESTART" == "1" ]]; then
  stop_inside
fi

mkdir -p "$STORE" "$LOGDIR"
MC_CONF_DIR=$(dirname "$MOONCAKE_CONFIG_PATH")
docker exec "$NAME" bash -lc "
set -e
mkdir -p '$MC_CONF_DIR'
cat > '$MOONCAKE_CONFIG_PATH' <<EOF
{
  \"protocol\": \"ascend\",
  \"use_ascend_direct\": true,
  \"master_server_address\": \"$MC_MASTER_ADDRESS:$MOONCAKE_RPC_PORT\",
  \"global_segment_size\": $MC_GLOBAL_SEGMENT_SIZE,
  \"local_buffer_size\": $MC_LOCAL_BUFFER_SIZE,
  \"metadata_server\": \"P2PHANDSHAKE\",
  \"preferred_segment\": false,
  \"prefer_alloc_in_same_node\": true
}
EOF
"

: >"$LOGDIR/frontend.log"
: >"$LOGDIR/worker_prefill.log"
: >"$LOGDIR/worker_decode.log"
: >"$LOGDIR/mooncake.log"

if [[ "$DISCOVERY" == "etcd" ]]; then
  DISCOVERY_ENV="export DYN_DISCOVERY_BACKEND=etcd ETCD_ENDPOINTS='$ETCD_ENDPOINTS'"
else
  DISCOVERY_ENV="export DYN_DISCOVERY_BACKEND=file DYN_FILE_KV='$STORE'"
fi

PREFILL_KV_CFG=$(kv_transfer_config_json kv_producer "$KV_PORT_PREFILL" 0)   # lookup_id=0
DECODE_KV_CFG=$(kv_transfer_config_json kv_consumer "$KV_PORT_DECODE" 1)     # lookup_id=1

docker exec -d "$NAME" bash -lc "
set -e
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export PYTHONHASHSEED=0
export MOONCAKE_CONFIG_PATH='$MOONCAKE_CONFIG_PATH'
export LD_LIBRARY_PATH='$MC_LIB_DIR':\$LD_LIBRARY_PATH
$DISCOVERY_ENV
cd /tmp

port_open() {
  python3 - \"\$1\" <<'PY'
import socket, sys
p=int(sys.argv[1]); s=socket.socket(); s.settimeout(0.5)
try:
  s.connect(('127.0.0.1', p)); sys.exit(0)
except Exception:
  sys.exit(1)
finally:
  s.close()
PY
}
# AscendStoreConnector needs mooncake_master; starting them separately is useless.
if ! port_open $MOONCAKE_RPC_PORT; then
  nohup mooncake_master --rpc_port $MOONCAKE_RPC_PORT \
    --eviction_high_watermark_ratio 0.9 --rpc_thread_num 32 \
    >> '$LOGDIR/mooncake.log' 2>&1 &
  echo \$! > '$LOGDIR/mooncake.pid'
  sleep 2
fi

nohup python3 -m dynamo.frontend \
  --http-port $PORT \
  --discovery-backend $DISCOVERY \
  --request-plane tcp \
  --router-mode $ROUTER_MODE \
  > '$LOGDIR/frontend.log' 2>&1 &
echo \$! > '$LOGDIR/frontend.pid'
sleep 2

export ASCEND_RT_VISIBLE_DEVICES='$P_NPU'
export DYN_SYSTEM_PORT=$P_SYSTEM_PORT
nohup python3 -m dynamo.vllm \
  --model '$MODEL' --served-model-name '$SERVED_NAME' \
  --tensor-parallel-size $P_TP --data-parallel-size $P_DP \
  --discovery-backend $DISCOVERY --request-plane tcp \
  --disaggregation-mode prefill --trust-remote-code \
  --gpu-memory-utilization 0.9 --max-model-len 32768 --max-num-seqs 64 \
  --enable-prefix-caching --kv-events-config '$P_KV_EVENTS_CONFIG' \
  --kv-transfer-config '$PREFILL_KV_CFG' \
  > '$LOGDIR/worker_prefill.log' 2>&1 &
echo \$! > '$LOGDIR/worker_prefill.pid'
sleep 5

export ASCEND_RT_VISIBLE_DEVICES='$D_NPU'
export DYN_SYSTEM_PORT=$D_SYSTEM_PORT
nohup python3 -m dynamo.vllm \
  --model '$MODEL' --served-model-name '$SERVED_NAME' \
  --tensor-parallel-size $D_TP --data-parallel-size $D_DP \
  --discovery-backend $DISCOVERY --request-plane tcp \
  --disaggregation-mode decode --trust-remote-code \
  --gpu-memory-utilization 0.9 --max-model-len 32768 --max-num-seqs 64 \
  --enable-prefix-caching --kv-events-config '$D_KV_EVENTS_CONFIG' \
  --kv-transfer-config '$DECODE_KV_CFG' \
  > '$LOGDIR/worker_decode.log' 2>&1 &
echo \$! > '$LOGDIR/worker_decode.pid'
"

echo "started PD inside $NAME (FE :$PORT router=$ROUTER_MODE offload=$ENABLE_KV_OFFLOAD P=[$P_NPU] D=[$D_NPU])"
echo "waiting for model registry (workers may take several minutes)..."
ok=0
for i in $(seq 1 240); do
  if curl -sf "localhost:$PORT/v1/models" 2>/dev/null | grep -q "\"$SERVED_NAME\""; then
    ok=1; break
  fi
  # Only abort early if the frontend process disappeared.
  if [[ "$i" -ge 12 ]]; then
    if ! docker exec "$NAME" bash -lc 'pgrep -f "python3 -m dynamo.frontend" >/dev/null'; then
      echo "frontend process gone:"; tail -60 "$LOGDIR/frontend.log" || true
      exit 1
    fi
  fi
  sleep 5
done

if [[ "$ok" == "1" ]]; then
  curl -s "localhost:$PORT/v1/models"; echo
  echo "OK logs=$LOGDIR"
else
  echo "TIMEOUT"; tail -60 "$LOGDIR"/worker_*.log "$LOGDIR/frontend.log" || true
  exit 1
fi
