# HBM 归属与 KV 卸载对照

> 2026-08-29。范围：`3rdparty/` 中涉及 GPU HBM 或 KV 卸载的组件。不改方案 Z。  
> 分项细节见各目录 overview；本文只做横切对照。lake：F3/F4、[`../architecture/storage-layer.md`](../architecture/storage-layer.md)。

## 1. 三个接口

| | 职责 | 实现 |
|--|------|------|
| GPU 页分配 | attention 使用的 paged KV：`alloc` / `free` | 几乎均为引擎：`KVCacheManager.allocate_slots`、HiCache `value` |
| GPU 内存注册 | 已分配页作为 DMA/RDMA 的源或目的 | NIXL `PhysicalLayout`、CUDA IPC、`registerLocalMemory` |
| 前缀与位置 | 命中查询；层、节点坐标 | 引擎 APC、实例 radix、对象 exact-key、lake 控制面 |

KVBM 的「注册 G1」属于第二行：GPU 进入 `LogicalLayoutHandle`。它不分配 GPU 页（第一行仍是引擎），也不在 GPU 上建 radix（第三行 GPU 命中仍是 APC）。

引擎进程退出后：第一行的页失效；第二行若为本进程 MR 则失效；第三行仅当权威在该进程之外仍有效。F4 依赖第三行。

## 2. KVBM G1

### 定义

`LogicalLayoutHandle::G1`：固定容量，由推理框架或本机 KVBM 管理。`OffloadEngine` 持 `BlockManager<G2>`、`BlockManager<G3>`，无 `BlockManager<G1>`。`InstanceLeader` 仅持 `g2_manager` / `g3_manager`。

`ExternalBlock<G1>`：`block_id`（引擎已分配槽）+ `sequence_hash`（写入 G2 后登记）+ 类型参数。注释：block is held elsewhere。Worker `g1_handle`：该进程 GPU KV 区的 NIXL layout，供 `TransferManager` 寻址。

GPU 前缀命中：`KVCacheManager.get_computed_blocks` / `num_computed_tokens`。Router `StorageTier::Device` 主要来自引擎 KV event（`EventSource::Vllm`），不是 KVBM 对 G1 做 `register_sequence_hash`。

### KVBM 内用途

Worker 层间搬运统一为「block 集合从 `Gx` 到 `Gy`」（`kvbm-engine/docs/onboarding.md`）。无 G1 layout 则下列传输没有 GPU 侧描述符：

| 方向 | 行为 |
|------|------|
| G1→G2 | `enqueue_g1_to_g2`；`request_finished` 为 true，settlement 后再 `free` |
| G2/G3→G1 | 写入引擎已分配的 `dst_block_ids`（含 layer-wise） |
| G1→G1 | 同机或 PD；多 rank 时仅 rank 0 有 G2/G3，其余 rank 仅 G1，NCCL 从 rank 0 G1 广播（`physical/replicated.rs`） |
| 跨 worker RDMA | peer 导入 G1 layout，按 `block_id` 访问引擎页 |

`layout.nixl_register` 服务于上表，不表示 KVBM 拥有 HBM 分配权。

### 其它组件如何覆盖同类传输

无 G1 时，KVBM 不能把 GPU 纳入上述 `Gx→Gy`。卸载、PD 仍可用其它路径：

| 传输 | KVBM | 其它 |
|------|------|------|
| 同机 D2H/H2D | `Pipeline<G1,G2>` | LMCache `from_gpu`/`to_gpu`；UCM `dump`/`load`；vLLM `swap_blocks_triton` |
| 请求结束再拷、推迟 `free` | G1→G2 settlement | FlexKV `request_finished`→true；LMCache 在 forward 内拷完，`request_finished`→false |
| GPU↔GPU PD | G1→G1 | NIXL connector、Mooncake TE、TileRT、UCM HBM 直传 |
| 仅 rank 0 配 host/disk | G1 layout + NCCL | 无对应则须每 rank 自备 host 或不用该拓扑 |

FlexKV `register_gpu_blocks` 同样是第二行（IPC 映射引擎页，填入 `TransferOpGraph`）。无 `BlockManager`，不进入 `BlockRegistry`。

## 3. 分项

Transformers 无 KV 运行时，不列入。

### 引擎进程内

| | GPU 页 | 卸载 | 索引 |
|--|--------|------|------|
| vLLM APC | `BlockPool` | 无 | 进程内 `block_hash` 表。可选 `BlockStored(medium=GPU)` |
| vLLM `kv_offload` | 同上 | Scheduler 进程内 GPU→CPU→FS/Obj | `OffloadingManager` / `OffloadKey`。无集群位置视图 |
| HiCache | 引擎分配；`TreeNode.value` 记 GPU 槽 | `write_backup` / `load_back`（`host_value` 保留） | 实例 `HiRadixTree`。SGLang L1=GPU（lake L0），L2=host DRAM（lake L1）。L3 不记位置，`batch_exists` |
| Elastic Memory Pool | 同卡 HBM 上 KV 与 Mamba 子池再切分 | 非跨介质卸载 | 与 HiCache 正交 |

