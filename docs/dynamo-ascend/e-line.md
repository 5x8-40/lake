# E 线：引擎适配（dynamo-ascend）

> 环境：Atlas A2（8 卡）/ openEuler / Kunpeng 920（aarch64）；镜像 `vllm-ascend:v0.26.0rc1`（Ubuntu 变体）。
> **版本锁定：dynamo 1.4.2 ↔ vllm-ascend 0.26.0rc1**（main/1.5.0 与 vllm 0.26 不兼容，需侵入式修改，不用）。
> 形态：frontend + `dynamo.vllm` worker 与 vllm-ascend 同容器；单机 file discovery（免 etcd），跨机才用 etcd。
> 环境事实（镜像内容 / 挂载 / apt 源）见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md)。

## 任务表

| 里程碑 | 任务 | 说明 | 状态 |
|--------|------|------|------|
| **E1 单机聚合跑通**（2026-09-20 完成） | E1.1 基础镜像 | `vllm serve` 单独验证引擎出 token | 完成（Qwen3.8-27B，DP2×TP4） |
| | E1.2 Dynamo 装入 NPU 环境 | 源码 v1.4.2，容器内 editable 安装（runtime .so 可拷贝） | 完成 |
| | E1.3 胶水层兼容性摸底 | import 面走查，结论：零代码改动 | 完成（见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md)） |
| | E1.4 拉起 worker | frontend + `dynamo.vllm` 同容器，file discovery | 完成 |
| | E1.5 全链路连通 | chat 出 token（含 MTP + cudagraph + 256K 全配置） | 完成 |
| **E1.6 KV 事件链** | E1.6 KV-aware 选路补验 | 脚本已内置事件参数；验证方式见下节 | 进行中 |
| 增强 | EX1 SGLang NPU 第二后端 | SGLang 主干自带 NPU 支持，vllm-ascend 路线跑通后接入（远期） | 未开始 |
| 增强 | EX2 ModelExpress 权重加速 | NPU 间流式传权重；前期共享存储兜底（远期） | 未开始 |

## 一次性安装（容器内，editable）

源码树在宿主机，经 `/data` 挂载进容器；安装在容器内做，editable 方式（改 Python 代码即时生效，`.pth` 由 pip 自动管理）：

```bash
# 宿主机:拉仓 + 锁定版本
export WM_ROOT=/data/wm
git clone https://github.com/5x8-40/dynamo-ascend.git $WM_ROOT/dynamo-ascend
cd $WM_ROOT/dynamo-ascend && git checkout v1.4.2

# 容器内:
docker exec -it vllm-ascend-wcd bash
cd /data/wm/dynamo-ascend

# ⓪ 新容器先拿依赖(老容器已装过 ai-dynamo 则跳过):
pip install ai-dynamo==1.4.2
#   装完确认 vllm 没被顶:python3 -c "import vllm; print(vllm.__version__)" 应为 0.26.0

# ① Rust runtime(二选一)
# A. 从头编(推荐;缺 gcc/protoc 等系统工具时按报错 apt 装,源见环境文档):
curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y
source ~/.cargo/env
pip install 'maturin[patchelf]'
cd lib/bindings/python && maturin build --release && cd ../..
# B. 网络受限:拿别人编好的 ai_dynamo_runtime wheel(或裸 _core.abi3.so)

# ② 装本地 runtime(覆盖 ⓪ 拉到的 PyPI wheel——Illegal instruction 风险源):
pip install --force-reinstall --no-deps \
  lib/bindings/python/target/release/wheels/ai_dynamo_runtime-*.whl
#   (B 路线只有裸 .so 时:直接拷进 site-packages 的 dynamo/ 目录,跳过本步)

# ③ 根包 editable 安装(自动替换 ⓪ 装的 ai-dynamo;--no-deps 不动镜像里 pin 好的包)
pip install -e . --no-deps
# import 若报缺包(如 kubernetes),单独 pip install 补,不要全量装依赖
```

③ 已由 `start_va_dynamo.sh` 自动化（检测到未安装时触发）；⓪①② 需手工做一次（新容器重建后要重做）。

- 仓库根 `.cargo/config.toml` 需配 `target-cpu=generic`（否则部分鲲鹏主机 `import dynamo._core` 报 Illegal instruction）；crates.io 镜像二选一（华为内网用 innersource，已验证；外网用 rsproxy）：

```toml
[target.aarch64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=generic", "-C", "force-frame-pointers=yes", "--cfg", "tokio_unstable"]

# 华为内网(2026-09-21 实测可用;另需 echo "insecure" >> ~/.curlrc)
[net]
git-fetch-with-cli = true
[http]
check-revoke = false
[source.crates-io]
replace-with = 'innersource'
[source.innersource]
registry = 'https://szv-open.codehub.huawei.com/rust/crates.io-index.git'

# 外网备选:
# [source.crates-io]
# replace-with = 'rsproxy-sparse'
# [source.rsproxy-sparse]
# registry = "sparse+https://rsproxy.cn/index/"
```

