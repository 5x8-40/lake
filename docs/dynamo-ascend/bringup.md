# Ascend bring-up（可交付）

在 **vllm-ascend 容器内**编译并安装 [5x8-40/dynamo-ascend](https://github.com/5x8-40/dynamo-ascend)，再拉聚合 FE + worker。路径默认相对 lake 仓（`SRC=lake/3rdparty/dynamo-ascend`），可用环境变量覆盖，不依赖个人机器根目录。

> 需要 NPU。frontend 单测可用 mock；本路径不做「无 NPU 再 docker commit」的绕路。

## 步骤

```bash
cd /path/to/lake
# 可选：PROXY / SSL_CERT_FILE / MODEL_HOST_DIR=/path/to/weights

bash scripts/dynamo-ascend/prepare_src.sh     # clone 5x8-40/dynamo-ascend → 3rdparty/
bash scripts/dynamo-ascend/start_docker.sh    # 带 NPU 的常驻容器
PROXY=$PROXY bash scripts/dynamo-ascend/build_install.sh   # maturin --release + editable
bash scripts/dynamo-ascend/start_etcd.sh
bash scripts/dynamo-ascend/start.sh
curl -s localhost:8000/v1/models
```

PD / KV / 跨机 / 卸载见 [pd-mooncake.md](pd-mooncake.md)。

## 安装流程（正常路径）

1. `prepare_src.sh`：checkout dynamo-ascend（须含 `MooncakeConnectorV1` 协议，见 [dynamo-ascend#2](https://github.com/5x8-40/dynamo-ascend/pull/2)）
2. `build_install.sh`：`maturin build --release` → 安装 `ai-dynamo-runtime` wheel → `pip install -e` 安装 Python 包
3. 运行时直接 `python3 -m dynamo.*`，不维护 `.pth` 注入

若 aarch64 树仍带 `target-cpu=neoverse-n1`，构建脚本会临时改成 `generic`（Kunpeng SIGILL）；长期应在 dynamo-ascend 仓内改 `.cargo`。

## 脚本

| 脚本 | 作用 |
|------|------|
| `prepare_src.sh` | clone/checkout dynamo-ascend 到 `$SRC` |
| `start_docker.sh` | 常驻 vllm-ascend 容器（NPU + 必要驱动挂载） |
| `build_install.sh` | 容器内 release 编译并安装 |
| `install_src.sh` | 断言已安装树含 `MooncakeConnectorV1` |
| `start_etcd.sh` | discovery（可设 `ADVERTISE_CLIENT_URL`） |
| `start.sh` | FE + 聚合 `dynamo.vllm` |
| `start_pd.sh` / `start_pd_multi.sh` | PD 路径 |

常用变量：`SRC`、`REF`（默认协议分支，合入后改 `ascend-dev`）、`REPO`、`NAME`、`MODEL`、`MODEL_HOST_DIR`、`LOGDIR`、`PROXY`。
