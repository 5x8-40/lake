# Ascend bring-up（可交付）

在 **vllm-ascend 容器内**编译并安装 [5x8-40/dynamo-ascend](https://github.com/5x8-40/dynamo-ascend)（默认分支 **`release/1.4.2`**），再拉聚合 FE + worker。路径默认相对 lake 仓（`SRC=lake/3rdparty/dynamo-ascend`），可用环境变量覆盖，不依赖个人机器根目录。

> 需要 NPU。frontend 单测可用 mock；本路径不做「无 NPU 再 docker commit」的绕路。

## 步骤

```bash
cd /path/to/lake
# 可选：PROXY / SSL_CERT_FILE / MODEL_HOST_DIR=/path/to/weights

bash scripts/dynamo-ascend/prepare_src.sh     # clone 5x8-40/dynamo-ascend → 3rdparty/
bash scripts/dynamo-ascend/start_docker.sh    # 带 NPU 的常驻容器
PROXY=$PROXY bash scripts/dynamo-ascend/build_install.sh   # maturin --release + editable
bash scripts/dynamo-ascend/start_etcd.sh
bash scripts/dynamo-ascend/start.sh          # 聚合 FE+worker；默认 --router-mode kv + kv-events
curl -s localhost:8000/v1/models
# KV 路由探针（与 PD 一致）
curl -s localhost:8000/metrics | grep -E 'router_kv_|dynamo_component_kv_cache' || true
curl -s localhost:8782/metrics | grep kv_publisher || true
```

PD / 跨机 / Store / 卸载见 [pd-mooncake.md](pd-mooncake.md)。

## 安装流程（正常路径）

1. `prepare_src.sh`：checkout **`release/1.4.2`**（1.4.2 交付线，含 `MooncakeConnectorV1` + Kunpeng `generic`）
2. `build_install.sh`：`maturin build --release` → 安装 `ai-dynamo-runtime` wheel → `pip install -e` 安装 Python 包
3. 运行时直接 `python3 -m dynamo.*`，不维护 `.pth` 注入

aarch64：`release/1.4.2` 的 `.cargo` 已用 `target-cpu=generic`（避免 Kunpeng SIGILL）。

## 实测补充（A2 / 华为内网）

- **新容器先拿依赖**：先 `pip install ai-dynamo==1.4.2` 再做 editable 替换（`--no-deps` 在全新容器上不够）；装完确认 `vllm.__version__` 仍是 0.26.0。
- **禁止** `pip install ai-dynamo`（PyPI wheel 有 SIGILL 风险）与 `[vllm]` extra（拉 CUDA 生态顶掉镜像内 vllm）。
- crates.io 华为内网镜像（2026-09-21 实测；另需 `echo "insecure" >> ~/.curlrc`）：

```toml
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

- 网络受限兜底：用成品 `ai_dynamo_runtime` wheel；只有裸 `_core.abi3.so` 时直接拷进 site-packages 的 `dynamo/`。x86 编的 .so 不能用于 aarch64。
- 两个 `dynamo` 目录（`components/src`、`lib/bindings/python/src`）均为 namespace 包，editable 安装后自动合并。
- 改 Rust 后重编先 `cargo clean --manifest-path lib/bindings/python/Cargo.toml`（incremental 可能不链新符号）。
- file discovery 日志刷 stream end → `sysctl -w fs.inotify.max_user_watches=1048576`。
- 本机 E1 验证变体：file discovery（单机免 etcd）+ MTP + cudagraph `FULL_DECODE_ONLY` + `--max-model-len 262144`，DP2×TP4。

## 脚本

| 脚本 | 作用 |
|------|------|
| `prepare_src.sh` | clone/checkout dynamo-ascend 到 `$SRC` |
| `start_docker.sh` | 常驻 vllm-ascend 容器（NPU + 必要驱动挂载） |
| `build_install.sh` | 容器内 release 编译并安装 |
| `verify_protocol.sh` | 容器内断言 `MooncakeConnectorV1`（不安装） |
| `start_etcd.sh` | discovery（可设 `ADVERTISE_CLIENT_URL`） |
| `start.sh` | FE + 聚合 worker（默认 KV router + kv-events + `DYN_SYSTEM_PORT`） |
| `start_pd.sh` / `start_pd_multi.sh` | PD 路径 |

常用变量：`SRC`、`REF`（默认 `release/1.4.2`）、`REPO`、`NAME`、`MODEL`、`MODEL_HOST_DIR`、`LOGDIR`、`PROXY`、`ROUTER_MODE`。
