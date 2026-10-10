#!/usr/bin/env bash
# End-to-end va-precopy on vllm-ascend + Mooncake (validated: TP=1, A2 RoCE).
#
#   DRY_RUN=1 bash run_e2e.sh
#   LOCAL_IP=... NIC_NAME=... MODEL=... bash run_e2e.sh
#
# Auto path (after workers ready): warm → precopy → hit B.
# precopy.py is the single control-plane entry: resolves B's rank→seg in-process
# (master admin API + pidfile/ss, no log parsing), computes keys (import
# vllm/vllm-ascend), checks batch_is_exist, then copies: rank i keys → B segs[i].
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
mkdir -p "$LOGDIR"

DRY_RUN=${DRY_RUN:-0}
MC_PORT=${MC_PORT:-50088}
MC_MASTER=${MC_MASTER:-127.0.0.1:$MC_PORT}
MODEL=${MODEL:-/data/models/Qwen3-VL-8B-w8a8c16}
SERVED_NAME=${SERVED_NAME:-qwen}
MODEL_NAME=${MODEL_NAME:-$(basename "$MODEL")}
PROTOCOL=${MOONCAKE_PROTOCOL:-ascend}
PORT_A=${PORT_A:-8001}
PORT_B=${PORT_B:-8002}
DEVICES_A=${DEVICES_A:-0}
DEVICES_B=${DEVICES_B:-1}
TP=${TP:-1}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-4096}
TARGET_SEGMENTS=${TARGET_SEGMENTS:-}
RESOLVE_TARGET_IP=${RESOLVE_TARGET_IP:-${LOCAL_IP:-}}
KEYS_FILE=${KEYS_FILE:-$SCRIPT_DIR/prefix_keys.txt}
PREFIX=${PREFIX:-"va-precopy shared prefix for store warmup. "}
PREFIX_REPEAT=${PREFIX_REPEAT:-80}
BLOCK_SIZE=${BLOCK_SIZE:-128}
PUT_STEP=${PUT_STEP:-1}
WARM_MAX_TOKENS=${WARM_MAX_TOKENS:-1}
ENABLE_ASCEND_A2=${ENABLE_ASCEND_A2:-1}
AUTO_PRECOPY=${AUTO_PRECOPY:-1}
HIT_B=${HIT_B:-1}
# Heterogeneous TP: A=TP_A (prefill/source), B=TP_B (decode/target). Same TP: leave unset.
TP_A=${TP_A:-$TP}
TP_B=${TP_B:-$TP}
PREFILL_TP_SIZE=${PREFILL_TP_SIZE:-$TP_A}
# kv_both reads decode_tp_size as PEER size; it must be effective_tp=max(A,B)
# in BOTH directions (forward: A does sub-key put; reverse: B does sub-key get).
# Defaulting to TP_B breaks reverse (B sees peer==local → plain get → fake hit).
DECODE_TP_SIZE=${DECODE_TP_SIZE:-$(( TP_A > TP_B ? TP_A : TP_B ))}

step() { echo; echo "==== $* ===="; }

if [[ "$TP_A" != "$TP_B" ]]; then
  echo "[e2e] heterogeneous TP: A=$TP_A -> B=$TP_B (prefill_tp_size=$PREFILL_TP_SIZE decode_tp_size=$DECODE_TP_SIZE; needs patch_tp_mismatch_worker.patch in container; see README 异构 TP)." >&2
elif [[ "$TP_B" != "1" ]]; then
  echo "[e2e] TP=$TP_B homogeneous: rank i keys → B local_seg[i] (see README 怎么运作)." >&2
fi

if [[ "$ENABLE_ASCEND_A2" == "1" && "$PROTOCOL" == "ascend" && "$DRY_RUN" != "1" ]]; then
  # shellcheck source=env_ascend_a2.sh
  source "$SCRIPT_DIR/cluster/env_ascend_a2.sh"
fi

WARM_PROMPT=$(PREFIX="$PREFIX" PREFIX_REPEAT="$PREFIX_REPEAT" python3 - <<'PY'
import os
print(os.environ["PREFIX"] * int(os.environ["PREFIX_REPEAT"]))
PY
)

