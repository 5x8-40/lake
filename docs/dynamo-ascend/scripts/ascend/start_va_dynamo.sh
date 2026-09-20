#!/usr/bin/env bash
# vllm-ascend + dynamo 一体化脚本(Atlas A2 8 卡):
#   ./start_va_dynamo.sh        建容器(已存在则复用)+ 拉起 frontend/worker
#   ./start_va_dynamo.sh stop   停容器内 dynamo 进程
# 可用环境变量覆盖:NAME IMAGE WM_ROOT MODEL MODEL_NAME HTTP_PORT
set -euo pipefail

NAME=${NAME:-vllm-ascend-wcd}
IMAGE=${IMAGE:-quay.io/ascend/vllm-ascend:v0.26.0rc1}   # Ubuntu 变体;openeuler 变体在部分主机上 npu-smi 会卡住
WM_ROOT=${WM_ROOT:-/data/wm}                            # 容器经 -v /data:/data 可见
REPO=$WM_ROOT/dynamo-ascend                             # 宿主机源码(checkout v1.4.2)
MODEL=${MODEL:-/data/models/Qwen3.8-27B}
MODEL_NAME=${MODEL_NAME:-qwen}
HTTP_PORT=${HTTP_PORT:-8000}

if [[ "${1:-}" == "stop" ]]; then
  docker exec "$NAME" bash -lc 'pkill -f dynamo.frontend || true; pkill -f dynamo.vllm || true'
  echo "dynamo stopped in $NAME"
  exit 0
fi

# 1. 建容器(已存在则复用)
if ! docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
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
    "$IMAGE" sleep infinity
fi

# 2. 容器内拉起 dynamo(单机 file discovery,免 etcd)
docker exec -i \
  -e REPO="$REPO" -e WM_ROOT="$WM_ROOT" -e MODEL="$MODEL" \
  -e MODEL_NAME="$MODEL_NAME" -e HTTP_PORT="$HTTP_PORT" \
  "$NAME" bash -s <<'EOS'
set -e
# dynamo editable 安装(幂等;pip 自动管 .pth,不动镜像依赖)
PYPATH=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
if [ -f "$PYPATH/dynamo-ascend.pth" ] || ! python3 -c 'import dynamo.frontend' 2>/dev/null; then
  mkdir -p "$PYPATH/dynamo"
  if [ ! -f "$PYPATH/dynamo/_core.abi3.so" ] && [ -f "$REPO/lib/bindings/python/src/dynamo/_core.abi3.so" ]; then
    cp "$REPO/lib/bindings/python/src/dynamo/_core.abi3.so" "$PYPATH/dynamo/"
  fi
  pip install -e "$REPO" --no-deps
  rm -f "$PYPATH/dynamo-ascend.pth"   # 旧的手工 .pth:装成功后再删,避免 pip 失败时无可用安装
fi

# 前置检查:runtime .so 必须在位,否则 frontend/worker 起不来
if ! python3 -c 'import dynamo._core' 2>/dev/null; then
  echo "ERROR: import dynamo._core 失败——_core.abi3.so 缺失或不兼容。" >&2
  echo "  编译:cd $REPO/lib/bindings/python && maturin build --release && pip install target/release/wheels/ai_dynamo_runtime-*.whl" >&2
  echo "  或拷贝编好的 .so 到 $PYPATH/dynamo/" >&2
  exit 1
fi

ulimit -n 65535 || true
sysctl -w fs.inotify.max_user_watches=1048576 || true
pkill -f dynamo.frontend 2>/dev/null || true
pkill -f dynamo.vllm 2>/dev/null || true
sleep 2

mkdir -p "$WM_ROOT/logs"
nohup python3 -m dynamo.frontend --http-port "$HTTP_PORT" \
  --discovery-backend file --router-mode kv \
  > "$WM_ROOT/logs/frontend.log" 2>&1 &

export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
nohup python3 -m dynamo.vllm \
  --model "$MODEL" --served-model-name "$MODEL_NAME" \
  --data-parallel-size 2 --tensor-parallel-size 4 \
  --max-num-seqs 64 --max-model-len 256000 --max-num-batched-tokens 16384 \
  --trust-remote-code --enable-prefix-caching --gpu-memory-utilization 0.9 \
  --speculative-config '{"method": "qwen3_next_mtp", "num_speculative_tokens": 3, "enforce_eager": true}' \
  --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \
  --additional-config '{"enable_cpu_binding":true}' \
  --kv-events-config '{"enable_kv_cache_events": true, "publisher": "zmq", "topic": "kv-events"}' \
  --discovery-backend file --disaggregation-mode agg \
  > "$WM_ROOT/logs/worker.log" 2>&1 &

echo "started: frontend :$HTTP_PORT, logs: $WM_ROOT/logs/{frontend,worker}.log"
EOS
