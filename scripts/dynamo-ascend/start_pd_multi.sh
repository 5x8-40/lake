#!/usr/bin/env bash
# Cross-host Ascend PD + Mooncake on release/1.4.2 + protocol patch.
# ROLE=p : etcd + mooncake_master + frontend + N× prefill
# ROLE=d : N× decode (points at remote etcd / mooncake master)
#
#   export HOST_IP=... PEER_IP=... ETCD_ENDPOINTS=http://$HOST_IP:2379 MC_MASTER_ADDRESS=$HOST_IP
#   RESTART=1 ROLE=p bash scripts/dynamo-ascend/start_pd_multi.sh
#   RESTART=1 ROLE=d bash scripts/dynamo-ascend/start_pd_multi.sh
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WM_ROOT=${WM_ROOT:-/data/wm}

ROLE=${ROLE:-}
NAME=${NAME:-vllm-ascend-lake-test}
PORT=${PORT:-8000}
MODEL=${MODEL:-/data/models/Qwen3.8-27B}
SERVED_NAME=${SERVED_NAME:-qwen}
DISCOVERY=${DISCOVERY:-etcd}
HOST_IP=${HOST_IP:-127.0.0.1}
ETCD_ENDPOINTS=${ETCD_ENDPOINTS:-http://${HOST_IP}:2379}
STORE=${STORE:-$WM_ROOT/dynamo_store_kv}
LOGDIR=${LOGDIR:-$WM_ROOT/dynamo-lake-logs}
RESTART=${RESTART:-0}
WAIT_MODEL=${WAIT_MODEL:-1}

P_TP=${P_TP:-4}
P_DP=${P_DP:-1}
D_TP=${D_TP:-4}
D_DP=${D_DP:-1}
P_NPU_GROUPS=${P_NPU_GROUPS:-"0,1,2,3;4,5,6,7"}
D_NPU_GROUPS=${D_NPU_GROUPS:-"0,1,2,3;4,5,6,7"}

MOONCAKE_RPC_PORT=${MOONCAKE_RPC_PORT:-50058}
MOONCAKE_CONFIG_PATH=${MOONCAKE_CONFIG_PATH:-$WM_ROOT/mooncake_conf/mooncake_config.json}
MC_MASTER_ADDRESS=${MC_MASTER_ADDRESS:-$HOST_IP}
MC_GLOBAL_SEGMENT_SIZE=${MC_GLOBAL_SEGMENT_SIZE:-4294967296}
MC_LOCAL_BUFFER_SIZE=${MC_LOCAL_BUFFER_SIZE:-4294967296}
PREFER_SAME_NODE=${PREFER_SAME_NODE:-false}
export PYTHONHASHSEED=0
MC_LIB_DIR=${MC_LIB_DIR:-/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/mooncake}

KV_PORT_PREFILL_BASE=${KV_PORT_PREFILL_BASE:-20001}
# Decode base must leave room for Prefill TP ranks (base .. base+TP-1).
KV_PORT_DECODE_BASE=${KV_PORT_DECODE_BASE:-20101}
P_SYSTEM_PORT_BASE=${P_SYSTEM_PORT_BASE:-8782}
D_SYSTEM_PORT_BASE=${D_SYSTEM_PORT_BASE:-8783}
P_KV_EVENT_PORT_BASE=${P_KV_EVENT_PORT_BASE:-20082}
D_KV_EVENT_PORT_BASE=${D_KV_EVENT_PORT_BASE:-20083}
ROUTER_MODE=${ROUTER_MODE:-kv}

ensure_container() {
  if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    WITH_NPU=1 bash "$SCRIPT_DIR/start_docker.sh"
  fi
}

ensure_etcd() {
  ADVERTISE_CLIENT_URL="http://${HOST_IP}:2379" bash "$SCRIPT_DIR/start_etcd.sh"
}

stop_role() {
  local role=$1
  docker exec -i "$NAME" /bin/bash <<EOS || true
set +e
python3 - <<'PY'
import os, signal, time
role = "$role"
needles = ("dynamo.frontend", "dynamo.vllm", "mooncake_master", "VLLM::", "VLLMWorker", "EngineCor")
me, parent = os.getpid(), os.getppid()
killed = []
for _ in range(4):
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
        if not any(n in cmd for n in needles):
            continue
        if role == "p" and "dynamo.vllm" in cmd and "--disaggregation-mode decode" in cmd:
            continue
        try:
            os.kill(ipid, signal.SIGKILL)
            killed.append(ipid)
        except Exception:
            pass
    time.sleep(2)
print(f"role={role} killed {len(set(killed))}: {sorted(set(killed))}")
PY
EOS
}

split_groups() { echo "$1" | tr ';' '\n'; }
count_csv() { echo "$1" | tr ',' '\n' | grep -c .; }

write_mooncake_config() {
  local conf_dir
  conf_dir=$(dirname "$MOONCAKE_CONFIG_PATH")
  docker exec "$NAME" bash -lc "
set -e
mkdir -p '$conf_dir'
cat > '$MOONCAKE_CONFIG_PATH' <<EOF
{
  \"protocol\": \"ascend\",
  \"use_ascend_direct\": true,
  \"master_server_address\": \"$MC_MASTER_ADDRESS:$MOONCAKE_RPC_PORT\",
  \"global_segment_size\": $MC_GLOBAL_SEGMENT_SIZE,
  \"local_buffer_size\": $MC_LOCAL_BUFFER_SIZE,
  \"metadata_server\": \"P2PHANDSHAKE\",
  \"preferred_segment\": false,
  \"prefer_alloc_in_same_node\": $PREFER_SAME_NODE
}
EOF
"
}

start_mooncake_master() {
  docker exec -d "$NAME" bash -lc "
set -e
export LD_LIBRARY_PATH='$MC_LIB_DIR':\$LD_LIBRARY_PATH
python3 -c 'import socket;s=socket.socket();s.settimeout(0.5);s.connect((\"127.0.0.1\",$MOONCAKE_RPC_PORT))' 2>/dev/null && exit 0
nohup mooncake_master --rpc_port $MOONCAKE_RPC_PORT \
  --eviction_high_watermark_ratio 0.9 --rpc_thread_num 32 \
  >> '$LOGDIR/mooncake.log' 2>&1 &
echo \$! > '$LOGDIR/mooncake.pid'
"
}

start_frontend() {
  docker exec -d "$NAME" bash -lc "
set -e
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export DYN_DISCOVERY_BACKEND=etcd ETCD_ENDPOINTS='$ETCD_ENDPOINTS'
cd /tmp
nohup python3 -m dynamo.frontend \
  --http-port $PORT --discovery-backend etcd \
  --request-plane tcp --router-mode $ROUTER_MODE \
  > '$LOGDIR/frontend.log' 2>&1 &
echo \$! > '$LOGDIR/frontend.pid'
"
}

start_prefill_worker() {
  local idx=$1 npu=$2 kv_port=$3 sys_port=$4 ev_port=$5 lookup=$6
  local kv_events
  kv_events="{\"publisher\":\"zmq\",\"topic\":\"kv-events\",\"endpoint\":\"tcp://*:${ev_port}\",\"enable_kv_cache_events\":true}"
  docker exec -d "$NAME" bash -lc "
set -e
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONHASHSEED=0
export DYN_DISCOVERY_BACKEND=etcd ETCD_ENDPOINTS='$ETCD_ENDPOINTS'
export MOONCAKE_CONFIG_PATH='$MOONCAKE_CONFIG_PATH'
export LD_LIBRARY_PATH='$MC_LIB_DIR':\$LD_LIBRARY_PATH
export ASCEND_RT_VISIBLE_DEVICES='$npu' DYN_SYSTEM_PORT=$sys_port
cd /tmp
nohup python3 -m dynamo.vllm \
  --model '$MODEL' --served-model-name '$SERVED_NAME' \
  --tensor-parallel-size $P_TP --data-parallel-size $P_DP \
  --discovery-backend etcd --request-plane tcp \
  --disaggregation-mode prefill --trust-remote-code \
  --gpu-memory-utilization 0.9 --max-model-len 32768 --max-num-seqs 64 \
  --enable-prefix-caching --kv-events-config '$kv_events' \
  --kv-transfer-config '{\"kv_connector\": \"MultiConnector\", \"kv_role\": \"kv_producer\", \"kv_connector_extra_config\": {\"connectors\": [{\"kv_connector\": \"MooncakeConnectorV1\", \"kv_role\": \"kv_producer\", \"kv_port\": \"$kv_port\", \"kv_connector_extra_config\": {\"prefill\": {\"dp_size\": $P_DP, \"tp_size\": $P_TP}, \"decode\": {\"dp_size\": $D_DP, \"tp_size\": $D_TP}}}, {\"kv_connector\": \"AscendStoreConnector\", \"kv_role\": \"kv_producer\", \"kv_connector_extra_config\": {\"backend\": \"mooncake\", \"lookup_rpc_port\": \"$lookup\"}}]}}' \
  > '$LOGDIR/worker_prefill_${idx}.log' 2>&1 &
echo \$! > '$LOGDIR/worker_prefill_${idx}.pid'
"
}

start_decode_worker() {
  local idx=$1 npu=$2 kv_port=$3 sys_port=$4 ev_port=$5 lookup=$6
  local kv_events
  kv_events="{\"publisher\":\"zmq\",\"topic\":\"kv-events\",\"endpoint\":\"tcp://*:${ev_port}\",\"enable_kv_cache_events\":true}"
  docker exec -d "$NAME" bash -lc "
set -e
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONHASHSEED=0
export DYN_DISCOVERY_BACKEND=etcd ETCD_ENDPOINTS='$ETCD_ENDPOINTS'
export MOONCAKE_CONFIG_PATH='$MOONCAKE_CONFIG_PATH'
export LD_LIBRARY_PATH='$MC_LIB_DIR':\$LD_LIBRARY_PATH
export ASCEND_RT_VISIBLE_DEVICES='$npu' DYN_SYSTEM_PORT=$sys_port
cd /tmp
nohup python3 -m dynamo.vllm \
  --model '$MODEL' --served-model-name '$SERVED_NAME' \
  --tensor-parallel-size $D_TP --data-parallel-size $D_DP \
  --discovery-backend etcd --request-plane tcp \
  --disaggregation-mode decode --trust-remote-code \
  --gpu-memory-utilization 0.9 --max-model-len 32768 --max-num-seqs 64 \
  --enable-prefix-caching --kv-events-config '$kv_events' \
  --kv-transfer-config '{\"kv_connector\": \"MultiConnector\", \"kv_role\": \"kv_consumer\", \"kv_connector_extra_config\": {\"connectors\": [{\"kv_connector\": \"MooncakeConnectorV1\", \"kv_role\": \"kv_consumer\", \"kv_port\": \"$kv_port\", \"kv_connector_extra_config\": {\"prefill\": {\"dp_size\": $P_DP, \"tp_size\": $P_TP}, \"decode\": {\"dp_size\": $D_DP, \"tp_size\": $D_TP}}}, {\"kv_connector\": \"AscendStoreConnector\", \"kv_role\": \"kv_consumer\", \"kv_connector_extra_config\": {\"backend\": \"mooncake\", \"lookup_rpc_port\": \"$lookup\"}}]}}' \
  > '$LOGDIR/worker_decode_${idx}.log' 2>&1 &
echo \$! > '$LOGDIR/worker_decode_${idx}.pid'
"
}

wait_for_model() {
  local ok=0 i
  for i in $(seq 1 240); do
    if curl -sf "localhost:$PORT/v1/models" 2>/dev/null | grep -q "\"$SERVED_NAME\""; then
      ok=1; break
    fi
    sleep 5
  done
  [[ "$ok" == "1" ]] || { echo "TIMEOUT waiting for $SERVED_NAME" >&2; return 1; }
  curl -s "localhost:$PORT/v1/models"; echo
}

cmd=${1:-start}
[[ -n "$ROLE" && ( "$ROLE" == "p" || "$ROLE" == "d" ) ]] || { echo "ROLE=p|d required" >&2; exit 2; }
[[ "$DISCOVERY" == "etcd" ]] || { echo "cross-host requires etcd" >&2; exit 2; }

ensure_container
SRC=${SRC:-$WM_ROOT/dynamo} bash "$SCRIPT_DIR/install_src.sh"
mkdir -p "$STORE" "$LOGDIR"

if [[ "$cmd" == "stop" ]]; then
  stop_role "$ROLE"; exit 0
fi
[[ "$RESTART" == "1" ]] && stop_role "$ROLE"

write_mooncake_config

if [[ "$ROLE" == "p" ]]; then
  ensure_etcd
  : >"$LOGDIR/frontend.log" : >"$LOGDIR/mooncake.log"
  start_mooncake_master
  sleep 1
  start_frontend
  sleep 2
  idx=0
  while IFS= read -r g; do
    [[ -z "$g" ]] && continue
    kv=$((KV_PORT_PREFILL_BASE + idx * 10))
    sys=$((P_SYSTEM_PORT_BASE + idx * 2))
    ev=$((P_KV_EVENT_PORT_BASE + idx * 2))
    : >"$LOGDIR/worker_prefill_${idx}.log"
    start_prefill_worker "$idx" "$g" "$kv" "$sys" "$ev" "$idx"
    idx=$((idx + 1))
    sleep 3
  done < <(split_groups "$P_NPU_GROUPS")
  echo "P-host up: FE :$PORT router=$ROUTER_MODE ${idx}×prefill"
  [[ "$WAIT_MODEL" == "1" ]] && wait_for_model || true
else
  idx=0
  while IFS= read -r g; do
    [[ -z "$g" ]] && continue
    kv=$((KV_PORT_DECODE_BASE + idx * 10))
    sys=$((D_SYSTEM_PORT_BASE + idx * 2))
    ev=$((D_KV_EVENT_PORT_BASE + idx * 2))
    : >"$LOGDIR/worker_decode_${idx}.log"
    start_decode_worker "$idx" "$g" "$kv" "$sys" "$ev" "$((100 + idx))"
    idx=$((idx + 1))
    sleep 3
  done < <(split_groups "$D_NPU_GROUPS")
  echo "D-host up: ${idx}×decode"
fi
