#!/usr/bin/env bash
# Start/reuse a vllm-ascend container with NPU devices.
# Mounts lake root (and SRC if outside it) so container builds see the same tree.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAKE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
SRC=${SRC:-$LAKE_ROOT/3rdparty/dynamo-ascend}
IMAGE=${IMAGE:-quay.io/ascend/vllm-ascend:v0.26.0rc1}
NAME=${NAME:-vllm-ascend-lake-test}

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker start "$NAME" >/dev/null
  echo "reused: $NAME"
  exit 0
fi

devs=()
for d in davinci{0..7} davinci_manager devmm_svm hisi_hdc; do
  [[ -e "/dev/$d" ]] && devs+=(--device "/dev/$d")
done

mounts=(-v "$LAKE_ROOT:$LAKE_ROOT")
# SRC may live outside lake (e.g. sibling clone); mount it explicitly.
case "$SRC" in
  "$LAKE_ROOT"/*) ;;
  *) mounts+=(-v "$SRC:$SRC") ;;
esac
# Optional model / cache mounts (override with MODEL_HOST_DIR).
MODEL_HOST_DIR=${MODEL_HOST_DIR:-/data/models}
[[ -d "$MODEL_HOST_DIR" ]] && mounts+=(-v "$MODEL_HOST_DIR:$MODEL_HOST_DIR")
[[ -d /root/.cache ]] && mounts+=(-v /root/.cache:/root/.cache)

extra_mounts=()
[[ -e /usr/local/Ascend/driver/tools/hccn_tool ]] && \
  extra_mounts+=(-v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool)
[[ -e /usr/local/bin/npu-smi ]] && \
  extra_mounts+=(-v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi)
[[ -f /etc/hccn.conf ]] && \
  extra_mounts+=(-v /etc/hccn.conf:/etc/hccn.conf)
[[ -f /usr/share/zoneinfo/Asia/Shanghai ]] && \
  extra_mounts+=(-v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime)
for p in /usr/local/dcmi /usr/local/Ascend/driver/lib64 /usr/local/Ascend/driver/version.info /etc/ascend_install.info; do
  [[ -e "$p" ]] && extra_mounts+=(-v "$p:$p")
done

docker run -d --name "$NAME" --shm-size=1g --net=host "${devs[@]}" \
  "${mounts[@]}" "${extra_mounts[@]}" \
  -e PIP_INDEX_URL=http://mirrors.tools.huawei.com/pypi/simple \
  -e PIP_TRUSTED_HOST=mirrors.tools.huawei.com \
  -e LANG=C.UTF-8 \
  "$IMAGE" sleep infinity
echo "created: $NAME (IMAGE=$IMAGE LAKE_ROOT=$LAKE_ROOT SRC=$SRC)"
