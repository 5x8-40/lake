# E 线：引擎适配（dynamo-ascend）

> 环境：Atlas A2（8 卡）/ openEuler / Kunpeng 920（aarch64）；镜像 `vllm-ascend:v0.26.0rc1`（Ubuntu 变体）。
> **版本锁定：dynamo `release/1.4.2` ↔ vllm-ascend 0.26.0rc1**（main/1.5.0 与 vllm 0.26 不兼容，不用）。
> 安装与拉起的**定稿流程**见 [bringup.md](bringup.md)（聚合）与 [pd-mooncake.md](pd-mooncake.md)（PD/Store/卸载/跨机），脚本在 [`scripts/dynamo-ascend/`](../../scripts/dynamo-ascend/)。本文只保留任务追踪、本机实测补充与进展日志。
> 环境事实（镜像内容 / 挂载 / apt 源）见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md)。

## 任务表

| 里程碑 | 任务 | 说明 | 状态 |
|--------|------|------|------|
| **E1 单机聚合跑通**（2026-09-20 完成） | E1.1 基础镜像 | `vllm serve` 单独验证引擎出 token | 完成（Qwen3.8-27B，DP2×TP4） |
| | E1.2 Dynamo 装入 NPU 环境 | 源码 1.4.2，容器内 editable 安装 | 完成（PR #43 固化为 `build_install.sh`） |
| | E1.3 胶水层兼容性摸底 | import 面走查，结论：零代码改动 | 完成（见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md)） |
| | E1.4 拉起 worker | frontend + `dynamo.vllm` 同容器 | 完成（单机 file discovery；etcd 跨机） |
| | E1.5 全链路连通 | chat 出 token（含 MTP + cudagraph + 256K 全配置） | 完成 |
| **E1.6 KV 事件链** | E1.6 KV-aware 选路补验 | 事件链已随 PR #43 验证（指标 ①-③ 通）；④ 前缀命中率实测待补 | 基本完成 |
| 增强 | EX1 SGLang NPU 第二后端 | SGLang 主干自带 NPU 支持，vllm-ascend 路线跑通后接入（远期） | 未开始 |
| 增强 | EX2 ModelExpress 权重加速 | NPU 间流式传权重；前期共享存储兜底（远期） | 未开始 |

## 安装：本机实测补充（正典流程在 bringup.md）

`build_install.sh` 已自动化主流程（maturin release + editable）。以下是本机踩过、脚本未必覆盖的坑：

- **新容器先拿依赖**：`pip install ai-dynamo==1.4.2`（装依赖），再做 editable 替换；装完确认 vllm 没被顶（`python3 -c "import vllm; print(vllm.__version__)"` 应为 0.26.0）。`--no-deps` 在全新容器上不够。
- **crates.io 华为内网镜像**（2026-09-21 实测可用；另需 `echo "insecure" >> ~/.curlrc`）：

```toml
[target.aarch64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=generic", "-C", "force-frame-pointers=yes", "--cfg", "tokio_unstable"]

[net]
git-fetch-with-cli = true
[http]
check-revoke = false
[source.crates-io]
replace-with = 'innersource'
[source.innersource]
registry = 'https://szv-open.codehub.huawei.com/rust/crates.io-index.git'

# 外网备选: rsproxy-sparse = "sparse+https://rsproxy.cn/index/"
```

- **网络受限时的兜底**：拿别人编好的 `ai_dynamo_runtime` wheel；只有裸 `_core.abi3.so` 时直接拷进 site-packages 的 `dynamo/` 目录。注意 **x86 编的 .so 不能用于 aarch64**（abi3 是 Python ABI，不是 CPU 架构）。
- 两个 `dynamo` 目录（`components/src` 与 `lib/bindings/python/src`）都是 namespace 包（无 `__init__.py`），editable 安装后自动合并：组件代码走源码树，`_core.abi3.so` 走 site-packages。
- 改 Rust 代码后重编：先 `cargo clean --manifest-path lib/bindings/python/Cargo.toml`（incremental 可能不链新符号），再重跑 maturin。
- **禁止** `pip install ai-dynamo` / 带 `[vllm]` extra：PyPI wheel 有 Illegal instruction 风险，且 extra 会拉 CUDA 生态顶掉镜像内 vllm。

