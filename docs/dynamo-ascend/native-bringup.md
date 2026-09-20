# Ascend 原生 bring-up：容器内 frontend + dynamo.vllm（etcd）

> E 线 bring-up 实测记录（2026-09-18 首验，910B3 8 卡；2026-09-20 本机复验，dynamo 1.4.2 + 全配置 + file discovery）。用 `vllm-ascend:v0.26.0rc1` 把 **Dynamo frontend + `dynamo.vllm` worker 全部跑进同一容器**，`curl :8000` chat 验通。  
> 路径以本机 `WM_ROOT=/data/wm` 为例；脚本在 [`scripts/ascend/`](scripts/ascend/)。

**目标模型**：`/data/models/Qwen3.8-27B`，served name `qwen`，TP4 × DP2  
**结果**：`/v1/models` 注册 `qwen`；`/v1/chat/completions` 返回 token

---

## 0. 最终形态

```text
宿主机
  ├─ 源码编译 Dynamo runtime（_core.abi3.so）
  ├─ 常驻 etcd（dynamo-etcd，--net=host，:2379）   ← 跨机 discovery
  └─ 常驻容器 vllm-ascend-wm（v0.26.0rc1，--net=host，挂 /data + NPU）
        ├─ python3 -m dynamo.frontend   :8000  --discovery-backend etcd
        └─ python3 -m dynamo.vllm       8×NPU  --discovery-backend etcd
```

要点：

- **版本锁定**：dynamo **1.4.2** ↔ vllm-ascend **0.26.0rc1**。dynamo main（1.5.0）与 vllm 0.26 不兼容（胶水层需侵入式修改），不要用。
- **推理栈**用昇腾官方 `vllm-ascend` 镜像（CANN / `torch_npu` 已齐）。
- **编排层**在宿主机源码编译 Dynamo，经 `.pth` 注入容器 Python；不要装 PyPI 的 `ai-dynamo` wheel（[vllm] extra 会拉 CUDA vLLM 顶掉镜像，aarch64 wheel 在部分鲲鹏主机 Illegal instruction）。
- Discovery：单机用 **file**（免 etcd）；跨机用 **etcd**（`ETCD_ENDPOINTS`）。
- Request/response plane 用 **tcp** 时不依赖 NATS（event plane 默认可走 zmq）。
- Worker 入口：`python -m dynamo.vllm`（无 OpenAI HTTP bridge）。
- KV 事件链（`--router-mode kv` + `--kv-events-config`）本路径未开，作为 E1.6 补齐（见 [e-line.md](e-line.md)）。

脚本：

| 脚本 | 作用 |
|------|------|
| [`start_docker_va.sh`](scripts/ascend/start_docker_va.sh) | 常驻容器 `vllm-ascend-wm` |
| [`start_etcd.sh`](scripts/ascend/start_etcd.sh) | 常驻单节点 etcd |
| [`start_dynamo_va_native.sh`](scripts/ascend/start_dynamo_va_native.sh) | 容器内 FE + worker（默认 etcd），写 `.pth` |

---

## 1. 环境前提

- 机器：aarch64 Kunpeng + Ascend 驱动 / `npu-smi`
- Docker 能挂 `/dev/davinci*`、`davinci_manager`、`devmm_svm`、`hisi_hdc`
- 镜像：`quay.io/ascend/vllm-ascend:v0.26.0rc1`（Ubuntu 变体；见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md)）
- 模型在 `/data/...`（容器 `-v /data:/data`）
- 出网常需 HTTP 代理；crates.io 可用 rsproxy

---

## 2. 宿主机：源码编译 Dynamo

### 2.1 依赖

- Rust（实测 `rustc 1.96`）+ `maturin`
- Python **3.12**（与容器一致；`uv` 可装）
- `clang` / `cmake` / `hwloc` / `protoc`（实测 `protoc 28.3`）
- `uv`

### 2.2 拉仓

```bash
export WM_ROOT=/data/wm
git clone https://github.com/5x8-40/dynamo-ascend.git $WM_ROOT/dynamo-ascend
cd $WM_ROOT/dynamo-ascend
git checkout v1.4.2    # 版本锁定:main(1.5.0)与 vllm 0.26 不兼容
```

### 2.3 aarch64：`target-cpu=generic`

官方 PyPI aarch64 wheel 在部分鲲鹏主机上 **Illegal instruction**。`.cargo/config.toml`：

```toml
[target.aarch64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=generic", "-C", "force-frame-pointers=yes", "--cfg", "tokio_unstable"]

# 可选:crates.io 国内镜像(网络受限时)
[source.crates-io]
replace-with = 'rsproxy-sparse'
[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"
```

代理走环境变量，**不要把代理密码写进仓库**。

网络实在不通时的绕行：拷贝他人编译的 `_core.abi3.so` 放入 `lib/bindings/python/src/dynamo/`（runtime 产物需与组件版本配套，本文档配套 v1.4.2），然后跳过 §2.4 直接进 §4。

### 2.4 编译

仓库是两个独立包：根包 `ai-dynamo`（hatchling，纯 Python，构建钩子只写版本文件，**不编 Rust**）；Rust runtime 是独立包 `ai-dynamo-runtime`（`lib/bindings/python/`，maturin backend，独立 cargo workspace）。所以必须分两步：

```bash
cd $WM_ROOT/dynamo-ascend
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install 'maturin[patchelf]'

# ① 编 Rust runtime → 产出 lib/bindings/python/src/dynamo/_core.abi3.so
cd lib/bindings/python && maturin develop --uv && cd ../../..

# ② 装根包(纯 Python;① 已装好 ai-dynamo-runtime==1.5.0,依赖被满足,不会从 PyPI 拉 wheel)
uv pip install -e '.[mocker]'
```

