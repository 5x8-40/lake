#!/usr/bin/env bash
# Start a persistent Ascend vLLM container (does not attach; use docker exec to enter).
#
# Usage:
#   bash scripts/ascend/start_docker_va.sh
#   NAME=vllm-ascend-wm IMAGE=quay.io/ascend/vllm-ascend:v0.26.0rc1 bash ...
set -euo pipefail

export IMAGE=${IMAGE:-quay.io/ascend/vllm-ascend:v0.26.0rc1}
export NAME=${NAME:-vllm-ascend-wm}

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "already running: $NAME"
  else
    echo "starting existing container: $NAME"
    docker start "$NAME"
  fi
else
  docker run -d \
    --name "$NAME" \
    --shm-size=1g \
    --net=host \
    --device /dev/davinci0 \
    --device /dev/davinci1 \
    --device /dev/davinci2 \
    --device /dev/davinci3 \
    --device /dev/davinci4 \
    --device /dev/davinci5 \
    --device /dev/davinci6 \
    --device /dev/davinci7 \
    --device /dev/davinci_manager \
    --device /dev/devmm_svm \
    --device /dev/hisi_hdc \
    -v /usr/local/dcmi:/usr/local/dcmi \
    -v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool \
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
    -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
    -v /etc/ascend_install.info:/etc/ascend_install.info \
    -v /root/.cache:/root/.cache \
    -v /data:/data \
    -e PIP_INDEX_URL=http://mirrors.tools.huawei.com/pypi/simple \
    -e PIP_TRUSTED_HOST=mirrors.tools.huawei.com \
    -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    -e LANG=C.UTF-8 \
    "$IMAGE" \
    sleep infinity
  echo "created and started: $NAME"
fi

echo "enter:  docker exec -it $NAME bash"
echo "stop:   docker stop $NAME"
echo "remove: docker rm -f $NAME"
