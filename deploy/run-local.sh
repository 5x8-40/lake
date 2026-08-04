#!/usr/bin/env bash
# P3 本地起全栈(无 Docker):controlplane + kv-pool + storage-agent + worker + router。
# 用法(仓库根):
#   ./deploy/run-local.sh          # 前台,Ctrl-C 全停
#   ./deploy/smoke.sh              # 另开终端打冒烟
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export LAKE_CP_ADDR="${LAKE_CP_ADDR:-127.0.0.1:50051}"
export LAKE_KV_ADDR="${LAKE_KV_ADDR:-127.0.0.1:50052}"
export LAKE_WORKER_BIND="${LAKE_WORKER_BIND:-[::]:50053}"
export LAKE_WORKER_ADDR="${LAKE_WORKER_ADDR:-127.0.0.1:50053}"
export LAKE_AGENT_ADDR="${LAKE_AGENT_ADDR:-127.0.0.1:50054}"
export LAKE_HTTP_ADDR="${LAKE_HTTP_ADDR:-:8080}"
export LAKE_MODEL_BACKEND="${LAKE_MODEL_BACKEND:-mock}"
export LAKE_SERVED_MODEL_NAME="${LAKE_SERVED_MODEL_NAME:-mock-llm}"

# 兼容 CARGO_TARGET_DIR 覆盖(如 CI/sandbox);不要写死 rust/target/debug。
RUST_DEBUG="$(
  cd rust
  cargo metadata --format-version 1 --no-deps \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["target_directory"] + "/debug")'
)"
GO_BIN="$ROOT/deploy/.bin/lake-router"
mkdir -p "$ROOT/deploy/.bin"

wait_http() {
  local url="$1" name="$2" tries="${3:-60}"
  for ((i=1; i<=tries; i++)); do
    if curl -sf "$url" >/dev/null 2>&1; then
      echo "  ready: $name"
      return 0
    fi
    sleep 0.25
  done
  echo "ERROR: timeout waiting for $name ($url)" >&2
  return 1
}

wait_tcp() {
  local hostport="$1" name="$2" tries="${3:-60}"
  local host="${hostport%:*}" port="${hostport##*:}"
  for ((i=1; i<=tries; i++)); do
    if (echo >/dev/tcp/"$host"/"$port") >/dev/null 2>&1; then
      echo "  ready: $name ($hostport)"
      return 0
    fi
    sleep 0.25
  done
  echo "ERROR: timeout waiting for $name ($hostport)" >&2
  return 1
}

PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

# 端口必须空闲,避免连到上一次残留进程导致「假复用」。
for port in 50051 50052 50053 50054 8080; do
  if (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1; then
    echo "ERROR: port $port already in use; stop the old stack first" >&2
    exit 1
  fi
done

echo "==> build rust bins → $RUST_DEBUG"
(cd rust && cargo build -q \
  -p lake-controlplane --bin lake-controlplane \
  -p lake-kv-pool --bin lake-kv-pool \
  -p lake-storage-agent --bin lake-storage-agent)
test -x "$RUST_DEBUG/lake-controlplane"
test -x "$RUST_DEBUG/lake-kv-pool"
test -x "$RUST_DEBUG/lake-storage-agent"

echo "==> build go router → $GO_BIN"
(cd go && go build -o "$GO_BIN" ./router/cmd/router)

echo "==> start controlplane"
LAKE_CP_ADDR=0.0.0.0:50051 "$RUST_DEBUG/lake-controlplane" &
PIDS+=($!)
wait_tcp 127.0.0.1:50051 controlplane

echo "==> start kv-pool"
LAKE_KV_ADDR=0.0.0.0:50052 "$RUST_DEBUG/lake-kv-pool" &
PIDS+=($!)
wait_tcp 127.0.0.1:50052 kv-pool

echo "==> start storage-agent (Dispatch)"
LAKE_AGENT_ADDR=0.0.0.0:50054 "$RUST_DEBUG/lake-storage-agent" &
PIDS+=($!)
wait_tcp 127.0.0.1:50054 storage-agent

echo "==> start python worker"
(
  cd python
  PYTHONPATH=. python3 -m lake.runtime
) &
PIDS+=($!)
wait_tcp 127.0.0.1:50053 worker

echo "==> start go router"
LAKE_HTTP_ADDR="$LAKE_HTTP_ADDR" \
LAKE_WORKER_ADDR="$LAKE_WORKER_ADDR" \
LAKE_AGENT_ADDR="$LAKE_AGENT_ADDR" \
"$GO_BIN" &
PIDS+=($!)

# HTTP 口可能是 :8080
HTTP_URL="http://127.0.0.1${LAKE_HTTP_ADDR/:/:}"
if [[ "$LAKE_HTTP_ADDR" == :* ]]; then
  HTTP_URL="http://127.0.0.1${LAKE_HTTP_ADDR}"
fi
wait_http "${HTTP_URL}/healthz" router

echo "stack up: HTTP ${HTTP_URL}  (try ./deploy/smoke.sh)"
wait
