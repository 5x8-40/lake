# Ascend bring-up（可交付）

在 **vllm-ascend 容器内**编译并安装 [5x8-40/dynamo-ascend](https://github.com/5x8-40/dynamo-ascend)（默认分支 **`feat/ascend-1.4.2-protocol-kunpeng`**），再拉聚合 FE + worker。路径默认相对 lake 仓（`SRC=lake/3rdparty/dynamo-ascend`），可用环境变量覆盖，不依赖个人机器根目录。

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

1. `prepare_src.sh`：checkout **`feat/ascend-1.4.2-protocol-kunpeng`**（1.4.2 交付线，含 `MooncakeConnectorV1` + Kunpeng `generic`）
2. `build_install.sh`：`maturin build --release` → 安装 `ai-dynamo-runtime` wheel → `pip install -e` 安装 Python 包
3. 运行时直接 `python3 -m dynamo.*`，不维护 `.pth` 注入

aarch64：`feat/ascend-1.4.2-protocol-kunpeng` 的 `.cargo` 已用 `target-cpu=generic`（避免 Kunpeng SIGILL）。

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

常用变量：`SRC`、`REF`（默认 `feat/ascend-1.4.2-protocol-kunpeng`）、`REPO`、`NAME`、`MODEL`、`MODEL_HOST_DIR`、`LOGDIR`、`PROXY`、`ROUTER_MODE`。
