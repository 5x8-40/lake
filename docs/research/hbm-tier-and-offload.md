# HBM 归属与 KV 卸载对照

> 2026-08-29。对照 `3rdparty/` 里所有会碰到 GPU HBM 或 KV 卸载的项目。不改方案 Z。  
> 分目录：[`sglang/hicache.md`](sglang/hicache.md) · [`sglang/elastic-memory-pool.md`](sglang/elastic-memory-pool.md) · [`vllm/overview.md`](vllm/overview.md) · [`lmcache/overview.md`](lmcache/overview.md) · [`dynamo/overview.md`](dynamo/overview.md) · [`flexkv/architecture.md`](flexkv/architecture.md) · [`ucm/architecture.md`](ucm/architecture.md) · [`mooncake/kv-store.md`](mooncake/kv-store.md) · [`mooncake/transfer-engine.md`](mooncake/transfer-engine.md) · [`memcache/architecture.md`](memcache/architecture.md) · [`tilert/pd-vllm.md`](tilert/pd-vllm.md) · [`tensorcast/overview.md`](tensorcast/overview.md) · [`nvidia-cmx.md`](nvidia-cmx.md)。  
> lake：[F1–F5](../features/features.md)、[`../architecture/storage-layer.md`](../architecture/storage-layer.md)。

本文只回答四件事：**谁发 GPU 槽**、**卸载栈把 HBM 编成什么**、**Dynamo 为什么要单独注册 G1**、**worker 退出后前缀位置还在不在**。

---

## 1. 先分清三件事

同一段前缀，各家往往记三本不同的账。混在一起就会觉得「没注册 G1 就不能卸载」。

| 账 | 管什么 | 典型落点 |
|----|--------|----------|
| **分配** | 谁 `alloc`/`free` HBM 页，attention 读哪 | 几乎全是引擎：`KVCacheManager.allocate_slots`、HiCache `value` |
| **传输端点** | 卸载/PD 能不能按 `block_id` 对 GPU 做 DMA/RDMA | 把 GPU 编进自己的 layout / IPC / `registerLocalMemory` |
| **索引** | 前缀命中查哪棵树 / 哪张表 | 引擎 APC、实例 radix、对象 exact-key、lake 控制面 |

「注册一层」说的是第二本账：GPU 成为卸载栈里的**具名端点**。不等于第三本账（GPU 上有 radix），更不等于第一本账（卸载栈发槽）。

进程退出后：第一本账（引擎页）没了；第二本账若只是本进程 MR，也没了；第三本账只有写在进程外的权威里才还在。F4 要的是第三本。

---

## 2. Dynamo G1：句柄，不是池

### 代码里 G1 是什么

`LogicalLayoutHandle::G1` 注释写明：固定大小，**由框架或本机 KVBM 管理**。`OffloadEngine` 只有 `BlockManager<G2>` / `BlockManager<G3>`，没有 `BlockManager<G1>`。`InstanceLeader` 只持 `g2_manager` / `g3_manager`。

卸载入队用 `ExternalBlock<G1>`：`block_id`（引擎已经分好的槽）+ `sequence_hash`（卸到 G2 后登记用）+ 类型参数。注释：*block is held elsewhere*。Worker 上另有 `g1_handle`：这块 GPU 内存的 NIXL/物理 layout，供 `TransferManager` 知道本进程 G1 在哪。

所以 G1 是：

- **有**：逻辑层枚举、物理 layout、`Pipeline<G1, G2>`、G2→G1 onboard 的 `dst_block_ids`。
- **无**：GPU free-list、GPU 上的 radix、进程退出后仍有效的 GPU 位置。

GPU 前缀命中仍是 vLLM APC / `num_computed_tokens`。Router 上的 `StorageTier::Device` 多来自引擎 KV event（`EventSource::Vllm`），不是 KVBM 往 G1 池里 insert。

### 为什么要注册这一层

KVBM 的 worker 用**同一套** layout + `TransferManager` 做所有层间搬运（`kvbm-engine/docs/onboarding.md`：leader 只说「把 block 1,2,3 从 Gx 转到 Gy」）。要把 GPU 接进这套机器，必须有一个 G1 句柄，否则 G1→G2、G2→G1、G1→G1 都没有源/目描述符。

注册之后，同一条路径能做：

