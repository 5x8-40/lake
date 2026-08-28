# HBM 归属与 KV 卸载对照

> 2026-08-28。对照 Dynamo KVBM、LMCache 及其它卸载栈；不改方案 Z。  
> 相关：[`dynamo/overview.md`](dynamo/overview.md)、[`lmcache/overview.md`](lmcache/overview.md)、[`sglang/hicache.md`](sglang/hicache.md)、[`vllm/overview.md`](vllm/overview.md)、[`ucm/overview.md`](ucm/overview.md)、[`../features/features.md`](../features/features.md) F1–F5。

## 1. 前缀位置为什么要合一

F4 要求：GPU worker 挂了之后，未完成请求换到新节点，从 Pool 里已有的 KV 续推。前提是系统能回答：这段前缀的字节在哪一层、哪台机器，而且这个答案在进程退出后仍然有效。

Dynamo 接 vLLM 时，同一段前缀通常有三套记录：

| 记录 | 内容 | worker 进程退出后 |
|------|------|-------------------|
| 引擎 APC | 本进程 GPU 上哪些 `block_hash` 还在（`KVCacheManager.get_computed_blocks`） | 没有。页和哈希表都在引擎里。 |
| 本实例 KVBM | 本机 DRAM/SSD 上哪些 `sequence_hash` 在 G2/G3（`match_sequence_hashes`） | 通常没有。G2/G3 跟 `InstanceLeader` 走。已写到 G4 对象存储的除外。 |
| Router 的 KV event 索引 | 某 worker 在 Device / HostPinned / Disk 上有过这些 hash（NATS/ZMQ，best-effort） | 索引还在，但可能过期，仍指向已退出的 worker。 |

进程退出后：APC 没了，本机 G2/G3 通常也没了。Router 仍可能按旧的 Device overlap 把请求送到那台机器。这时没有一份仍有效的位置记录，续推只能退回从 prompt 重算。

lake 的做法是：前缀树和 L0–L3 位置只放在存储控制面（进程内存为权威，etcd 存降频 checkpoint）。Router 读镜像选路；搬 KV 和 F4 查权威树。引擎不再另持一套 APC 当前缀真相。worker 退出后，L2 仍在池里，位置视图仍指向对应 KV Node，新 worker 按视图拉取。

Router 用不用 event 推送镜像，和「有没有一份进程退出后仍有效的位置」是两件事。event 可以做推送；GPU 前缀也不该再有一份只活在引擎里的账。

## 2. GPU 槽仍由引擎分配

接 vLLM 时，Dynamo 和 LMCache 都走 `KVConnectorBase_V1`：调度器 `allocate_slots` → `update_state_after_alloc` → 结束时 `request_finished` 再 `free`。

KVBM 不管理 HBM free-list。`ExternallyManagedDeviceSlot` 只接收引擎给的 `block_ids`（`append_mutable_device_blocks`）。UCM、vLLM `kv_offload`、SGLang HiCache 同样：HBM 页由引擎分配和释放。

## 3. Dynamo 的 G1 是句柄，不是池

`OffloadEngine` 写明：G1 是 vLLM 的 GPU cache，没有 `BlockManager<G1>`。卸载管线用 `ExternalBlock<G1>`（`block_id` + `sequence_hash`）。`InstanceLeader` 只持 `g2_manager` / `g3_manager`。G1 用来把 G1↔G2 接进和 G2/G3 相同的 transfer 路径，并不替代 GPU 分配器。

若只需「引擎管 HBM，插件把 KV 卸到 DRAM/SSD 再拉回」，LMCache、UCM、vLLM `kv_offload` 即可。KVBM 把 G1 标成一层，是为了：请求结束后再卸、写入引擎已分好的页、按 `block_id` 做 GPU 直连。没有这三项，不必单独设 G1。

## 4. 选路、本机命中、内存注册

