# E 线：引擎适配（dynamo-ascend）

> 环境：Atlas A2（8 卡）/ openEuler / Kunpeng 920（aarch64）；镜像 `vllm-ascend:v0.26.0rc1`（Ubuntu 变体）。
> **版本锁定：dynamo 1.4.2 ↔ vllm-ascend 0.26.0rc1**（main/1.5.0 与 vllm 0.26 不兼容，需侵入式修改，不用）。
> 形态：frontend + `dynamo.vllm` worker 与 vllm-ascend 同容器；单机 file discovery（免 etcd），跨机才用 etcd。
> 环境事实（镜像内容 / 挂载 / apt 源）见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md)。

## 任务表

| 里程碑 | 任务 | 说明 | 状态 |
|--------|------|------|------|
| **E1 单机聚合跑通**（2026-09-20 完成） | E1.1 基础镜像 | `vllm serve` 单独验证引擎出 token | 完成（Qwen3.8-27B，DP2×TP4） |
| | E1.2 Dynamo 装入 NPU 环境 | 源码 v1.4.2 + `.pth` 注入（runtime .so 可拷贝） | 完成 |
| | E1.3 胶水层兼容性摸底 | import 面走查，结论：零代码改动 | 完成（见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md)） |
| | E1.4 拉起 worker | frontend + `dynamo.vllm` 同容器，file discovery | 完成 |
| | E1.5 全链路连通 | chat 出 token（含 MTP + cudagraph + 256K 全配置） | 完成 |
| **E1.6 KV 事件链** | E1.6 KV-aware 选路补验 | 已验通路线未开 KV 事件；补验方式见下节 | 未开始 |
| 增强 | EX1 SGLang NPU 第二后端 | SGLang 主干自带 NPU 支持，vllm-ascend 路线跑通后接入（远期） | 未开始 |
| 增强 | EX2 ModelExpress 权重加速 | NPU 间流式传权重；前期共享存储兜底（远期） | 未开始 |

## 一次性安装（宿主机）

```bash
export WM_ROOT=/data/wm
git clone https://github.com/5x8-40/dynamo-ascend.git $WM_ROOT/dynamo-ascend
cd $WM_ROOT/dynamo-ascend && git checkout v1.4.2    # 版本锁定

# 构建环境
uv venv .venv --python 3.12 && source .venv/bin/activate
uv pip install 'maturin[patchelf]'

# ① 编 Rust runtime → 产出 lib/bindings/python/src/dynamo/_core.abi3.so
cd lib/bindings/python && maturin develop --uv && cd ../..
#   网络受限编不了时:拷贝他人编译的 _core.abi3.so 到 lib/bindings/python/src/dynamo/,跳过①

# ② 装根包(纯 Python)
uv pip install -e '.[mocker]'
```

- 仓库根 `.cargo/config.toml` 需配 `target-cpu=generic`（否则部分鲲鹏主机 `import dynamo._core` 报 Illegal instruction）：

```toml
[target.aarch64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=generic", "-C", "force-frame-pointers=yes", "--cfg", "tokio_unstable"]

# 可选:crates.io 国内镜像
[source.crates-io]
replace-with = 'rsproxy-sparse'
[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"
```

- 不用 uv：`cd lib/bindings/python && maturin build --release`（免 venv），把 `target/release/wheels/` 里 wheel 中的 `_core.abi3.so` 解到 `lib/bindings/python/src/dynamo/`；根包用 `pip install -e .`。
- 改 Rust 代码后重编：先 `cargo clean --manifest-path lib/bindings/python/Cargo.toml`（incremental 可能不链新符号），再重跑 ①。
- ③ `.pth` 注入容器由 `start_va_dynamo.sh` 自动做：把 `components/src` 和 `lib/bindings/python/src` 写进容器 site-packages 的 `dynamo-ascend.pth`，容器内 python 直接 import 宿主机源码。

## 拉起（两个脚本）

脚本在 [scripts/ascend/](scripts/ascend/)：

```bash
./scripts/ascend/start_etcd.sh              # ① etcd 容器(仅跨机 discovery 需要;单机不用跑)
./scripts/ascend/start_va_dynamo.sh         # ② 建 vllm-ascend 容器 + 容器内拉起 frontend/worker
./scripts/ascend/start_va_dynamo.sh stop    # 停容器内 dynamo 进程
```

② 做的事：容器不存在则创建（8 卡挂载 + `--net=host`）；写 `.pth` 注入宿主机源码；后台起 frontend（file discovery）与 worker（agg 模式，全配置）；日志在 `$WM_ROOT/logs/`。

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
| pip 装 dynamo 后 vllm 被替换 | `[vllm]` extra 拉 CUDA 生态顶掉镜像内 vllm | 卸载，只用 `.pth` 注入 |
| file discovery 日志刷 stream end | inotify watch 上限 | `sysctl -w fs.inotify.max_user_watches=1048576`（脚本已带） |

## E1.6 KV 事件链（下一任务）

- 已验通的路线没开 KV 事件，router 的 KV-aware 选路没实际走过。
- 补验：worker 加 `--kv-events-config '{"publisher":"zmq","topic":"kv-events"}'`，frontend 加 `--router-mode kv`（1.4.2 实参以 `--help` 为准）。
- 验证：同一长前缀请求发两次，第二次应命中前缀 KV（TTFT 明显下降）。

## 进展日志

| 日期 | 进展 |
|------|------|
| 2026-09-17 | 计划建立 |
| 2026-09-18 | E1.1–E1.3 完成；镜像换 Ubuntu 变体 |
| 2026-09-20 | E1.4/E1.5 本机完成（全配置 + file discovery，单机免 etcd）；**版本锁定 1.4.2**（main 与 vllm 0.26 不兼容）；runtime .so 走拷贝绕行 |
