# Ascend PD + Mooncake

基线：**5x8-40/dynamo-ascend @ `release/1.4.2`**（含 `MooncakeConnectorV1` → `NixlConnectorProtocol` + Kunpeng `generic`，PR #3 已合入）。对齐镜像 **vllm-ascend 0.26.0rc1**。lake **只**提供文档与脚本，**不再**打 protocol patch。勿用 `ascend-dev`（1.5.0）。

不要用上游 GPU 名 `MooncakeConnector`（Ascend KV 无 CUDA `data_ptr()`）。

## 为什么协议挂到 Nixl

Dynamo 的 `KvConnectorProtocol` 按 **`kv_transfer_params` 线格式**选型，不是按连接器品牌名：

| 引擎侧 `kv_connector` | 线格式 | Dynamo protocol |
|----------------------|--------|-----------------|
| `NixlConnector`（0.26 为 Pull 兼容别名；另有 `NixlPullConnector` / `NixlPushConnector`） | pull：`remote_host` / `remote_port` / `remote_block_ids`（push 另有 writer 线程 + PUSH_REG + RDMA WRITE） | `NixlConnectorProtocol` |
| `MooncakeConnector`（上游 GPU） | push：`transfer_id` + bootstrap | `MooncakeConnectorProtocol` |
| `MooncakeConnectorV1`（Ascend） | **与 NIXL pull 相同的包** | **`NixlConnectorProtocol`** |

因此 Ascend V1 必须注册为 Nixl 协议类；挂 `MooncakeConnectorProtocol` 会按 bootstrap/push 解析，PD 对不上。

**结构性耦合**：dynamo 协议表按连接器**名字字符串**匹配。上游每改名/新增一次（例如 NIXL 拆 Pull/Push），dynamo 侧就要补登记。

**Ascend 注册面**：vllm-ascend `register_connector()` 约 11 个名字（含覆盖上游的 `MultiConnector` / `SimpleCPUOffloadConnector`）。dynamo 目前只显式认识 **`MooncakeConnectorV1`**。另有 `MooncakeLayerwiseConnector`（按层推，`do_remote_prefill` + metaserver）是**第三种线格式**，现有两个协议类都套不上；若要走 dynamo 需单写协议类。

## MultiConnector：当前层级结构（并行，非分级）

脚本默认拼三个**并列**子连接器，**不是** HBM→DRAM→远端 的统一分级缓存：

```
MultiConnector
├── MooncakeConnectorV1          # P↔D KV 传输（唯一产出 PD kv_transfer_params）
├── AscendStoreConnector         # 前缀 Store（backend=mooncake）
│     └── 依赖同机/可达的 mooncake_master（脚本会拉起）
└── AscendSimpleCPUOffloadConnector   # NPU→本进程 CPU 池（可选）
```

| 子连接器 | 职责 | 与其它子项关系 |
|----------|------|----------------|
| `MooncakeConnectorV1` | Prefill→Decode **块传输** | PD 热路径；与 Store/Offload **无关** |
| `AscendStoreConnector` | **前缀命中**读写（mooncake backend） | **必须**配 `mooncake_master`；索引/分配/驱逐归 master，连接器只是客户端 |
| `AscendSimpleCPUOffloadConnector` | 引擎侧 **NPU→CPU** 卸载 | 无后端钩子；驱逐即丢弃；**不可能**级联到 mooncake |

要点：

- **并列 = 广播写**：`MultiConnector.save_kv_layer` 对所有子连接器广播；Store 与 SimpleCPU **都会写**，两池都在 DRAM，内容易重叠。
- **生产配置宜二选一**：要跨实例留前缀 → 开 Store（可关 `ENABLE_KV_OFFLOAD=0`）；单节点只要本机 CPU 池 → 关 Store 子项、开 SimpleCPU。默认双开偏演示，不是推荐生产形态。
- **master 不感知 SimpleCPU**：Store 走 mooncake master；SimpleCPU 是 worker 进程内另一条路径。单用 Store 时，master 内部已有 DRAM→SSD 一类分级；并联 CPU 池反而绕开了它。

关闭卸载：`ENABLE_KV_OFFLOAD=0`。调 CPU 池：`OFFLOAD_CPU_BYTES`（默认 8GiB）。

### `lookup_rpc_port`（脚本里的 0/1）

AscendStore 配置字段名是 `lookup_rpc_port`，脚本传入的 `0`（Prefill）/ `1`（Decode）**不是 TCP 端口**，而是拼本地 IPC 路径的 **lookup id 后缀**（`lookup_rpc_port_{id}_dp_rank`）。Prefill 侧基本不做 lookup；Decode 用各自本地端点。脚本变量按 `lookup_id` 命名，避免当成网络端口。