| | 依据 | 作用 |
|--|--------|------|
| Router | KV event（worker + hash + `StorageTier`） | 选 worker |
| 本实例 KVBM | `host()` / `disk().match_sequence_hashes_blocking`（`acquire_local_matches`） | 本机 DRAM/SSD 是否命中、是否 onboard |
| NIXL 注册 | `layout.nixl_register` | 搬字节 |

Router 不靠 GPU 内存注册判断命中。本机 G2/G3 不靠 Router 做 lookup。注册只服务传输。

本机 GPU 命中先看 vLLM APC / `num_computed_tokens`。KVBM 从 GPU 未命中的后缀起查 host/disk。Router 上的 `StorageTier::Device` overlap 主要来自引擎 KV event（`EventSource::Vllm`），不是 KVBM 的 G1 池事件。

## 5. 同机卸载：拷贝相近，时间不同

同机 GPU→host：LMCache 用 `GPUConnector.from_gpu` 写入 `MemoryObj`；Dynamo 从已注册的 `DeviceStorage` 做 G1→G2。都是把页内字节拷走。

差别在拷贝时机和 GPU 页何时可回收：

- LMCache：attention 中 `save_kv_layer`，forward 结束 `wait_for_save`。`get_num_new_matched_tokens` 第二项为 `False`。`request_finished` 返回 `False`，GPU 页可立即 `free`。
- Dynamo：请求结束后 `enqueue_g1_to_g2`，`request_finished` 返回 `True`，等卸完再 `free`，避免页被复用。可用 `OffloadFilter` 选择卸哪些块。GPU 页会多占一段时间。

跨 worker / PD 时，已注册的引擎页可按 `block_id` 直连。LMCache P2P 主路径是 CPU 内存，PD 走 `NixlStorageBackend`。若卸载路径是 GPU→host 再进远程 store，不必把 GPU 叫做 G1。

## 6. 各家对照

下表只比：谁发 GPU 槽、HBM 在卸载栈中的角色、何时离 GPU、DRAM/SSD 谁索引、集群如何看见、进程退出后 KV 是否还在。

| | GPU 槽 | HBM 角色 | 何时离开 GPU | DRAM/SSD 索引 | 集群可见性 | 前缀记录 | 该 GPU 进程退出后 |
|--|--------|----------|--------------|---------------|------------|----------|-------------------|
| vLLM APC | `KVCacheManager` | 引擎私有，不卸载 | 不离 | 无 | 可选 `kv_events`（`BlockStored` + `medium`） | 进程内 hash 表 | GPU KV 丢失 |
| vLLM `kv_offload` | 同上 | 引擎内 CPU→FS/Obj | Scheduler 进程内 cascade / promotion | 同进程 `OffloadingManager` | 事件可带 medium | APC hash + `OffloadKey` | CPU/FS 随进程；Obj 另计 |
| SGLang HiCache | 引擎 | L1 实例私有；树节点记 L1/L2 槽 | `write_backup` / `write_back` / write_through | 同一棵 `HiRadixTree` | L3 不记位置，`batch_exists` | 实例 radix；L3 只有 key | L1/L2 丢失；独立 L3 可供新实例 prefetch |
| LMCache | vLLM | 不作为一层；`register_kv_caches` 提供拷贝基址 | 前向 `save_kv_layer` | chunk key + CPU/disk 后端 | `RegistryTree` 或共享 L2 | 引擎 APC + 顺序 `contains` | 引擎页丢失；daemon / 远程 L2 可保留 |
| Dynamo KVBM | vLLM | G1 句柄，无 `BlockManager<G1>` | 结束后 delay-free，再 G1→G2 | 本实例 `g2_manager` / `g3_manager` | Device 事件多来自引擎；Host/Disk 来自 KVBM `BlockRegistry` | APC + KVBM hash + Router 索引 | 本机 G1/G2/G3 随实例；G4 可保留；Router 索引可能过期 |
| UCM | 引擎 | 不作为一层；dump/load | `UcmKVStoreBaseV1.dump` | store key（多为 vLLM block hash） | 无全局 radix | 引擎 APC + store `lookup` | 引擎页丢失；store 可保留 |
| Mooncake store | 不参与 | 不解释 HBM | 调用方 put | master exact-key | master | 无 radix | 对象可保留；前缀不在 store |
| MemCache | 引擎/对接 | HBM/DRAM/SSD 对象池，exact-key | 池 put/get | Meta/Local | MetaService | 无前缀 radix | 池可保留 |
| lake（目标） | 池分配 L0 | L0 由池放置 | 池写回 / 预放置 / 驱逐 | 控制面 radix + 位置视图 | 控制面内存；Router 读镜像 | 仅控制面 | 该卡 HBM 丢失；L2 作 F4 恢复点，位置仍在 |

