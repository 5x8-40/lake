#!/usr/bin/env bash
# Start FE + one aggregated worker inside the container (Dynamo already installed).
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAKE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
NAME=${NAME:-vllm-ascend-lake-test}
LOGDIR=${LOGDIR:-$LAKE_ROOT/scripts/dynamo-ascend/logs}
MODEL=${MODEL:-/data/models/Qwen3.8-27B}
SERVED=${SERVED_NAME:-qwen}
TP=${TP:-4}
DP=${DP:-2}
PORT=${PORT:-8000}
ETCD=${ETCD_ENDPOINTS:-http://127.0.0.1:2379}

mkdir -p "$LOGDIR"
: >"$LOGDIR/frontend.log" : >"$LOGDIR/worker.log"

docker exec "$NAME" bash -lc 'pkill -f "python3 -m dynamo.frontend" 2>/dev/null || true
pkill -f "python3 -m dynamo.vllm" 2>/dev/null || true; sleep 1' || true

docker exec -d "$NAME" bash -lc "
export ETCD_ENDPOINTS=$ETCD DYN_LOG=info
nohup python3 -m dynamo.frontend --http-port $PORT --discovery-backend etcd \
  >$LOGDIR/frontend.log 2>&1 &
nohup python3 -m dynamo.vllm --model $MODEL --served-model-name $SERVED \
  --tensor-parallel-size $TP --data-parallel-size $DP \
  --discovery-backend etcd --connector none \
  >$LOGDIR/worker.log 2>&1 &
echo started
"
echo "logs: $LOGDIR  probe: curl -s localhost:$PORT/v1/models"
