#!/usr/bin/env bash
# End-to-end va-precopy on vllm-ascend + Mooncake.
#
#   DRY_RUN=1 bash run_e2e.sh
#   TARGET_SEGMENT=... KEYS_FILE=... MODEL=... bash run_e2e.sh
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
mkdir -p "$LOGDIR"

DRY_RUN=${DRY_RUN:-0}
MC_PORT=${MC_PORT:-50088}
MC_MASTER=${MC_MASTER:-127.0.0.1:$MC_PORT}
MODEL=${MODEL:-/data/models/Qwen2.5-7B-Instruct}
SERVED_NAME=${SERVED_NAME:-qwen}
PROTOCOL=${MOONCAKE_PROTOCOL:-ascend}
PORT_A=${PORT_A:-8001}
PORT_B=${PORT_B:-8002}
DEVICES_A=${DEVICES_A:-0}
DEVICES_B=${DEVICES_B:-1}
TARGET_SEGMENT=${TARGET_SEGMENT:-}
KEYS_FILE=${KEYS_FILE:-}
PREFIX=${PREFIX:-"va-precopy shared prefix for store warmup. "}
WARM_MAX_TOKENS=${WARM_MAX_TOKENS:-1}

step() { echo; echo "==== $* ===="; }

step "0. Plan"
cat <<EOF
  master:   $MC_MASTER
  worker-A: port=$PORT_A devices=$DEVICES_A  (warm / source)
  worker-B: port=$PORT_B devices=$DEVICES_B  (target)
  protocol: $PROTOCOL
  model:    $MODEL (served=$SERVED_NAME)
  precopy:  create_copy_task(keys -> TARGET_SEGMENT) then request worker-B
EOF

step "1. mooncake_master"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: MC_PORT=$MC_PORT bash $SCRIPT_DIR/start_master.sh"
else
  MC_PORT=$MC_PORT bash "$SCRIPT_DIR/start_master.sh"
fi

step "2. worker-A (source)"
if [[ "$DRY_RUN" == "1" ]]; then
  DRY_RUN=1 ROLE=A PORT=$PORT_A LOOKUP_ID=0 ASCEND_RT_VISIBLE_DEVICES=$DEVICES_A \
    MODEL=$MODEL SERVED_NAME=$SERVED_NAME MC_MASTER=$MC_MASTER \
    MOONCAKE_PROTOCOL=$PROTOCOL bash "$SCRIPT_DIR/start_worker.sh"
else
  ROLE=A PORT=$PORT_A LOOKUP_ID=0 ASCEND_RT_VISIBLE_DEVICES=$DEVICES_A \
    MODEL=$MODEL SERVED_NAME=$SERVED_NAME MC_MASTER=$MC_MASTER \
    MOONCAKE_PROTOCOL=$PROTOCOL bash "$SCRIPT_DIR/start_worker.sh"
fi

step "3. worker-B (target)"
if [[ "$DRY_RUN" == "1" ]]; then
  DRY_RUN=1 ROLE=B PORT=$PORT_B LOOKUP_ID=1 ASCEND_RT_VISIBLE_DEVICES=$DEVICES_B \
    MODEL=$MODEL SERVED_NAME=$SERVED_NAME MC_MASTER=$MC_MASTER \
    MOONCAKE_PROTOCOL=$PROTOCOL bash "$SCRIPT_DIR/start_worker.sh"
else
  ROLE=B PORT=$PORT_B LOOKUP_ID=1 ASCEND_RT_VISIBLE_DEVICES=$DEVICES_B \
    MODEL=$MODEL SERVED_NAME=$SERVED_NAME MC_MASTER=$MC_MASTER \
    MOONCAKE_PROTOCOL=$PROTOCOL bash "$SCRIPT_DIR/start_worker.sh"
fi

step "4. Warm prefix on worker-A"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: curl worker-A /v1/completions max_tokens=$WARM_MAX_TOKENS"
else
  for i in $(seq 1 120); do
    if curl -sf "http://127.0.0.1:${PORT_A}/v1/models" | grep -q "$SERVED_NAME"; then
      break
    fi
    sleep 5
  done
  BODY=$(PREFIX="$PREFIX" SERVED_NAME="$SERVED_NAME" WARM_MAX_TOKENS="$WARM_MAX_TOKENS" python3 - <<'PY'
import json, os
print(json.dumps({
  "model": os.environ["SERVED_NAME"],
  "prompt": os.environ["PREFIX"],
  "max_tokens": int(os.environ["WARM_MAX_TOKENS"]),
  "temperature": 0,
}))
PY
)
  curl -s "http://127.0.0.1:${PORT_A}/v1/completions" \
    -H 'Content-Type: application/json' \
    -d "$BODY"
  echo
fi

step "5. precopy create_copy_task -> worker-B segment"
if [[ -z "$TARGET_SEGMENT" || -z "$KEYS_FILE" ]]; then
  cat <<EOF
  SKIPPED (need both):
    TARGET_SEGMENT=<worker-B local_seg, e.g. hostname:rpc_port>
    KEYS_FILE=<one Mooncake object key per line>

  How to obtain:
    - TARGET_SEGMENT: worker-B log (MooncakeBackend local_seg)
    - KEYS_FILE: python3 keys.py --model-name $SERVED_NAME --chunk-hashes <hex,...>

  Example:
    python3 $SCRIPT_DIR/precopy.py \\
      --master $MC_MASTER --protocol $PROTOCOL \\
      --target "\$TARGET_SEGMENT" --keys-file "\$KEYS_FILE" --dry-show-before
EOF
  exit 0
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: precopy.py --target $TARGET_SEGMENT --keys-file $KEYS_FILE"
  exit 0
fi

export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
python3 "$SCRIPT_DIR/precopy.py" \
  --master "$MC_MASTER" \
  --protocol "$PROTOCOL" \
  --target "$TARGET_SEGMENT" \
  --keys-file "$KEYS_FILE" \
  --dry-show-before

step "6. Hit worker-B AFTER READY"
BODY=$(PREFIX="$PREFIX" SERVED_NAME="$SERVED_NAME" python3 - <<'PY'
import json, os
print(json.dumps({
  "model": os.environ["SERVED_NAME"],
  "prompt": os.environ["PREFIX"],
  "max_tokens": 16,
  "temperature": 0,
}))
PY
)
curl -s "http://127.0.0.1:${PORT_B}/v1/completions" \
  -H 'Content-Type: application/json' \
  -d "$BODY"
echo
echo "DONE - check worker-B logs for local-first replica"