- 两个 `dynamo` 目录（`components/src` 与 `lib/bindings/python/src`）都是 namespace 包（无 `__init__.py`），editable 安装后自动合并：组件代码走源码树，`_core.abi3.so` 走 site-packages。
- 改 Rust 代码后重编：先 `cargo clean --manifest-path lib/bindings/python/Cargo.toml`（incremental 可能不链新符号），再重跑 ①A。
- **禁止** `pip install ai-dynamo` / 带 `[vllm]` extra：PyPI wheel 有 Illegal instruction 风险，且 extra 会拉 CUDA 生态顶掉镜像内 vllm。

## 拉起（两个脚本）

脚本在 [scripts/ascend/](scripts/ascend/)：

```bash
./scripts/ascend/start_etcd.sh              # ① etcd 容器(仅跨机 discovery 需要;单机不用跑)
./scripts/ascend/start_va_dynamo.sh         # ② 建 vllm-ascend 容器 + 容器内拉起 frontend/worker
./scripts/ascend/start_va_dynamo.sh stop    # 停容器内 dynamo 进程
```

② 做的事：容器不存在则创建（8 卡挂载 + `--net=host`）；若 dynamo 未装则做 editable 安装（含旧手工 `.pth` 迁移、`.so` 兜底拷贝）；后台起 frontend（file discovery）与 worker（agg 模式，全配置）；日志在 `$WM_ROOT/logs/`。

## 验证

```bash
curl -s http://127.0.0.1:8000/v1/models    # 应见 "qwen"
curl -s http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hello"}],"max_tokens":16}'
```

## 已知坑

| 现象 | 原因 | 处理 |
|------|------|------|
| `import dynamo._core` Illegal instruction | PyPI aarch64 wheel target-cpu 基线过新 | 源码编（`target-cpu=generic`）或拷贝 .so；**不要 pip 装 ai-dynamo** |
| pip 装 dynamo 后 vllm 被替换 | `[vllm]` extra 拉 CUDA 生态顶掉镜像内 vllm | 卸载，改用 editable 安装（`pip install -e . --no-deps`） |
| file discovery 日志刷 stream end | inotify watch 上限 | `sysctl -w fs.inotify.max_user_watches=1048576`（脚本已带） |

## E1.6 KV 事件链（进行中）

- 脚本已内置参数：frontend `--router-mode kv` + worker `--kv-events-config '{"enable_kv_cache_events": true, "publisher": "zmq", "topic": "kv-events"}'`，重跑 `start_va_dynamo.sh` 即开验。
- **关键坑**：vLLM 的 `enable_kv_cache_events` 默认 `False`，不显式写 `true` 事件一律不发（dynamo 侧日志只会打一条 warning）；不给 `--kv-events-config` 则发布器不建。传输不用配：file discovery 下 event plane 自动走本地 ZMQ，无需 NATS。
- 验证：① **启动早期**（参数解析时）worker 日志出现 `Using kv_events_config ... enable_kv_cache_events=True ... (use_kv_events=True)`——若为 `None`/`False` 说明参数没生效；② **模型加载完之后**（publisher 在 engine 初始化后才装配，加载期间 grep 不到属正常）出现 `KV event publisher for dp_rank=N subscribing to vLLM at tcp://127.0.0.1:5557+N`；③ **frontend 收到事件**：`curl -s :8000/metrics | grep -E "ingress|kv_hit"`——`router_kv_zmq_ingress_sources`>0（已订阅事件源）、`router_kv_zmq_ingress_batches_total` 持续增长（事件在流）；worker 侧事件计数在 `curl -s :8782/metrics | grep kv_publisher`（脚本已带 `DYN_SYSTEM_PORT=8782`）；④ 同一长前缀（≥16 token）请求发两次，第二次应命中前缀 KV（`router_kv_hit_rate` 非零、APC 命中率 >0、TTFT 明显下降）。DP2 下事件按 dp_rank 发布，router 靠事件做 rank 级亲和。

## 进展日志

| 日期 | 进展 |
|------|------|
| 2026-09-17 | 计划建立 |
| 2026-09-18 | E1.1–E1.3 完成；镜像换 Ubuntu 变体 |
| 2026-09-20 | E1.4/E1.5 本机完成（全配置 + file discovery，单机免 etcd）；**版本锁定 1.4.2**（main 与 vllm 0.26 不兼容）；runtime .so 走拷贝绕行 |
| 2026-09-20 | 安装改容器内 editable（`pip install -e . --no-deps`），不再手工管 `.pth`；E1.6 开验：修正事件参数（vLLM 默认 `enable_kv_cache_events=False`，须显式开），参数已入脚本 |