### Connector 卸载

GPU 页仍由引擎分配。组件维护 DRAM/SSD/远端索引；访问 GPU 靠注册指针或 IPC。

| | 何时离开 GPU | GPU 如何被访问 | 自管索引 |
|--|--------------|----------------|----------|
| LMCache | forward `save_kv_layer` | `register_kv_caches` 取 paged tensor，`from_gpu` 写入 `MemoryObj` | chunk key + 后端。GPU 不是 LMCache 的 cache tier |
| FlexKV | 请求结束 D2H | `register_gpu_blocks`；`set_gpu_blocks(engine block_id)` | CPU/SSD/REMOTE 各一棵 radix。无 GPU cache engine |
| UCM | `dump`/`load` | 设备指针 | store exact-key / `lookup_on_prefix`。PD 拓扑（HBM 直传 / DRAM 中介 / 统一池）是 connector 路径，HBM 分配仍在引擎 |

### 对象存储与传输引擎

与上一节的差别：组件按 **object key** 存字节，不维护引擎 `BlockPool` 的 slot 表。

**Mooncake store**：Client 挂载 DRAM/SSD segment；`ObjectKey` → replica。引擎 attention 使用的 paged KV 仍在引擎 `BlockPool`。store 不分配该 `BlockPool`，也不把 GPU 编入对象层。

**Mooncake Transfer Engine**：`registerLocalMemory` 可注册 CUDA 指针（DMA-BUF / nvidia-peermem），用于 PD 等 HBM↔HBM 传输。这是传输 MR，与 store 的对象层无关。lake Transfer Bus 对照 TE，不对照 store 的 HBM 模型。

**MemCache**：`LocalService` 将本机 HBM/DRAM（及 SSD）贡献为池介质；对象可落 `MEDIA_HBM`。元数据 exact-key（`MmcBmProxy`）。引擎 `BlockPool` 与池内 HBM 对象是否同一物理区，集成在 vllm-ascend，本 submodule 无。无 radix。进程退出后：引擎页随引擎；已 put 进池且介质仍在的对象由 Meta 保留。

### 其它

| | 行为 |
|--|------|
| TileRT | vLLM `block_id` 抽 KV，NIXL/Mooncake 写入 decode 单请求 GPU arena（`inject_cache`）。无分层卸载 |
| TensorCast | `publish` 将 KV 从引擎页拷到 Store Daemon 侧 artifact。引擎内页仍由引擎分配。LIP 原地租借 v1 不用于 KV |
| NVIDIA CMX | 目标栈含 GPU、KVBM、共享 flash。公开代码无端到端实现，不作对照依据 |

## 4. 对照表

| | GPU 页分配 | 该组件如何访问 GPU | 该组件维护的索引 | 引擎进程退出后 |
|--|----------|-------------------|------------------|----------------|
| vLLM APC | 引擎 | 不卸载 | 进程内 hash | GPU KV 失效；KV event 可指向已退出 worker |
| vLLM `kv_offload` | 引擎 | 引擎内级联 | 同进程 `OffloadingManager` | CPU/FS 随进程；对象存储另计 |
| HiCache | 引擎 | 树节点存 GPU 槽下标 | 实例 radix | 实例 L1/L2 失效；L3 可 prefetch |
| Elastic Memory Pool | 引擎 | 同卡子池，不卸载 | 各 pool | 同实例 |
| LMCache | 引擎 | tensor 指针拷出/拷入 | chunk + 后端 | 引擎页失效；daemon / 远程后端可保留 |
| Dynamo KVBM | 引擎 | G1 layout；无 `BlockManager<G1>` | 本实例 G2/G3 | 本实例 G1/G2/G3 失效；G4 可保留；Router 索引可过期 |
| FlexKV | 引擎 | IPC；无 GPU cache engine | CPU/SSD/REMOTE 树 | 本机失效；远端 / Mooncake store 可保留 |
| UCM | 引擎 | `dump`/`load` | store key | 引擎页失效；store 可保留 |
| Mooncake store | 不分配引擎页 | 不管理引擎 `BlockPool` | master exact-key（DRAM/SSD） | 对象可保留；无前缀树 |
| Mooncake TE | 调用方注册 | 传输 MR | 无 | MR 随进程 |
| MemCache | 引擎页由对接层分配；池可占用独立 HBM 段 | 对象可落 `MEDIA_HBM` | Meta exact-key | 池对象可保留；无 radix |
| TileRT | vLLM 抽；decode 单槽 | 不卸载 | 无 | 随 decode 请求 |
| TensorCast | 引擎 | 不索引引擎页；管 publish 后副本 | GS / daemon | 引擎页失效；已 publish artifact 可保留 |
| CMX | — | 未落地 | — | — |
| lake（目标） | 池分配 L0 | L0 ∈ `locations` | 控制面 radix + `locations` | 该卡 HBM 失效；L2 为 F4；视图仍指向 KV Node |