- HiCache：L1/L2 在同一棵树上，但树在实例内，L3 位置不在树上。新实例靠探测 L3，没有全局位置视图。
- vLLM `kv_offload` 也是引擎内降层，没有 Dynamo 那套集群 Router event。`OffloadingConnector` 把它暴露成 connector。
- LMCache / UCM 是引擎外的 cache 插件，不把 GPU 编进自己的分层。
- Mooncake / MemCache 是对象池；前缀和位置由上层处理。lake 用 Mooncake 传输，不用它的 store 做控制面。

## 7. 对 lake

前缀命中、按 cache 所在 worker 选路、分层卸载，Dynamo / LMCache / HiCache 都能做。P7.6 里本地命中主要来自亲和选路，池侧预放置是补充。这些不能单独用来论证必须把 HBM 划归池。

lake 文档里的 D-direct 是：前缀已由存储池放到某节点 HBM，位置在控制面元数据中。Dynamo 的 Device overlap 是该 worker 曾经算过、引擎页还在。请求侧可以少传或不传，但不能在请求到达前，把前缀放到一台尚未计算过它的 GPU 上。

HBM 归池对应的是：worker 退出后仍能从 L2 续推；控制面能把 L0 放到尚未计算该前缀的 GPU（方案 Z / warmup）；引擎不再维护私有 APC。这些要用弹性和故障场景验证，而不是说「没有 HBM 归池就做不了 D-direct」。

## 8. 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| G1 无 BlockManager | `3rdparty/dynamo/lib/kvbm-engine/src/offload/engine.rs`；`ExternalBlock` |
| 本机 G2/G3 命中 | `…/kvbm/src/block_manager/vllm/connector/leader/slot.rs`::`acquire_local_matches` |
| 推迟释放 GPU 页 | `…/connector/leader.rs`::`request_finished` |
| NIXL 注册 | `…/llm/src/block_manager/state/local.rs`::`layout.nixl_register` |
| Router 层枚举 | `…/kv-router/src/protocols.rs`::`StorageTier` |
| G2/G3 注册发 event | `…/llm/src/block_manager/block/registry.rs`::`BlockRegistry` |
| LMCache 前向保存 | `lmcache/integration/vllm/lmcache_connector_v1.py`::`get_num_new_matched_tokens`；adapter `request_finished` |
| LMCache GPU 拷贝 | `lmcache/v1/gpu_connector/gpu_connectors.py`::`from_gpu` / `to_gpu` |
| vLLM 分配后再通知 | `vllm/v1/core/sched/scheduler.py`::`allocate_slots` → `update_state_after_alloc` |
| vLLM APC / 事件 | `KVCacheManager.get_computed_blocks`；`vllm/distributed/kv_events.py`::`BlockStored` |
| vLLM 进程内 offload | `vllm/v1/kv_offload/base.py`::`OffloadingManager` / `OffloadKey` |
| HiCache L1/L2 | `radix_cache.py`::`TreeNode.value` / `host_value`；`hiradix_cache.py`::`write_backup` |
| UCM dump/load | `ucm/store/ucmstore_v1.py`::`UcmKVStoreBaseV1` |
