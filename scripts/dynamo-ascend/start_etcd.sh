#!/usr/bin/env bash
# Persistent single-node etcd for Dynamo discovery.
set -euo pipefail
NAME=${NAME:-dynamo-etcd}
IMAGE=${IMAGE:-quay.io/coreos/etcd:v3.5.16}
DATA_DIR=${DATA_DIR:-${WM_ROOT:-/data/wm}/etcd-data}

case "${1:-start}" in
  stop) docker rm -f "$NAME" 2>/dev/null; echo removed; exit 0 ;;
  status) docker exec "$NAME" etcdctl endpoint health; exit $? ;;
esac

mkdir -p "$DATA_DIR"
if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "already running: $NAME"; exit 0
fi
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker start "$NAME"; exit 0
fi
ADVERTISE_CLIENT_URL=${ADVERTISE_CLIENT_URL:-http://127.0.0.1:2379}
docker run -d --name "$NAME" --net=host --restart unless-stopped \
  -v "$DATA_DIR:/etcd-data" "$IMAGE" \
  etcd --data-dir=/etcd-data --listen-client-urls=http://0.0.0.0:2379 \
  --advertise-client-urls="$ADVERTISE_CLIENT_URL"
echo "started: $NAME (advertise=$ADVERTISE_CLIENT_URL)"