## 5. lake

前缀命中、按 overlap 选 worker、HBM→DRAM/SSD 卸载、本机 HBM 已有前缀则不跨机传（D-direct），上表多数系统都能做。D-direct 是执行模式，与卸载不是同一条路径；引擎持 HBM 时靠 APC / Device overlap 判定本地命中。lake 用 `locations` 里是否有该节点 L0 判定，因为没有 APC。

L0 归池，是把 HBM 收成与 L1/L2 相同的一层缓存，不是为了独占 D-direct。

| | 引擎持 HBM | L0 归池 |
|--|----------|---------|
| 往 GPU 装 KV | 请求到达后 `allocate_slots`，再 H2D / G2→G1 onboard | 后台按热度 promotion / 预取到 L0，请求路径只读视图（方案 Z） |
| L0 坐标 | APC + KV event，随引擎进程 | 与 L1/L2 同在控制面 `locations` |
| 计算进程退出（节点还在） | 引擎页与 APC 失效 | slot 仍由池 agent 管，视图不必作废 |
| 整机退出 | 两边 L0 都没了，靠 L2/L3 | 同左；F4 从 L2，视图仍指向 KV Node |

对 lake 要验证的是：热块能否在请求前落到目标 L0；Router 读到的 L0 与权威一致；计算进程重启不丢未驱逐的 L0 位置。D-direct 本身不在这份清单里。

L0→L0 RDMA 仍要注册 GPU 内存（对照 KVBM `g1_handle`）。归池后注册的是池 slot，不是引擎 `BlockPool` 再挂一层句柄。

## 6. 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| G1 枚举 | `3rdparty/dynamo/lib/kvbm-common/src/lib.rs`::`LogicalLayoutHandle::G1` |
| 无 `BlockManager<G1>`；G1→G2 | `kvbm-engine/src/offload/engine.rs`::`OffloadEngine` / `enqueue_g1_to_g2` |
| 外部槽 | `kvbm-engine/src/offload/source.rs`::`ExternalBlock` |
| Worker G1 layout | `kvbm-engine/src/worker/physical.rs`::`g1_handle`；`docs/onboarding.md` |
| 仅 rank 0 有 G2/G3 | `kvbm-engine/src/worker/physical/replicated.rs` |
| 本机 G2/G3 命中 | `kvbm/.../connector/leader/slot.rs`::`acquire_local_matches` |
| 推迟 `free` | `.../connector/leader.rs`::`request_finished` |
| NIXL 注册 | `lib/llm/src/block_manager/state/local.rs`::`LocalBlockDataFactories` |
| Router 介质 | `lib/kv-router/src/protocols.rs`::`StorageTier` |
| vLLM 分配后通知 connector | `vllm/v1/core/sched/scheduler.py`::`allocate_slots` → `update_state_after_alloc` |
| APC / KV event | `KVCacheManager.get_computed_blocks`；`vllm/distributed/kv_events.py`::`BlockStored` |
| 引擎内 offload | `vllm/v1/kv_offload/base.py`::`OffloadingManager` / `OffloadKey` |
| HiCache 槽 | `radix_cache.py`::`TreeNode.value` / `host_value`；`hiradix_cache.py`::`load_back` / `write_backup` |
| Elastic | `unified_memory_pool.py`::`UnifiedKVPool`；`kv_vmm_backing.py`::`KvVmmArena` |
| LMCache | `lmcache_connector_v1.py`::`get_num_new_matched_tokens` / `request_finished`；`gpu_connectors.py`::`from_gpu` / `to_gpu` |
| FlexKV 无 GPU cache engine | `cache_engine.py`::`GlobalCacheEngine` |
| FlexKV GPU 映射 | `storage_engine.py`::`register_gpu_blocks`；`GPUAllocator.from_raw_data` |
| FlexKV delay-free | `vllm_v1_adapter.py`::`request_finished` / `save_kv_layer` |
| UCM | `ucmstore_v1.py`::`UcmKVStoreBaseV1`；PD：`docs/source/user-guide/pd-disaggregation/` |
| Mooncake store | `MasterService` / `ObjectKey` |
| Mooncake GPU MR | `transfer_engine.h`::`registerLocalMemory` |
| MemCache 介质 | `mmc_bm_proxy.cpp`::`MmcBmProxy`（`MEDIA_HBM` / `MEDIA_DRAM`） |
| TileRT | `prefill_connector.py`::`TileRTConnector`；`inject_cache` |
| TensorCast | `publish` / `hydrate`（[`tensorcast/overview.md`](tensorcast/overview.md)） |
