#!/usr/bin/env bash
# Start/reuse a vllm-ascend container. WITH_NPU=0 skips NPU devices (build-only).
set -euo pipefail
IMAGE=${IMAGE:-quay.io/ascend/vllm-ascend:v0.26.0rc1}
NAME=${NAME:-vllm-ascend-lake-test}
WITH_NPU=${WITH_NPU:-1}

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker start "$NAME" >/dev/null
  echo "reused: $NAME"
  exit 0
fi

devs=()
if [[ "$WITH_NPU" == "1" ]]; then
  for d in davinci{0..7} davinci_manager devmm_svm hisi_hdc; do
    devs+=(--device "/dev/$d")
  done
fi

docker run -d --name "$NAME" --shm-size=1g --net=host "${devs[@]}" \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
  -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
  -v /etc/ascend_install.info:/etc/ascend_install.info \
  -v /data:/data -v /root/.cache:/root/.cache \
  -e PIP_INDEX_URL=http://mirrors.tools.huawei.com/pypi/simple \
  -e PIP_TRUSTED_HOST=mirrors.tools.huawei.com \
  "$IMAGE" sleep infinity
echo "created: $NAME (WITH_NPU=$WITH_NPU)"
