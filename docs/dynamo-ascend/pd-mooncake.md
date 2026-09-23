# Ascend PD + Mooncake

基线：**5x8-40/dynamo-ascend**（含 `MooncakeConnectorV1` → `NixlConnectorProtocol`，见 [dynamo-ascend#2](https://github.com/5x8-40/dynamo-ascend/pull/2)）。lake **只**提供文档与脚本，**不再**打 protocol patch。

不要用上游 GPU 名 `MooncakeConnector`（Ascend KV 无 CUDA `data_ptr()`）。

## 为什么协议挂到 Nixl

Dynamo 的 `KvConnectorProtocol` 按 **`kv_transfer_params` 线格式**选型，不是按连接器品牌名：

| 引擎侧 `kv_connector` | 线格式 | Dynamo protocol |
|----------------------|--------|-----------------|
| `NixlConnector` | pull：`remote_host` / `remote_port` / `remote_block_ids` | `NixlConnectorProtocol` |
| `MooncakeConnector`（上游 GPU） | push：`transfer_id` + bootstrap | `MooncakeConnectorProtocol` |
| `MooncakeConnectorV1`（Ascend） | **与 NIXL 相同的 pull 包** | **`NixlConnectorProtocol`** |

因此 Ascend V1 必须注册为 Nixl 协议类；挂 `MooncakeConnectorProtocol` 会按 bootstrap/push 解析，PD 对不上。

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
| `AscendStoreConnector` | **前缀命中**读写（mooncake backend） | **必须**配 `mooncake_master`；master 管的是 Store 段，**不**感知 SimpleCPU offload 池 |
| `AscendSimpleCPUOffloadConnector` | 引擎侧 **NPU→CPU** 卸载 | 独立 DRAM 池；**不会**自动晋升/降级到 Store，也**不是** G1→G2→G3 |

要点（对应评审「卸载后 mooncake_master 怎么知道」）：

- **不知道，也不需要知道。** master 只服务 AscendStore；SimpleCPU offload 是 worker 进程内另一条路径。
- 当前实现是 **功能并列**，不是 Dynamo KVBM 那种统一分层。真分级（统一调度 / 跨层迁移）未做，后续若要做再单独立项。

关闭卸载：`ENABLE_KV_OFFLOAD=0`。调 CPU 池：`OFFLOAD_CPU_BYTES`（默认 8GiB）。

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

1. FE 启动参数含 `--router-mode kv`（脚本默认）。
2. Worker 带 `--kv-events-config`（ZMQ）与 `DYN_SYSTEM_PORT`。
3. 探针：
   ```bash
   # FE：路由/组件指标
   curl -s localhost:8000/metrics | grep -E 'router_kv_|dynamo_component_kv_cache' || true
   # Prefill worker system port（默认 8782）
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

- 源码：dynamo-ascend + MooncakeConnectorV1 协议
- `/v1/models` → `qwen`；`/v1/chat/completions` 出 token
- FE `--router-mode kv`；metrics 可见 `dynamo_component_kv_cache_*` / worker `kv_publisher_*`

**跨机（2026-09-22）**

- P=`7.242.106.153`，D=`7.242.108.153`，1P1D TP4
- etcd advertise / `MC_MASTER_ADDRESS` 指向 P；`PREFER_SAME_NODE=false`
- 跨机 chat HTTP 200 出 token

## 脚本

| 脚本 | 作用 |
|------|------|
| `install_src.sh` | 断言协议已安装 |
| `start_pd.sh` | 单机 PD + Mooncake + KV router + 可选卸载 |
| `start_pd_multi.sh` | `ROLE=p\|d` 跨机 |
