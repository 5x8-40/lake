# Ascend bring-up（可交付）

单仓假设：机器上只需要 **一个 Dynamo 源码目录**（默认 `/data/wm/dynamo`），在 `vllm-ascend` **容器内**编译并 `pip install`，不用宿主机 `.pth` 注入。

> 公开仓没有 `1.4.7`；1.4 线用 `release/1.4.2`（`REF` 可改）。不要用 `master` / `main`。

## 步骤

```bash
cd /path/to/lake
export WM_ROOT=/data/wm
# 可选代理（crates / rustup / pypi）
# export PROXY=http://user:pass@host:port
# export SSL_CERT_FILE=/path/to/ca-bundle.crt

bash scripts/dynamo-ascend/prepare_src.sh
WITH_NPU=0 bash scripts/dynamo-ascend/start_docker.sh   # 可先无 NPU 编译
PROXY=$PROXY bash scripts/dynamo-ascend/build_install.sh
bash scripts/dynamo-ascend/start_etcd.sh
# 推理需要 NPU：docker commit $NAME img && docker rm -f $NAME
# IMAGE=img WITH_NPU=1 bash scripts/dynamo-ascend/start_docker.sh
bash scripts/dynamo-ascend/start.sh
curl -s localhost:8000/v1/models
```

## 脚本已自动处理的坑

| 坑 | 脚本做法 |
|---|---|
| Kunpeng `SIGILL`（`neoverse-n1`） | 构建前改 `.cargo` → `target-cpu=generic` |
| 缺 `protoc` | 自动装 28.3 到 `/usr/local` |
| 华为 PyPI + 代理超时 | `maturin`/`hatchling` 走 cargo 或 `pypi.org` |
| `maturin develop` 要 venv | 改用 `maturin build` + `pip install` 进系统 Python |
| 旧 `.pth` 注入 | 安装前删除 `dynamo_ascend_*.pth` |

## 脚本一览

| 脚本 | 作用 |
|------|------|
| `prepare_src.sh` | clone/checkout **一个** Dynamo 仓到 `$SRC` |
| `start_docker.sh` | 常驻 `vllm-ascend` 容器（`WITH_NPU=0/1`） |
| `build_install.sh` | 容器内 rustup + maturin release 编译并安装 |
| `start_etcd.sh` | discovery |
| `start.sh` | 容器内 FE + `dynamo.vllm` |

环境变量：`SRC`（默认 `$WM_ROOT/dynamo`）、`REF`（默认 `release/1.4.2`）、`REPO`（默认 `ai-dynamo/dynamo`）、`NAME`、`PROXY`、`SSL_CERT_FILE`。

## 相对旧 PR1

- 旧：宿主机编译 + `.pth` 注入  
- 现：容器内安装；对齐「镜像内含 vllm-ascend + dynamo」
