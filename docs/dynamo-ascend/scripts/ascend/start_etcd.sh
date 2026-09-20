#!/usr/bin/env bash
# Persistent single-node etcd for Dynamo discovery (cross-host ready).
#
# Usage:
#   bash scripts/ascend/start_etcd.sh          # start or reuse
#   bash scripts/ascend/start_etcd.sh stop
#   bash scripts/ascend/start_etcd.sh status
#
# Clients (host or --net=host containers):
#   export ETCD_ENDPOINTS=http://127.0.0.1:2379
# Other machines: use this host's reachable IP, e.g.
#   export ETCD_ENDPOINTS=http://<this-host-ip>:2379
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WM_ROOT=${WM_ROOT:-/data/wm}

NAME=${NAME:-dynamo-etcd}
IMAGE=${IMAGE:-quay.io/coreos/etcd:v3.5.16}
DATA_DIR=${DATA_DIR:-$WM_ROOT/etcd-data}
CLIENT_URL=${CLIENT_URL:-http://0.0.0.0:2379}
ADVERTISE_CLIENT_URL=${ADVERTISE_CLIENT_URL:-http://127.0.0.1:2379}

cmd=${1:-start}

status() {
  if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "running: $NAME"
    docker exec "$NAME" etcdctl --endpoints=http://127.0.0.1:2379 endpoint health 2>&1 || true
    return 0
  fi
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "exists but stopped: $NAME"
    return 1
  fi
  echo "missing: $NAME"
  return 1
}

stop() {
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker rm -f "$NAME" >/dev/null
    echo "removed: $NAME"
  else
    echo "not present: $NAME"
  fi
}

start() {
  mkdir -p "$DATA_DIR"
  if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "already running: $NAME"
  elif docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "starting existing: $NAME"
    docker start "$NAME" >/dev/null
  else
    # --net=host so vllm-ascend-wm (--net=host) and other nodes can use 127.0.0.1
    # or the host LAN IP on :2379 without docker bridge NAT surprises.
    docker run -d \
      --name "$NAME" \
      --net=host \
      --restart unless-stopped \
      -v "$DATA_DIR:/etcd-data" \
      "$IMAGE" \
      /usr/local/bin/etcd \
        --name dynamo-etcd0 \
        --data-dir /etcd-data \
        --listen-client-urls "$CLIENT_URL" \
        --advertise-client-urls "$ADVERTISE_CLIENT_URL" \
        --listen-peer-urls http://127.0.0.1:2380 \
        --initial-advertise-peer-urls http://127.0.0.1:2380 \
        --initial-cluster dynamo-etcd0=http://127.0.0.1:2380 \
        --initial-cluster-token dynamo-etcd-cluster \
        --initial-cluster-state new \
        --auto-compaction-retention=1 \
        --quota-backend-bytes=8589934592
    echo "created and started: $NAME"
  fi

  for i in $(seq 1 30); do
    if docker exec "$NAME" etcdctl --endpoints=http://127.0.0.1:2379 endpoint health 2>/dev/null | grep -qi healthy; then
      echo "healthy: ETCD_ENDPOINTS=http://127.0.0.1:2379 (data: $DATA_DIR)"
      echo "cross-host: set ETCD_ENDPOINTS=http://<this-host-ip>:2379 on other nodes"
      return 0
    fi
    sleep 1
  done
  echo "etcd did not become healthy in time" >&2
  docker logs --tail 40 "$NAME" || true
  exit 1
}

case "$cmd" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