## 拉起：本机验证过的变体配置

正典脚本：`scripts/dynamo-ascend/start.sh`（聚合）/ `start_pd.sh`（PD）。本机 E1 验证用的是如下变体配置，与正典脚本的差异供调参参考：

- **file discovery**（`--discovery-backend file`，单机免 etcd；跨机才换 etcd）；
- worker 全配置：DP2×TP4、MTP（`qwen3_next_mtp`）、cudagraph `FULL_DECODE_ONLY`、`--max-model-len 262144`；
- `--kv-events-config` 与 `DYN_SYSTEM_PORT=8782`（正典脚本已内置，此处同源）。

## 已知坑

| 现象 | 原因 | 处理 |
|------|------|------|
| `import dynamo._core` Illegal instruction | PyPI aarch64 wheel target-cpu 基线过新 | 源码编（`target-cpu=generic`，`release/1.4.2` 已带）或拷贝 .so；**不要 pip 装 ai-dynamo** |
| pip 装 dynamo 后 vllm 被替换 | `[vllm]` extra 拉 CUDA 生态顶掉镜像内 vllm | 卸载，改用 editable 安装（`pip install -e . --no-deps`） |
| file discovery 日志刷 stream end | inotify watch 上限 | `sysctl -w fs.inotify.max_user_watches=1048576` |

## E1.6 KV 事件链

- **关键坑**：vLLM 的 `enable_kv_cache_events` 默认 `False`，不显式写 `true` 事件一律不发（dynamo 侧日志只会打一条 warning）；不给 `--kv-events-config` 则发布器不建。传输不用配：file discovery 下 event plane 自动走本地 ZMQ，无需 NATS。
- 验证四 checkpoint：① **启动早期**（参数解析时）worker 日志出现 `Using kv_events_config ... enable_kv_cache_events=True ... (use_kv_events=True)`——若为 `None`/`False` 说明参数没生效；② **模型加载完之后**（publisher 在 engine 初始化后才装配，加载期间 grep 不到属正常）出现 `KV event publisher for dp_rank=N subscribing to vLLM at tcp://127.0.0.1:5557+N`；③ **frontend 收到事件**：`curl -s :8000/metrics | grep -E "ingress|kv_hit"`——`router_kv_zmq_ingress_sources`>0（已订阅事件源）、`router_kv_zmq_ingress_batches_total` 持续增长（事件在流）；worker 侧 `curl -s :8782/metrics | grep kv_publisher`；④ 同一长前缀（≥16 token）请求发两次，第二次应命中前缀 KV（`router_kv_hit_rate` 非零、APC 命中率 >0、TTFT 明显下降）。DP2 下事件按 dp_rank 发布，router 靠事件做 rank 级亲和。
- 状态：①-③ 与指标可见性已随 PR #43 验证（2026-09-21）；④ 前缀命中率实测待补。

## 进展日志

| 日期 | 进展 |
|------|------|
| 2026-09-17 | 计划建立 |
| 2026-09-18 | E1.1–E1.3 完成；镜像换 Ubuntu 变体 |
| 2026-09-20 | E1.4/E1.5 本机完成（全配置 + file discovery，单机免 etcd）；**版本锁定 1.4.2**（main 与 vllm 0.26 不兼容）；runtime .so 走拷贝绕行 |
| 2026-09-20 | 安装改容器内 editable（`pip install -e . --no-deps`），不再手工管 `.pth`；E1.6 开验：修正事件参数（vLLM 默认 `enable_kv_cache_events=False`，须显式开） |
| 2026-09-24 | PR #43 合入：交付基线 `release/1.4.2`（dynamo-ascend PR #3 已并入），安装/拉起正典化到 `scripts/dynamo-ascend/`；E1.6 事件链随之验证（指标 ①-③）；本目录 `scripts/ascend/` 下线，本文改为纯任务追踪 + 实测补充 |
