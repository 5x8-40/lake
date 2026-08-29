# FlexKV — 总览

> 源码:`3rdparty/flexkv`(submodule,HEAD `a5c8f12`,2026-08-27)。上游 [taco-project/FlexKV](https://github.com/taco-project/FlexKV)（腾讯云 TACO）。许可：**Apache-2.0**（第三方组件见 `LICENSE`）。  
> 分层、GPU 注册、传输图见 [architecture.md](architecture.md)；与 lake 对照见 [pain-points.md](pain-points.md)。HBM/卸载对照见 [`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)。

## 一句话定位

FlexKV 是挂在推理引擎上的 **CPU / SSD / 远端 多级 KV 卸载库**：自己管这三层的 radix 与 mempool，用 CUDA IPC 映射引擎已分配的 GPU 页做拷贝。HBM 页的分配和释放仍在引擎里。

已进 vLLM（`FlexKVConnectorV1`，≥0.17.2 无需补丁）、SGLang（`--enable-flexkv`）、NVIDIA Dynamo（`--connector flexkv`）、TensorRT-LLM。

## 与本系统的关系

| FlexKV 概念 | 本系统对应 | 关系 |
|-------------|-----------|------|
| `FlexKVConnectorV1` / SGLang `FlexKVConnector` | worker↔存储池 client | **接入样板**；lake 要把 connector 升为必经路径 |
| `GlobalCacheEngine` + 每层 `CacheEngineAccel` | 控制面 radix + 层内块池 | **形态可对照**（每层一棵树 + mempool）；树在引擎侧进程，不是集群权威 |
| `StorageEngine` CPU/SSD/REMOTE 分配 | L1/L2/L3 介质 | FlexKV 自己分配 CPU/SSD；lake 由池统一放置 |
| `register_gpu_blocks` / `TensorSharedHandle` | L0 句柄 | **只映射、不拥有**；对标 Dynamo G1 `ExternalBlock` |
| `TransferOpGraph` + Mooncake TE / GDS | Transfer Bus | D2H/H2D/DISK2D 图可对照；发起方仍是 connector 任务，不是池 agent |
| Redis GMS + 本机 radix 快照 | 位置视图 | 周期上传/拉取，lease 保传输窗口；不是单写者权威 |
| Dynamo `KVEventCollector` | Router 镜像推送 | 可选；与 P2P 分布式复用文档声明互斥 |

**核心结论**：FlexKV 与 **LMCache / UCM 同层**（引擎插件），控制面更接近 **Dynamo KVBM 的 G2/G3**：自管 DRAM/SSD 的 radix，GPU 只当拷贝端点。不是「HBM 归池」的存储基础设施。

## 设计哲学

- GPU 显存不够时把 KV 卸到更便宜的介质，避免丢掉后重算。
- 三层在 GPU **之下**：CPU → 本地 SSD → 远端（云盘 / Mooncake store / PCFS）。
- 通过 connector 注入，不自研 serving 引擎。
- 默认可库内直调；DP>1 或多实例走 ZMQ server-client。

## 架构

```
vLLM / SGLang / TRT-LLM / Dynamo worker
  ├─ 引擎 APC + allocate_slots / free     ← GPU 槽的唯一所有者
  └─ connector
        ├─ scheduler: get_match / put_match / launch_tasks
        └─ worker: register_kv_caches（IPC 映射 GPU 页）
              │
              ▼
        KVManager → KVTaskEngine
              ├─ GlobalCacheEngine（CPU / SSD / REMOTE 各一棵 radix + mempool）
              ├─ StorageEngine（CPU/SSD/REMOTE 自分配；GPU 只 from_raw_data）
              └─ TransferEngine（D2H / H2D / GDS / Mooncake / P2P）
```

| 模块 | 职责 |
|------|------|
| **StorageEngine** | 按配置建 CPU/SSD/REMOTE 缓冲；GPU 经 `register_gpu_blocks` 挂引擎 tensor |
| **GlobalCacheEngine** | 规划 get/put 方向与物理 block id；不建 GPU 层 cache engine |
| **TransferEngine** | 执行传输图；`set_gpu_blocks` 把引擎 slot 填进图 |

## 分布式模型

> 跨项目汇总对比见 [../distributed-models.md](../distributed-models.md)；细节见 [architecture.md](architecture.md)。

- **拓扑**：本机自治 + 可选中心快照。默认单实例（库内直调）；DP>1 或多实例走 ZMQ server-client；跨节点复用（P2P）需 `FLEXKV_ENABLE_P2P=1` + Redis。
- **元数据权威**：本机每层一棵 `CRadixTreeIndex`（CPU/SSD/REMOTE）+ mempool，索引在 connector 进程内，非集群权威；GPU（HBM）不进索引（引擎 APC 管）。集群级仅有 Redis GMS 存的全局快照。
- **同步机制**：各节点周期 upload/rebuild 快照到/自 Redis；查询读本地快照，不打中心；lease 保证传输窗口内块有效（只保传输，不保位置权威）。或选 Dynamo `KVEventCollector` 事件推送，但文档声明与 P2P 分布式复用互斥，二选一。
- **一致性**：快照最终一致，无单写者权威；快照陈旧时拉到已驱逐块，靠 lease/重试兜底。无可同步查询的权威点。
- **HA 与故障**：worker/进程退出，本机树通常一并失效；Redis 快照在 lease/TTL 过期后无效。不存在"worker 退出后仍指向有效 L2、可供续推"的集群位置权威（[architecture.md](architecture.md) §5），lake F4 即针对该场景。
- **扩展性**：本机索引 + 周期快照，水平扩展无协调成本；代价是全局视图的时效与准确性。
- **与 lake 对照**：FlexKV 的"本机索引 + Redis 周期快照"是 lake"CP 权威 + 镜像推送"的弱化版——lake 镜像由 CP 权威变更触发推送（增量 + gap replay），误判可回查 CP；FlexKV 快照无权威可回查。且 lake L0（HBM）在控制面索引内，FlexKV 不索引 HBM。

## 技术栈

- **语言**：Python（集成、任务、控制面编排）+ C++（`CRadixTreeIndex`、io_uring SSD、GDS、P2P/Redis）。
- **构建**：`build.sh` / `c_ext`；分布式需 `FLEXKV_ENABLE_P2P=1` + Redis。
- **传输**：本机 GPU↔CPU；SSD 走 io_uring 或 GDS（NIXL GDS_MT / cuFile）；跨节点 Mooncake TE；远端可接 Mooncake store。

## 代码索引

| 概念 | 文件:符号 |
|------|-----------|
| vLLM 适配 | `flexkv/integration/vllm/vllm_v1_adapter.py`::`FlexKVConnectorV1Impl` |
| 引擎包装 | `3rdparty/vllm/.../flexkv_connector.py`::`FlexKVConnectorV1` |
| GPU 注册 | `vllm_v1_adapter.py`::`register_to_server`；`storage_engine.py`::`register_gpu_blocks` |
| 结束卸载 | `vllm_v1_adapter.py`::`request_finished` |
| 前缀匹配 | `kvmanager.py`::`get_match`；`kvtask.py`::`get_match` |
| 分层控制面 | `cache/cache_engine.py`::`GlobalCacheEngine` / `CacheEngineAccel` |
| GPU 填槽 | `common/transfer.py`::`TransferOpGraph.set_gpu_blocks` |
| IPC 句柄 | `common/memory_handle.py`::`TensorSharedHandle` |
| 每层树 | `csrc/radix_tree.h`::`CRadixTreeIndex` |
| 分布式元数据 | `cache/redis_meta.py`::`RedisMeta`；`cache/hie_cache_engine.py`::`HierarchyLRCacheEngine` |
| Dynamo 事件 | `integration/dynamo/collector.py`::`KVEventCollector` |
| Mooncake store 远端 | `external/mooncake_store_utils.py`::`MooncakeStoreCacheEngine` |
| SGLang | `integration/sglang/connector.py`::`FlexKVConnector` |

## 参考

- 上游：[github.com/taco-project/FlexKV](https://github.com/taco-project/FlexKV)
- 本仓：`3rdparty/flexkv` @ `a5c8f12`
- 卸载对照：[`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)
- 引擎接入：[`../vllm/compute.md`](../vllm/compute.md)、[`../sglang/storage-backends.md`](../sglang/storage-backends.md)