1. **G1→G2 offload**：`enqueue_g1_to_g2(SourceBlocks::External)`；`request_finished` 返回 `True`，settlement 完再 `free`，避免页被引擎复用。
2. **G2/G3→G1 onboard**：写入引擎已分配的 `dst_block_ids`（layer-wise 也走这条）。
3. **G1→G1**：PD / 本机复制；多 rank 时 rank 0 有 G2/G3，其余 rank **只有 G1**，靠 NCCL 从 rank 0 的 G1 广播（`physical/replicated.rs`）。
4. **远端 RDMA**：peer 导入 G1 layout，按 `block_id` 直连引擎页，不必先落 host。

`layout.nixl_register` 服务的是这四条，不是「KVBM 拥有 HBM」。

### 不注册 G1，哪些仍做得到，哪些要另写一套

**仍做得到**（别的卸载都在做）：把 KV 从 GPU **拷走**存到 DRAM/SSD/对象；命中后再拷回引擎页；PD 走 NIXL / Mooncake TE / UCM「HBM 直传」等**另一条** GPU 路径。

**没有 G1 句柄时，KVBM 自己做不到**的是：用 **G2/G3 那套 worker 协议** 把 GPU 当源或目。缺了就要：

| 缺口 | 别人怎么补 |
|------|------------|
| 同机 D2H/H2D | LMCache `GPUConnector.from_gpu` / `to_gpu`；UCM `dump`/`load`；vLLM `swap_blocks_triton` |
| 请求结束再卸、页先别 `free` | FlexKV 同样 delay-free；LMCache 选择前向就拷完，`request_finished`→`False` |
| GPU↔GPU PD | TileRT / Mooncake TE / UCM HBM 直传 / vLLM NIXL connector，**不经过 KVBM G1** |
| 多 rank 只给 rank 0 配 host/disk | 没有 G1 layout 就无法「卸到 G2 再广播到各 rank G1」 |

结论：G1 **不是**「能卸载」的前提，是「GPU 参与 KVBM 自己的分层搬运与 collectives」。只做引擎旁 CPU 缓存，不必设 G1。FlexKV 注册的是 **IPC 映射 + 传输图里的 GPU 端点**（`register_gpu_blocks`），语义接近 G1 句柄，只是不叫 G1、也不进 `BlockRegistry`。

---

## 3. 各家怎么处理 HBM

按角色写。Transformers 只有模型定义，不进本对照。

### 引擎自己的 GPU 池

**vLLM APC** — `KVCacheManager` / `BlockPool` 发槽、hash 表做本进程前缀。不卸载。可选 `kv_events.BlockStored(medium=GPU)` 给 Router 看，进程一没事件就指向幽灵 worker。

**vLLM `vllm/v1/kv_offload/`** — 引擎内 GPU→CPU→FS/Obj。槽仍是 APC 的。CPU 是网关层，secondary 做 cascade/promotion。无集群位置视图。`OffloadingConnector` 只是把这套暴露成 connector。

**SGLang HiCache** — 命名：它的 L1 = GPU（lake L0），L2 = host DRAM（lake L1）。`TreeNode.value` / `host_value` 记两套槽。GPU 页仍引擎分配，但树把 GPU 当**本实例的一层**，不是外部句柄。`load_back` 拷回 GPU，保留 `host_value`。L3 不记位置，`batch_exists`。进程退出则 L1/L2 没了。

**SGLang Elastic Memory Pool** — 同卡 HBM 上 KV 池与 Mamba 池再切分（VMM / `UnifiedKVPool`），**不是**跨介质卸载。与 HiCache 正交。

### 引擎旁卸载：GPU 是端点

**Dynamo KVBM** — 上节。G2/G3 本实例 `BlockManager`；G4 外部对象。

**FlexKV** — `GlobalCacheEngine` 只建 CPU/SSD/REMOTE 树。GPU：`GPUAllocator.from_raw_data` + `register_gpu_blocks`（CUDA IPC / fabric）。`save_kv_layer` 为空；结束 delay-free D2H。`TransferOpGraph.set_gpu_blocks` 把引擎 `block_id` 填进 H2D/D2H。文档：GPU 不进 FlexKV 驱逐范围。

### 引擎旁卸载：GPU 只当拷贝源/目，不进分层

**LMCache** — `register_kv_caches` 拿 paged tensor 基址。attention 里 `save_kv_layer` → `from_gpu` 进 `MemoryObj`。`get_num_new_matched_tokens` 第二项 `False`；`request_finished`→`False`，GPU 可立即 `free`。索引是 chunk key + 后端，不是 GPU 层 radix。P2P 主路径多走 CPU；PD 可另挂 `NixlStorageBackend`。

