# FlexKV — 总览

> 源码:`3rdparty/flexkv`(submodule,HEAD `a5c8f12`,2026-08-27)。上游 [taco-project/FlexKV](https://github.com/taco-project/FlexKV)（腾讯云 TACO）。许可：**Apache-2.0**（第三方组件见 `LICENSE`）。  
> 分层、GPU 注册、传输图见 [architecture.md](architecture.md)；与 lake 对照见 [pain-points.md](pain-points.md)。HBM/卸载对照见 [`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)。  
> 2026-09-29 补充：SGLang 主干接入形态、异构 KV（Gemma4/DSV4）与 Mamba 边界（submodule 指针不变，所引 `docs/gemma4_support.md`、`docs/dsv4_compress_state_io_zh.md` 均在 `a5c8f12` 内）。

## 一句话定位

FlexKV 是挂在推理引擎上的 **CPU / SSD / 远端 多级 KV 卸载库**：自己管这三层的 radix 与 mempool，用 CUDA IPC 映射引擎已分配的 GPU 页做拷贝。HBM 页的分配和释放仍在引擎里。

已进 vLLM（`FlexKVConnectorV1`，≥0.17.2 无需补丁）、SGLang（`--enable-flexkv`）、NVIDIA Dynamo（`--connector flexkv`）、TensorRT-LLM。

SGLang 侧有两种形态，注意区分：

- **主干原生**（[sglang#29701](https://github.com/sgl-project/sglang/pull/29701)，2026-07-07 合入）：`FlexKVRadixCache` 继承 `RadixCache`，经 `mem_cache/registry.py` 注册，`--enable-flexkv` 单 flag 启用。它**不是** `HiCacheStorage` 后端——FlexKV 的 radix 索引与淘汰策略在自己的 server 进程里，没法当 HiRadixTree 下的哑字节后端，只能与 HiRadixCache **平行**存在。代价已在合入时显现：scheduler 只在 `enable_hierarchical_cache` 时 tick `check_hicache_events()`，FlexKV 的异步 store 锁无人释放导致集群卡死，修复方式是把该钩子（及 `NO_TOKEN` 软处理）改成 OR 两个 flag——树内两套平行缓存层级，每个 scheduler 钩子都要记得两边。
- **树外 connector**（`integration/sglang/connector.py`）：早期接入路径，仍随 FlexKV 仓分发。

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

## 异构 KV 与 Mamba 边界

FlexKV 原假设所有层 KV shape 一致（统一 `num_kv_heads`/`head_size`，贯穿配置、buffer、stride、传输）。2026-07 起陆续打破：

- **Gemma4 31B**（`docs/gemma4_support.md`）：50 层 SWA（16 head×256）+ 10 层 full attention（4 head×512）。解法是 `LayerGroupSpec`（按 `(num_kv_heads, head_size)` 分组），但**统一切块被保住**：单一 BLOCKFIRST CPU buffer，每个 block 内按组顺序拼接各层 K/V，`token_size_in_bytes` 按组求和；异构性只体现为组内 `(offset, layer_stride, kv_stride, chunk_size)`（`get_group_strides()`），传输时每组各调一次 `transfer_kv_blocks()`。两个配套修复值得记：GPU stride 从 tensor 实际 `stride()` 探测（triton `[N,2,B,H,D]` vs flash_attn `[2,N,B,H,D]` 布局 dim0/1 互换，信配置会算错）；`StorageEngine` 延迟到 GPU 注册后创建（否则按 max×max 估算 token 大小，16GB 只分出 546 块 vs 正确值 1191）。vLLM 侧代价：开 `--kv-transfer-config` 后 vLLM 禁用自己的 Hybrid KV Cache Manager，connector 看到全部层完整 per-token KV，SWA 滑窗语义被放弃（窗口外 KV 照存，浪费但正确）。
- **DeepSeek-V4**（[#225](https://github.com/taco-project/FlexKV/pull/225)，`docs/dsv4_compress_state_io_zh.md`）：异构 C4/C128/indexer KV group + FullKV/SWA 双缓存 + attention/indexer compress-state sidecar + 逐层恢复。与 Gemma4 同属"混合注意力"一族的层组化。
- **Mamba / 线性注意力 state：不支持**（2026-09-29 全仓核实，`mamba|conv_state|recurrent|linear_attn` 零命中）。FlexKV 的抽象是"每 token 定长 KV 块 + radix 前缀匹配"；递归 state 每序列定长、不随 token 增长，前缀复用需要块边界 state 快照，概念体系不同。DSV4 的 compress-state sidecar 是稀疏注意力 indexer 状态，仍属注意力家族，不算递归 state 支持。对照：SGLang `HiCacheStorage` v2 用**物理分池**承载 Mamba/SWA/DSA/Draft（见 [`../sglang/storage-backends.md`](../sglang/storage-backends.md)）；lake 的对应答案是 t-type/r-type 布局元数据（[`../../architecture/storage-layer.md`](../../architecture/storage-layer.md) "KV 类型"节）。

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
| SGLang（树外） | `integration/sglang/connector.py`::`FlexKVConnector` |
| SGLang（主干） | `3rdparty/sglang` `mem_cache/storage/flexkv/flexkv_radix_cache.py`::`FlexKVRadixCache`；`mem_cache/registry.py` |
| 异构层组 | `common/config.py`::`LayerGroupSpec`；`common/storage.py`::`KVCacheLayout.get_group_strides` |
| GPU stride 探测 | `transfer/worker.py`::`_get_gpu_strides_from_tensor` |
| 延迟建 StorageEngine | `transfer_manager.py`::`initialize_transfer_engine` |

## 参考

- 上游：[github.com/taco-project/FlexKV](https://github.com/taco-project/FlexKV)
- 本仓：`3rdparty/flexkv` @ `a5c8f12`
- 卸载对照：[`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)
- 引擎接入：[`../vllm/compute.md`](../vllm/compute.md)、[`../sglang/storage-backends.md`](../sglang/storage-backends.md)
