# Ascend PD + Mooncake（1.4.2 最小协议补丁）

基线：`release/1.4.2` + 一行/一块协议注册（`MooncakeConnectorV1` → `NixlConnectorProtocol`）。**不用** dynamo-ascend 1.5 整树。

## 代码缺口

| 文件 | 改动 |
|------|------|
| `kv_connector_protocols.py` | 注册 `MooncakeConnectorV1` |
| 对应 unit test | 更新 registry keys 断言 |

补丁在 [`../../scripts/dynamo-ascend/patches/0001-mooncake-connector-v1-protocol.patch`](../../scripts/dynamo-ascend/patches/0001-mooncake-connector-v1-protocol.patch)；`prepare_src.sh` / `apply_protocol_patch.sh` 自动打上。

不要用上游 GPU `MooncakeConnector`（Ascend KV 无 `data_ptr()`）。

1.4.2 CLI 注意：有 `--request-plane`，**没有** `--response-plane`（那是更新树里的旗标）。

## MultiConnector 分工

| 子连接器 | 作用 | 依赖 |
|----------|------|------|
| `MooncakeConnectorV1` | P↔D **KV 传输**（唯一产出 PD `kv_transfer_params` 的子项） | — |
| `AscendStoreConnector` | **前缀 Store**（`backend=mooncake`） | 必须同起 **`mooncake_master`**；单独开 Store 或单独开 master 都没用 |
| `OffloadingConnector` | **NPU→CPU KV 卸载**（本镜像用 `AscendSimpleCPUOffloadConnector`） | 默认开（`ENABLE_KV_OFFLOAD=1`）；`ENABLE_KV_OFFLOAD=0` 可关 |

关闭卸载：`ENABLE_KV_OFFLOAD=0 bash scripts/dynamo-ascend/start_pd.sh`。  
调 CPU 池：`OFFLOAD_CPU_BYTES`（默认 8GiB）。

## 单机 PD

```bash
cd /path/to/lake
export WM_ROOT=/data/wm
bash scripts/dynamo-ascend/prepare_src.sh          # checkout 1.4.2 + 打补丁
WITH_NPU=0 bash scripts/dynamo-ascend/start_docker.sh
PROXY=$PROXY bash scripts/dynamo-ascend/build_install.sh
# 需要 NPU 时 commit 镜像后 WITH_NPU=1 重建容器，或直接用已有 lake-test
bash scripts/dynamo-ascend/start_etcd.sh
bash scripts/dynamo-ascend/install_src.sh           # 刷新 editable + 断言协议
RESTART=1 bash scripts/dynamo-ascend/start_pd.sh
curl -s localhost:8000/v1/models
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

默认：`ROUTER_MODE=kv` + worker `--kv-events-config`（ZMQ）。关闭：`ROUTER_MODE=round-robin`。

布局默认 Prefill NPU `0-3` TP4、Decode `4-7` TP4。日志：`$WM_ROOT/dynamo-lake-logs/`。

## KV-aware router

`start_pd.sh` 已打开：

- FE：`--router-mode kv`
- Worker：`--kv-events-config` + `DYN_SYSTEM_PORT`（`/metrics`）

探针：

```bash
curl -s localhost:8000/metrics | grep -E 'router_kv_|dynamo_component' || true
curl -s localhost:8782/metrics | grep kv_publisher || true
```

## 跨机 2P2D

```bash
# P-host
export HOST_IP=<p-ip> ETCD_ENDPOINTS=http://$HOST_IP:2379 MC_MASTER_ADDRESS=$HOST_IP
RESTART=1 ROLE=p bash scripts/dynamo-ascend/start_pd_multi.sh
# D-host
export HOST_IP=<d-ip> ETCD_ENDPOINTS=http://<p-ip>:2379 MC_MASTER_ADDRESS=<p-ip>
RESTART=1 ROLE=d bash scripts/dynamo-ascend/start_pd_multi.sh
```

`start_etcd.sh` 支持 `ADVERTISE_CLIENT_URL`（多机脚本会设成 `http://$HOST_IP:2379`）。

## 验证记录（本机）

- 日期：2026-09-21
- 基线：`release/1.4.2` + `0001-mooncake-connector-v1-protocol.patch`
- 容器：`vllm-ascend-wm`（`NAME` 可改；同机勿让两个容器同时占 NPU）
- `RESTART=1 bash scripts/dynamo-ascend/start_pd.sh`
- `/v1/models` → `qwen`；`/v1/chat/completions` → 返回 completion tokens
- FE `--router-mode kv`；`/metrics` 有 `dynamo_component_kv_cache_*`；worker `:8782/metrics` 有 `kv_publisher_*`
- Worker 日志：`AscendMultiConnector`（vllm-ascend 对 MultiConnector 的包装）

## 跨机验证（2026-09-22）

- P=`7.242.106.153`，D=`7.242.108.153`，1P1D TP4（`P_NPU_GROUPS`/`D_NPU_GROUPS=0,1,2,3`）
- etcd advertise：`http://7.242.106.153:2379`；`MC_MASTER_ADDRESS=7.242.106.153`；`PREFER_SAME_NODE=false`
- 容器需挂 `hccn_tool` / `npu-smi` / `hccn.conf`（`start_docker.sh` 已补），否则 Ascend 直连 KV 会 `Mooncake transfer failed, ret: -1`
- Decode `kv_port` 默认基址 `20101`（避开 Prefill TP 占用的 `20001..20001+TP-1`）
- 结果：`/v1/models` → `qwen`；跨机 chat HTTP 200 出 token

## 脚本

| 脚本 | 作用 |
|------|------|
| `apply_protocol_patch.sh` | 打 `MooncakeConnectorV1` 补丁 |
| `install_src.sh` | 容器内挂接 SRC 并断言协议 |
| `start_pd.sh` | 单机 PD + Mooncake + KV router |
| `start_pd_multi.sh` | `ROLE=p\|d` 跨机 |

相对 bring-up（`start.sh` 聚合）：本路径是 Prefill/Decode 分离 + MultiConnector。
