#!/usr/bin/env bash
# Aggregated FE + one worker (smoke / KV-router check). Aligned with start_pd defaults:
# --router-mode kv, --kv-events-config, DYN_SYSTEM_PORT for worker metrics.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAKE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
NAME=${NAME:-vllm-ascend-lake-test}
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
MODEL=${MODEL:-/data/models/Qwen3.8-27B}
SERVED=${SERVED_NAME:-qwen}
TP=${TP:-4}
DP=${DP:-2}
PORT=${PORT:-8000}
ETCD=${ETCD_ENDPOINTS:-http://127.0.0.1:2379}
ROUTER_MODE=${ROUTER_MODE:-kv}
SYSTEM_PORT=${SYSTEM_PORT:-8782}
KV_EVENT_PORT=${KV_EVENT_PORT:-20082}
KV_EVENTS_CONFIG=${KV_EVENTS_CONFIG:-"{\"publisher\":\"zmq\",\"topic\":\"kv-events\",\"endpoint\":\"tcp://*:${KV_EVENT_PORT}\",\"enable_kv_cache_events\":true}"}

mkdir -p "$LOGDIR"
: >"$LOGDIR/frontend.log" : >"$LOGDIR/worker.log"

docker exec "$NAME" bash -lc 'pkill -f "python3 -m dynamo.frontend" 2>/dev/null || true
pkill -f "python3 -m dynamo.vllm" 2>/dev/null || true; sleep 1' || true

docker exec -d "$NAME" bash -lc "
export ETCD_ENDPOINTS=$ETCD DYN_LOG=info DYN_SYSTEM_PORT=$SYSTEM_PORT
nohup python3 -m dynamo.frontend --http-port $PORT --discovery-backend etcd \
  --router-mode $ROUTER_MODE \
  >$LOGDIR/frontend.log 2>&1 &
nohup python3 -m dynamo.vllm --model $MODEL --served-model-name $SERVED \
  --tensor-parallel-size $TP --data-parallel-size $DP \
  --discovery-backend etcd --connector none \
  --enable-prefix-caching --kv-events-config '$KV_EVENTS_CONFIG' \
  >$LOGDIR/worker.log 2>&1 &
echo started
"
echo "logs: $LOGDIR  probe: curl -s localhost:$PORT/v1/models"
echo "kv metrics: curl -s localhost:$PORT/metrics | grep router_kv_; curl -s localhost:$SYSTEM_PORT/metrics | grep kv_publisher"
