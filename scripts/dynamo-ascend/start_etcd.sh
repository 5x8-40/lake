#!/usr/bin/env bash
# Persistent single-node etcd for Dynamo discovery.
# Uses ETCD_NAME (not NAME) so callers exporting NAME=vllm-ascend-* cannot wipe the wrong container.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAKE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
ETCD_NAME=${ETCD_NAME:-dynamo-etcd}
IMAGE=${IMAGE:-quay.io/coreos/etcd:v3.5.16}
DATA_DIR=${DATA_DIR:-$LAKE_ROOT/scripts/dynamo-ascend/etcd-data}
ADVERTISE_CLIENT_URL=${ADVERTISE_CLIENT_URL:-http://127.0.0.1:2379}

case "${1:-start}" in
  stop) docker rm -f "$ETCD_NAME" 2>/dev/null; echo removed; exit 0 ;;
  status) docker exec "$ETCD_NAME" etcdctl endpoint health; exit $? ;;
esac

mkdir -p "$DATA_DIR"
if docker ps --format '{{.Names}}' | grep -qx "$ETCD_NAME"; then
  echo "already running: $ETCD_NAME"; exit 0
fi
if docker ps -a --format '{{.Names}}' | grep -qx "$ETCD_NAME"; then
  docker start "$ETCD_NAME"; exit 0
fi
docker run -d --name "$ETCD_NAME" --net=host --restart unless-stopped \
  -v "$DATA_DIR:/etcd-data" "$IMAGE" \
  etcd --data-dir=/etcd-data --listen-client-urls=http://0.0.0.0:2379 \
  --advertise-client-urls="$ADVERTISE_CLIENT_URL"
echo "started: $ETCD_NAME (advertise=$ADVERTISE_CLIENT_URL)"