**UCM** — store `dump`/`load` 对设备指针搬字节。Store 元数据是 key，不是 G1。PD 三种拓扑（HBM 直传 / DRAM 中介 / 统一池）都是 **connector 怎么走**，HBM 权威仍在引擎。无 D-direct。

### 对象池：HBM 不是引擎 paged KV 的账

**Mooncake store** — 池化 Client 贡献的 DRAM/SSD，exact-key，无 radix。HBM 仍实例私有。Store 不解释 HBM。

**Mooncake Transfer Engine** — `registerLocalMemory` 可注册 CUDA 指针（DMA-BUF / nvidia-peermem），PD 可 HBM→HBM。这是**传输**，不是 store 把 GPU 编成一层。lake Transfer Bus 对标这里。

**Ascend MemCache** — `LocalService` 贡献连续 **HBM/DRAM**（及 SSD），Meta 记 `MEDIA_HBM` 等。对象 exact-key，`GetInto` / `RegisterBuffer`。这是「池介质含 HBM」，最接近 lake「HBM 进池」的工业形态，但：无前缀 radix；引擎计算用 KV 与池对象是否同一 arena，集成在 vllm-ascend，不在本 submodule。放置/D-direct 仍不是方案 Z。

### 专用 decode / 张量层 / 目标栈

**TileRT** — `TileRTConnector` 从 vLLM `block_id` 抽 KV，NIXL/Mooncake **GPU 直传** 进 decode 单请求 arena，`inject_cache`。无卸载分层、无池化 L0。

**TensorCast** — `publish` 把 KV **拷出**引擎 HBM，管的是 worker 侧副本。引擎内 KV 仍引擎自留地（LIP 原地租借 v1 不用在 KV 上）。无「G1 层」。

**NVIDIA CMX** — 目标路径含 GPU + KVBM + 共享 flash。公开仓库拼不出端到端。不当作已实现对照。

---

## 4. 总表

| | GPU 槽 | HBM 在卸载/存储栈里 | 自管索引 | 该 GPU 进程退出后 |
|--|--------|---------------------|----------|-------------------|
| vLLM APC | 引擎 | 不卸载 | 进程内 hash | GPU KV 丢失；event 可能过期 |
| vLLM `kv_offload` | 引擎 | 引擎内级联 | 同进程 `OffloadingManager` | CPU/FS 随进程；Obj 另计 |
| HiCache | 引擎 | 树节点记 GPU 槽 | 实例 `HiRadixTree` | L1/L2 丢失；L3 可 prefetch |
| Elastic Memory Pool | 引擎（同卡多子池） | 不卸载 | 同 HiCache / 各 pool | 同实例私有 |
| LMCache | 引擎 | 拷贝基址，非一层 | chunk + 后端 | 引擎页丢失；daemon/远程 L2 可留 |
| Dynamo KVBM | 引擎 | **G1 句柄**，无 `BlockManager<G1>` | 本实例 G2/G3 | G1/G2/G3 随实例；G4 可留；Router 可能过期 |
| FlexKV | 引擎 | IPC 端点，无 GPU cache engine | CPU/SSD/REMOTE 各树 | 本机随进程；远端/Mooncake store 可留 |
| UCM | 引擎 | dump/load，非一层 | store key | 引擎页丢失；store 可留 |
| Mooncake store | 不参与 | 不解释 HBM | master exact-key | 对象可留；前缀不在 store |
| Mooncake TE | 调用方注册 | 传输 MR，非池 | 无 | MR 随进程 |
| MemCache | 对接引擎；池贡献 HBM 段 | 对象可在 `MEDIA_HBM` | Meta exact-key | 池对象可留；无 radix 位置视图 |
| TileRT | vLLM 抽、decode 单槽 | 不卸载 | 无 | 槽随 decode 请求 |
| TensorCast | 引擎 | 只管 publish 之后的副本 | GS/daemon 副本账 | 引擎内 KV 丢失；已 publish 的 artifact 可留 |
| CMX | 目标栈 | 未公开落地 | — | — |
| **lake（目标）** | **池分配 L0** | L0 是位置视图里的一层 | 仅控制面 radix + `locations` | 该卡 HBM 丢失；L2 为 F4；视图仍指向 KV Node |

---

## 5. 对 lake

前缀命中、按 cache 所在 worker 选路、把 KV 卸到 DRAM/SSD，上表里多数系统都能做。P7.6 本地命中主要来自亲和选路，池侧预放置是补充。这些不能单独论证「必须把 HBM 划归池」。