### 真分级（未做；后续方向）

不要另起炉灶。上游 0.26 已有 HiCache 式框架：`OffloadingConnector` + `OffloadingSpecFactory`（`spec_module_path` 热加载）+ `TieringOffloadingSpec`（CPU 主层作 GPU 网关 + 可插拔二级 fs/obj/p2p）。镜像注释里的更新线 **`AscendOffloadingConnector` + `NPUOffloadingSpec`** 即这条；当前默认仍用 `AscendSimpleCPUOffloadConnector`（与 v0.26.0rc1 配对）。

## 单机 PD

```bash
cd /path/to/lake
bash scripts/dynamo-ascend/prepare_src.sh
bash scripts/dynamo-ascend/start_docker.sh
PROXY=$PROXY bash scripts/dynamo-ascend/build_install.sh
bash scripts/dynamo-ascend/start_etcd.sh
RESTART=1 bash scripts/dynamo-ascend/start_pd.sh
curl -s localhost:8000/v1/models
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

默认：Prefill NPU `0-3` TP4、Decode `4-7` TP4；`ROUTER_MODE=kv` + worker `--kv-events-config`。日志默认 `scripts/dynamo-ascend/logs/`。

## 如何核实 KV-aware router

1. FE 启动参数含 `--router-mode kv`（脚本默认；聚合 `start.sh` 已对齐）。
2. Worker 带 `--kv-events-config`（ZMQ）与 `DYN_SYSTEM_PORT`。
3. 探针：
   ```bash
   # FE：路由/组件指标
   curl -s localhost:8000/metrics | grep -E 'router_kv_|dynamo_component_kv_cache' || true
   # Prefill / 聚合 worker system port（PD 默认 8782；agg 默认 8782）
   curl -s localhost:8782/metrics | grep kv_publisher || true
   ```
4. 发几条**共享前缀**的 chat 后，再看上述指标是否增长；也可用 `ROUTER_MODE=round-robin` 对照。

## 如何核实 KV 卸载

1. 默认 `ENABLE_KV_OFFLOAD=1`；worker 日志应出现 `AscendSimpleCPUOffloadConnector` / `AscendMultiConnector`。
2. 对照关卸载：`ENABLE_KV_OFFLOAD=0 RESTART=1 bash scripts/dynamo-ascend/start_pd.sh`，日志中不应再加载该子连接器。
3. 压一段长上下文 / 多路并发后，用 `npu-smi` 看 HBM；卸载开启时更不易顶满（粗信号，非精确计数）。
4. 精确 block 级计数依赖连接器内部 stats；当前镜像以「配置生效 + 日志类名 + 对比开关」为主。

## 跨机 2P2D

```bash
# P-host
export HOST_IP=<p-ip> ETCD_ENDPOINTS=http://$HOST_IP:2379 MC_MASTER_ADDRESS=$HOST_IP
RESTART=1 ROLE=p bash scripts/dynamo-ascend/start_pd_multi.sh
# D-host
export HOST_IP=<d-ip> ETCD_ENDPOINTS=http://<p-ip>:2379 MC_MASTER_ADDRESS=<p-ip>
RESTART=1 ROLE=d bash scripts/dynamo-ascend/start_pd_multi.sh
```

容器需挂 `hccn_tool` / `npu-smi` / `hccn.conf`（`start_docker.sh` 在宿主机存在时自动挂），否则跨机 Ascend 直连 KV 易失败。Decode `kv_port` 默认基址 `20101`（避开 Prefill TP 占用的 `20001..`）。

## 验证记录

**单机（2026-09-21）**

- 源码基线：**dynamo `release/1.4.2` + MooncakeConnectorV1 协议**（PR #3 已合入该分支，非 `ascend-dev`/1.5.0）
- `/v1/models` → `qwen`；`/v1/chat/completions` 出 token
- FE `--router-mode kv`；metrics 可见 `dynamo_component_kv_cache_*` / worker `kv_publisher_*`

**跨机（2026-09-22）**

- P=`7.242.106.153`，D=`7.242.108.153`，1P1D TP4
- etcd advertise / `MC_MASTER_ADDRESS` 指向 P；`PREFER_SAME_NODE=false`
- 跨机 chat HTTP 200 出 token

## 脚本

| 脚本 | 作用 |
|------|------|
| `verify_protocol.sh` | 容器内断言已安装树含 `MooncakeConnectorV1` |
| `start_pd.sh` | 单机 PD + Mooncake + KV router + 可选卸载 |
| `start_pd_multi.sh` | `ROLE=p\|d` 跨机 |
