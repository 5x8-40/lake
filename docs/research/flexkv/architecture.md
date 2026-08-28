# FlexKV — 架构与 HBM

> 前置：[overview.md](overview.md)。源码 `3rdparty/flexkv` @ `a5c8f12`。  
> 对照：[`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)、[`../dynamo/overview.md`](../dynamo/overview.md)、[`../lmcache/overview.md`](../lmcache/overview.md)。

## 1. GPU 槽与 HBM

引擎 `allocate_slots` / `free` 管 GPU 页。FlexKV 不维护 GPU free-list，也不对 GPU 做 LRU。

文档写明：FlexKV 在 GPU 之下，GPU KV 由引擎管，不进 FlexKV 驱逐范围（`docs/eviction_policy/README_zh.md`）。

`GlobalCacheEngine` 只为 CPU / SSD / REMOTE 建 `CacheEngineAccel`（或 P2P 时的 `HierarchyLRCacheEngine`）。没有 `DeviceType.GPU` 的 cache engine。`DeviceType.GPU` 只出现在传输图和 `StorageEngine` 的映射表里。

生产路径：`GPUAllocator.from_raw_data`，数据是 `TensorSharedHandle` 或引擎 tensor。`GPUAllocator.allocate`（自己 `torch.empty`）只给测试或非对接场景。

worker 在 `register_kv_caches` 里把 paged KV 交给 `KVTPClient.register_to_server`。句柄走 CUDA IPC；vLLM sleep/VMM 路径用 fabric handle（`memory_handle.py`）。Transfer 子进程映射同一块 HBM，按 `block_id` 读写。这和 Dynamo `layout.nixl_register` 同类：注册服务传输，不表示 FlexKV 拥有这些页。

SWA 另开一套 GPU handle（`register_swa_gpu_blocks`），与主 KV 同 device_id 但不共用 key，避免撞槽。

## 2. 请求路径上 GPU 怎么用

**命中**

1. 引擎 APC 先给出 `num_computed_tokens`（已在本机 GPU 的前缀）。
2. connector `get_num_new_matched_tokens` 用剩余 token 调 `get_match`。
3. FlexKV 在 CPU/SSD/REMOTE 树上匹配；第二返回值 `True` 表示要从 FlexKV 拉块。
4. `update_state_after_alloc` 之后才有引擎 `block_ids`。`TransferOpGraph.set_gpu_blocks` 把这些 id 填进 H2D 的 dst。

`gpu_matched_blocks` 传进 `match_all` 时，是引擎已经算过的前缀长度，不是 FlexKV 自己的 GPU 索引。

**卸载**

`save_kv_layer` / `wait_for_save` 为空。不在 attention 里逐层拷。请求结束 `request_finished`：需要 put 则返回 `True`，vLLM 推迟 `free`，scheduler `launch_tasks` 做 D2H，`query_finished_task` 后再放页。和 Dynamo delay-free 同类，与 LMCache（前向保存、`request_finished`→`False`）相反。

abort 默认不 put；`offload_kv_on_finish` 可强制卸。

## 3. 分层与索引

三层外部缓存：

| 层 | 介质 | 索引 | 字节谁分配 |
|----|------|------|------------|
| （引擎） | HBM | 引擎 APC | 引擎 |
| CPU | DRAM / hugepage | `cpu_cache_engine` radix + mempool | FlexKV |
| SSD | 本地 NVMe | `ssd_cache_engine` | FlexKV |
| REMOTE | 云盘 / PCFS / Mooncake store | 远端树或 exact-key | 后端 |

默认可视为 inclusive / write-through：put 先写 CPU，再补 SSD/远端未命中块。某层驱逐只摘该层树、回收该层块，**不**向下层写回。下层没有副本时，上层赶走就 miss。

驱逐策略：lru（默认）/ lfu / slru / fifo / mru / filo；水位 `evict_start_threshold`、每次 `evict_ratio`。

C++ `CRadixTreeIndex` 节点记 physical block、hash 链、lock、ready。SWA 挂在同一节点上（slot / tombstone），不是第二棵 GPU 树。

Mooncake store 作 REMOTE 时走 `MooncakeStoreCacheEngine`：`match`/`insert` 对齐 `CacheEngineAccel`，键是内容寻址对象，不是节点挂槽。文档：REMOTE2H 只走 prefetch，compute GET 忽略 remote，从本机 ready 层 H2D。

## 4. 传输

`TransferType`：H2D、D2H、DISK2H、H2DISK、DISK2D、D2DISK、REMOTE2H、H2REMOTE、PEER*。GPU 相关 op 的 GPU 侧 id 在 launch 时绑定。

GDS：SSD↔GPU 可不经 CPU。跨节点：Mooncake TE + Redis 元数据；本机索引是全局快照，查询不打中心。lease 保证传输期间块有效。周期 upload/rebuild。

FlexKV 文档：Dynamo KV events 与 namespace isolation、与 distributed reuse **不要一起开**。

## 5. 进程退出之后

| 记录 | 内容 | 该 GPU/worker 进程退出后 |
|------|------|-------------------------|
| 引擎 APC | 本进程 GPU 上的 hash | 没有 |
| FlexKV 本机 CPU/SSD 树 | 本实例 DRAM/NVMe | 通常没有（跟 FlexKV/引擎进程） |
| Redis GMS 快照 | 他节点曾公布的块 | 可能还在，lease/TTL 过期后无效 |
| Dynamo Router 事件 | `BlockStored` medium 多为 `CPU` | 索引可能过期 |
| Mooncake store / 远端文件 | 对象 | 可保留；前缀不在 lake 控制面 |

没有一份「worker 没了仍指向有效 L2、可供 F4 续推」的集群位置权威。SSD 在本机时，进程没了盘上文件是否还能被新实例认领，取决于是否另开共享远端层，且新实例要重建或拉取索引。

## 6. 和邻近项目

| | FlexKV | Dynamo KVBM | LMCache | HiCache |
|--|--------|-------------|---------|---------|
| GPU 槽 | 引擎 | 引擎；G1 句柄 | 引擎 | 引擎 |
| 自管层 | CPU/SSD/REMOTE | G2/G3（G4 对象） | CPU/disk/远程 backend | 实例 L1/L2 |
| 何时离 GPU | 结束后 delay-free | 结束后 delay-free | 前向 `save_kv_layer` | `write_backup` 等 |
| GPU 在卸载栈中 | 注册端点，非一层 | G1 句柄 | 拷贝基址 | 树节点可记 GPU 槽 |
| 集群 | Redis 快照或 Dynamo events | NATS KV events | RegistryTree / 共享 L2 | L3 `batch_exists` |

## 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| 无 GPU cache engine | `cache_engine.py`::`GlobalCacheEngine.__init__`（只 enable_cpu/ssd/remote） |
| GPU 映射 | `storage/allocator.py`::`GPUAllocator.from_raw_data`；`storage_engine.py`::`register_gpu_blocks` |
| delay-free | `vllm_v1_adapter.py`::`request_finished` |
| 前向不卸 | `vllm_v1_adapter.py`::`save_kv_layer` |
| 引擎前缀 + FlexKV 后缀 | `vllm_v1_adapter.py`::`get_num_new_matched_tokens`；`num_gpu_matched_tokens` |
| 图绑定引擎 slot | `common/transfer.py`::`set_gpu_blocks` |
| 每层驱逐 | `docs/eviction_policy/README_zh.md`；`csrc/radix_tree.cpp`::`CRadixTreeIndex::evict` |
| 事件 medium | `integration/dynamo/collector.py`::`publish_stored`（默认 `CPU`） |