lake 的 D-direct：前缀**已由池放到**某节点 HBM，坐标在控制面。Dynamo Device overlap 是「这台 worker 算过、引擎页还在」。请求侧可以少传，但不能在到达前把前缀放到一台**从未算过它**的 GPU 上。

HBM 归池要验证的是：worker 退出后从 L2 续推；控制面能把 L0 放到尚未计算该前缀的 GPU（方案 Z / warmup）；引擎不再另持 APC 当前缀真相。MemCache 证明「HBM 可以当池介质」；Mooncake 证明「传输可以打 GPU、store 仍不管 HBM」。lake 要的是两者合一：L0 既是池介质，又进同一份 radix/`locations`。

G1 对 lake 的借鉴是 **传输图把 L0 当端点**（agent 按 `block_id`/slot 做 RDMA），不是再引入「引擎发槽 + 卸载栈注册句柄」的双轨。方案 Z 下槽的分配也归池，句柄与池是同一套。

---

## 6. 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| G1 枚举（框架管 GPU） | `3rdparty/dynamo/lib/kvbm-common/src/lib.rs`::`LogicalLayoutHandle::G1` |
| G1 无 BlockManager；G1→G2 | `kvbm-engine/src/offload/engine.rs`::`OffloadEngine` / `enqueue_g1_to_g2` |
| 外部槽位 | `kvbm-engine/src/offload/source.rs`::`ExternalBlock` |
| Worker G1 layout | `kvbm-engine/src/worker/physical.rs`::`g1_handle`；`docs/onboarding.md` |
| 仅 rank 0 有 G2/G3 | `kvbm-engine/src/worker/physical/replicated.rs` |
| 本机 G2/G3 命中 | `dynamo/.../kvbm/.../connector/leader/slot.rs`::`acquire_local_matches` |
| delay-free | `.../connector/leader.rs`::`request_finished` |
| NIXL 注册 | `lib/llm/src/block_manager/state/local.rs`::`LocalBlockDataFactories` / `layout.nixl_register` |
| Router 介质 | `lib/kv-router/src/protocols.rs`::`StorageTier` |
| vLLM 槽 + 通知 connector | `vllm/v1/core/sched/scheduler.py`::`allocate_slots` → `update_state_after_alloc` |
| vLLM APC / 事件 | `KVCacheManager.get_computed_blocks`；`vllm/distributed/kv_events.py`::`BlockStored` |
| vLLM 进程内 offload | `vllm/v1/kv_offload/base.py`::`OffloadingManager` / `OffloadKey` |
| HiCache GPU/host 槽 | `radix_cache.py`::`TreeNode.value` / `host_value`；`hiradix_cache.py`::`load_back` / `write_backup` |
| Elastic 同卡多池 | `unified_memory_pool.py`::`UnifiedKVPool`；`kv_vmm_backing.py`::`KvVmmArena` |
| LMCache 前向保存 | `lmcache/.../lmcache_connector_v1.py`::`get_num_new_matched_tokens` / `request_finished` |
| LMCache GPU 拷贝 | `lmcache/v1/gpu_connector/gpu_connectors.py`::`from_gpu` / `to_gpu` |
| FlexKV 无 GPU 层树 | `flexkv/cache/cache_engine.py`::`GlobalCacheEngine` |
| FlexKV GPU 映射 | `storage/storage_engine.py`::`register_gpu_blocks`；`GPUAllocator.from_raw_data` |
| FlexKV delay-free | `integration/vllm/vllm_v1_adapter.py`::`request_finished` / `save_kv_layer` |
| UCM dump/load | `ucm/store/ucmstore_v1.py`::`UcmKVStoreBaseV1`；PD 拓扑 `docs/source/user-guide/pd-disaggregation/` |
| Mooncake store | `mooncake-store` `MasterService` / `ObjectKey`（无 HBM 层） |
| Mooncake GPU MR | `transfer_engine.h`::`registerLocalMemory`；`rdma_transport.cpp` DMA-BUF |
| MemCache HBM 介质 | `mmc_bm_proxy.cpp`::`MmcBmProxy`（`MEDIA_HBM` / `MEDIA_DRAM`） |
| TileRT 灌入 decode HBM | `tilert/pd_vllm/prefill_connector.py`::`TileRTConnector`；`decode_server` `inject_cache` |
| TensorCast 不管引擎内 KV | `publish` / `hydrate`（见 [`tensorcast/overview.md`](tensorcast/overview.md)） |