step "0. Plan"
cat <<EOF
  master:     $MC_MASTER
  worker-A:   port=$PORT_A devices=$DEVICES_A TP=$TP_A  (warm / source)
  worker-B:   port=$PORT_B devices=$DEVICES_B TP=$TP_B  (target)
  protocol:   $PROTOCOL  ENABLE_ASCEND_A2=$ENABLE_ASCEND_A2
  model:      $MODEL (served=$SERVED_NAME pool_model_name=$MODEL_NAME)
  prefix:     len=${#WARM_PROMPT} (base*${PREFIX_REPEAT})
  precopy:    rank i keys → B segs[i] (segments resolved in-process unless given)
  AUTO_PRECOPY=$AUTO_PRECOPY KEYS_FILE=$KEYS_FILE
  TARGET_SEGMENTS=${TARGET_SEGMENTS:-<auto>}
EOF

step "1. mooncake_master"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: MC_PORT=$MC_PORT bash $SCRIPT_DIR/cluster/start_master.sh"
else
  MC_PORT=$MC_PORT bash "$SCRIPT_DIR/cluster/start_master.sh"
fi

step "2. worker-A (source)"
_start_worker() {
  local role=$1 port=$2 lookup=$3 devices=$4 tp=$5
  ROLE=$role PORT=$port LOOKUP_ID=$lookup ASCEND_RT_VISIBLE_DEVICES=$devices \
    MODEL=$MODEL SERVED_NAME=$SERVED_NAME MC_MASTER=$MC_MASTER \
    MOONCAKE_PROTOCOL=$PROTOCOL TP=$tp MAX_MODEL_LEN=$MAX_MODEL_LEN \
    PREFILL_TP_SIZE=$PREFILL_TP_SIZE DECODE_TP_SIZE=$DECODE_TP_SIZE \
    ENABLE_ASCEND_A2=$ENABLE_ASCEND_A2 DRY_RUN=$DRY_RUN \
    bash "$SCRIPT_DIR/cluster/start_worker.sh"
}
_start_worker A "$PORT_A" 0 "$DEVICES_A" "$TP_A"

step "3. worker-B (target)"
_start_worker B "$PORT_B" 1 "$DEVICES_B" "$TP_B"

step "4. Warm prefix on worker-A"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: curl worker-A /v1/completions max_tokens=$WARM_MAX_TOKENS prompt_len=${#WARM_PROMPT}"
else
  wait_ready() {
    local role=$1 port=$2
    for _ in $(seq 1 120); do
      if curl -sf "http://127.0.0.1:${port}/v1/models" | grep -q "$SERVED_NAME"; then
        return 0
      fi
      sleep 5
    done
    echo "[e2e] ERROR: worker-$role (:$port) not ready after 600s — check for stale instances (fake READY: pgrep -af 'VLLM::'; see README 操作注意事项)" >&2
    return 1
  }
  wait_ready A "$PORT_A"
  wait_ready B "$PORT_B"
  BODY=$(WARM_PROMPT="$WARM_PROMPT" SERVED_NAME="$SERVED_NAME" WARM_MAX_TOKENS="$WARM_MAX_TOKENS" python3 - <<'PY'
import json, os
print(json.dumps({
  "model": os.environ["SERVED_NAME"],
  "prompt": os.environ["WARM_PROMPT"],
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

step "5. precopy (resolve segs → compute keys → check → copy, all in-process)"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: precopy.py prompt mode (segments + keys resolved in-process)"
  exit 0
fi

if [[ "$AUTO_PRECOPY" != "1" ]]; then
  echo "AUTO_PRECOPY=0 — skip precopy. Run precopy.py manually (--role B, or --targets to skip resolution)."
  exit 0
fi

# precopy.py inserts its own dir into sys.path; PYTHONHASHSEED is a legacy
# guard (only matters when engine prefix_caching_hash_algo=builtin).
export PYTHONHASHSEED=${PYTHONHASHSEED:-0}

# Single-entry precopy: warm prompt → in-process seg resolution + keys → check → copy.
PRECOPY_ARGS=(
  --master "$MC_MASTER"
  --protocol "$PROTOCOL"
  --model "$MODEL"
  --model-name "$MODEL_NAME"
  --prefix "$PREFIX"
  --prefix-repeat "$PREFIX_REPEAT"
  --block-size "$BLOCK_SIZE"
  --tp-size "$TP_B"
  --peer-tp-size "$TP_A"
  --put-step "$PUT_STEP"
  --device "${DEVICES_A%%,*}"
  --dump-keys "$KEYS_FILE"
  --dry-show-before
)
if [[ -n "$TARGET_SEGMENTS" ]]; then
  PRECOPY_ARGS+=(--targets "$TARGET_SEGMENTS")
else
  # In-process resolution: admin API + pidfile/ss cross-check (no log parsing).
  PRECOPY_ARGS+=(--role B)
  [[ -n "$RESOLVE_TARGET_IP" ]] && PRECOPY_ARGS+=(--target-ip "$RESOLVE_TARGET_IP")
  [[ -n "${RESOLVE_SSH:-}" ]] && PRECOPY_ARGS+=(--ssh "$RESOLVE_SSH")
  [[ -n "${RESOLVE_PIDFILE:-}" ]] && PRECOPY_ARGS+=(--pidfile "$RESOLVE_PIDFILE")
fi
if ! python3 "$SCRIPT_DIR/precopy/precopy.py" "${PRECOPY_ARGS[@]}"; then
  echo "[e2e] ERROR: precopy failed (keys missing = warm 未覆盖, or hash/key drift, or seg 解析失败)." >&2
  echo "[e2e] HINT: heterogeneous TP needs patch_tp_mismatch_worker.patch applied in the container (upstream tp_mismatch put is dead code otherwise)." >&2
  exit 1
fi

if [[ "$HIT_B" != "1" ]]; then
  echo "HIT_B=0 — skip request to worker-B"
  exit 0
fi

step "6. Hit worker-B AFTER READY"
BODY=$(WARM_PROMPT="$WARM_PROMPT" SERVED_NAME="$SERVED_NAME" python3 - <<'PY'
import json, os
print(json.dumps({
  "model": os.environ["SERVED_NAME"],
  "prompt": os.environ["WARM_PROMPT"],
  "max_tokens": 16,
  "temperature": 0,
}))
PY
)
curl -s "http://127.0.0.1:${PORT_B}/v1/completions" \
  -H 'Content-Type: application/json' \
  -d "$BODY"
echo
echo "DONE - expect External prefix cache hit on worker-B (人工取证: logs/worker_B.log；脚本不解析日志)"