成功标志：`import dynamo._core`；`python -m dynamo.frontend --help`。  
本机 `_core.abi3.so` ~1.9GB（带 debug）属正常。

不用 uv 的等价写法：`python3.12 -m venv .venv`（宿主机需自装 python3.12；产物是 abi3，≥3.10 即可）+ `pip install ...` + `maturin develop`（去掉 `--uv`）。

连 venv 也不想用（只要产物、不在宿主机跑 dynamo）：`maturin build` 不需要 venv，但只产 wheel，需把 `.so` 解进源码树（`.pth` 注入读的是源码树）：

```bash
cd lib/bindings/python
maturin build
unzip -o target/wheels/ai_dynamo_runtime-*.whl 'dynamo/_core*' -d src/
```

### 2.5 修改后重编

Rust 编译只由 `lib/bindings/python` 下的 maturin 触发（根包安装命令不编 Rust）：

```bash
cd $WM_ROOT/dynamo-ascend/lib/bindings/python
source ../../../.venv/bin/activate
cargo clean                # 仅当改了 .cargo/config.toml(rustflags/target-cpu):cargo 不跟踪 flag 变化,不清则复用旧产物
maturin develop --uv
```

- 只改 Python 代码（`components/` 等）：不用编，`.pth` 直读源码，重启进程即可。
- 容器侧无需重装（`/data` 挂载 + `.pth` 注入，新 `.so` 即刻可见），`RESTART=1 bash scripts/ascend/start_dynamo_va_native.sh` 重启进程即可。

---

## 3. （可选）宿主机 mock 冒烟

不占 NPU，先验证编排：`dynamo.frontend` + `dynamo.mocker`，`--discovery-backend file`，本地 tokenizer 路径 + `HF_HUB_OFFLINE=1`。

---

## 4. 常驻容器

不要用 `docker run --rm -it ... bash`（Ctrl+D 容器就没了）：

```bash
bash scripts/ascend/start_docker_va.sh
# IMAGE=v0.26.0rc1 NAME=vllm-ascend-wm --net=host sleep infinity -v /data:/data
```

**若已有占满 NPU 的 `vllm serve`，先停掉。**

---

## 5. `.pth` 注入容器

容器经 `/data` 看见宿主机源码，但默认 `sys.path` 不含 Dynamo。写入：

```text
.../site-packages/dynamo_ascend_runtime.pth
  → $WM_ROOT/dynamo-ascend/lib/bindings/python/src

.../site-packages/dynamo_ascend_components.pth
  → $WM_ROOT/dynamo-ascend/components/src
```

0.26 镜像 site-packages：`/usr/local/python3.12.13/lib/python3.12/site-packages/`。  
`start_dynamo_va_native.sh` 每次启动会重写。

**注意**：容器里若装过 PyPI 的 `ai-dynamo`/`ai-dynamo-runtime`，先卸载——site-packages 里的已装包优先于 `.pth` 注入的路径，不卸会 shadow 源码版本。

---

## 5.1 etcd

```bash
bash scripts/ascend/start_etcd.sh
export ETCD_ENDPOINTS=http://127.0.0.1:2379
# 跨机：http://<etcd-host-ip>:2379
```

---

## 6. 启动 FE + worker

```bash
bash scripts/ascend/start_etcd.sh
bash scripts/ascend/start_dynamo_va_native.sh
RESTART=1 bash scripts/ascend/start_dynamo_va_native.sh
bash scripts/ascend/start_dynamo_va_native.sh stop
```

关键参数（容器内，2026-09-20 本机实测，dynamo 1.4.2 + 全配置）：

```bash
# 单机用 file discovery,免 etcd;跨机才需要 etcd(见 §5.1)
python3 -m dynamo.frontend --http-port 8000 \
  --discovery-backend file > frontend.log 2>&1 &

export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
python3 -m dynamo.vllm \
  --model /data/models/Qwen3.8-27B --served-model-name qwen \
  --data-parallel-size 2 --tensor-parallel-size 4 \
  --max-num-seqs 64 --max-model-len 256000 --max-num-batched-tokens 16384 \
  --trust-remote-code --enable-prefix-caching --gpu-memory-utilization 0.9 \
  --speculative-config '{"method": "qwen3_next_mtp", "num_speculative_tokens": 3, "enforce_eager": true}' \
  --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \
  --additional-config '{"enable_cpu_binding":true}' \
  --discovery-backend file --disaggregation-mode agg
```

日志：`$WM_ROOT/dynamo-native-logs/`。加载模型数分钟属正常。

---

## 7. 验收

```bash
curl -s localhost:8000/v1/models
curl -s localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}],"max_tokens":32}'
```

---

## 8. 踩坑

| 现象 | 处理 |
|------|------|
| aarch64 wheel Illegal instruction | `target-cpu=generic` 源码编译 |
| `--rm -it` + Ctrl+D | 改用 `sleep infinity` 常驻 |
| file discovery stream end | 提高 `fs.inotify.max_user_watches`；或改用 etcd |
| NPU 被旧 serve / 孤儿 `VLLM::*` 占满 | 先停干净再起；脚本 `stop` 会 `pkill -9 -f 'VLLM::'` |
| etcd FE + file worker → models 空 | discovery 后端必须一致 |
| CuPy / NIXL 警告 | agg 单机可先忽略 |

---

## 9. 最短复现（已有编译产物）

```bash
export WM_ROOT=/data/wm
bash scripts/ascend/start_docker_va.sh
bash scripts/ascend/start_etcd.sh
bash scripts/ascend/start_dynamo_va_native.sh
curl -s localhost:8000/v1/models
```
