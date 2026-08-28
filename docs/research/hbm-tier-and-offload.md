# HBM 归属、卸载路径与「一份 radix / 一份位置」

> 来源：2026-08-28 对照 Dynamo KVBM / LMCache / 其它卸载栈。  
> 状态：研究笔记，不改 lake 已拍板的方案 Z。  
> 相关：[`dynamo/overview.md`](dynamo/overview.md)、[`lmcache/overview.md`](lmcache/overview.md)、[`sglang/hicache.md`](sglang/hicache.md)、[`vllm/overview.md`](vllm/overview.md)、[`ucm/overview.md`](ucm/overview.md)、[`features/features.md`](../features/features.md) F1–F5。

## 1. 那句话在说什么

讨论里有一句：

> 一个 radix、一份位置：不拆成引擎 APC + KVBM 池 + 弱一致 event。F4 从 L2 续推、失败重跑选路，靠这个。

拆开是三本账 vs 一本账，用**算力进程被杀掉**来看。

lake 的 F4（[`../features/features.md`](../features/features.md)）：GPU worker 崩溃后，未完成请求换到新节点，从 Pool 里最近的 KV 续推。要做到这一点，系统必须能回答：**这段前缀的字节现在在哪一层、哪台机器，而且这个答案在杀进程之后仍然成立。**

Dynamo 接 vLLM 时，同一段前缀往往同时存在三处、三套语义：

| 账本 | 记什么 | 进程被杀后 |
|------|--------|------------|
| 引擎 APC | 本进程 GPU 上哪些 `block_hash` 还在 HBM（`KVCacheManager.get_computed_blocks`） | **没了**。HBM 和这张哈希表都在引擎里。 |
| 本实例 KVBM | 本 worker DRAM/SSD 上哪些 `sequence_hash` 在 G2/G3（`BlockManager<G2/G3>.match_sequence_hashes`） | **多半没了**。G2/G3 跟 `InstanceLeader` 走，不是独立于引擎的全局池。除非已经卸到 G4 对象存储。 |
| Router 的 KV event 索引 | 某 worker 在 Device / HostPinned / Disk 上「好像」有这些 hash（NATS/ZMQ，best-effort） | **还在，但可能是谎**。事件会丢、会滞后。索引仍指向已死的 worker。 |

杀 worker 之后：APC 没了，本机 G2/G3 通常一起没了，Router 还可能把下一请求送到「Device overlap 很高」的尸体上。这时没有一份活着的权威位置，F4 只能退回从 prompt 重算——不是「从 L2 checkpoint 续推」。

lake 要的「一份 radix、一份位置」是：前缀树和 L0–L3 位置只在存储控制面（内存权威 + etcd 降频 checkpoint）。Router 读镜像选路；真正搬 KV、F4 续推查权威树。引擎没有私有 APC 当第二套前缀真相。worker 死了，L2 还在池里，位置视图仍写着「这块在某 KV Node 的 NVMe」，新 worker 按视图拉，而不是猜三本账里哪本还对。

这和「Router 用不用 event」不是一件事。event 可以当镜像的推送通道。问题是：**有没有一本杀不掉的位置真相**，以及 **GPU 前缀是不是还另有一本引擎私有账**。

## 2. 讨论结论（HBM / G1 / 注册 / 选路）

### 2.1 GPU 槽：申请和释放都归引擎

接 vLLM 时，Dynamo 和 LMCache 一样走 `KVConnectorBase_V1`：

1. 调度器 `allocate_slots` 发 GPU 页；
2. 再 `update_state_after_alloc` 通知 connector；
3. 结束时先 `request_finished`，再 `free`。

KVBM **不**当 HBM 的 free-list。`ExternallyManagedDeviceSlot` 的注释就是：不控制 device pool，只接受外部给的 `block_ids`（`append_mutable_device_blocks`）。

UCM、vLLM 自带 `kv_offload`、SGLang HiCache 也是同一模式：HBM 页是引擎的。

### 2.2 Dynamo 的 G1 不是一层池，是句柄

`OffloadEngine` 写明：G1 由 vLLM GPU cache 外拥，没有 `BlockManager<G1>`。pipeline 吃 `ExternalBlock<G1>` = `block_id` + `sequence_hash`（`kvbm-engine/src/offload/source.rs`）。

`InstanceLeader` 只持 `g2_manager` / `g3_manager`。G1 让 G1→G2 / G2→G1 和 G2/G3 走同一套 transfer 状态机，**不是**再当一次 GPU 分配器。

### 2.3 三件事不要绑在一起

| | 看什么 | 干什么 |
|--|--------|--------|
| Router | KV event（worker + hash + `StorageTier`） | 选哪台 worker |
| 本实例 KVBM | 本地 `host()` / `disk().match_sequence_hashes_blocking`（`acquire_local_matches`） | 这台机器 DRAM/SSD **到底有没有**、要不要 onboard |
| NIXL 长期注册 | `layout.nixl_register` 后的地址 / MR | **怎么搬字节** |

Router **不**靠 GPU 注册感知命中。本 worker G2/G3 **不**靠 Router 做精确 lookup。注册是搬运执照。

GPU 上的「本地命中」首先是 vLLM APC / `num_computed_tokens`。KVBM 从「GPU 没有的那段」起搜 host/disk。Router 上的 `StorageTier::Device`  overlap 多半来自引擎 KV event（`EventSource::Vllm`），不是 KVBM 发的 G1 池事件。

### 2.4 同机拷贝很像，时间点不同

同机 GPU→host：LMCache 是 `GPUConnector.from_gpu` 进 `MemoryObj`；Dynamo 是已注册的 `DeviceStorage` 走 G1→G2。都是把页里的字节搬走。

差别在**何时拷、拷完 GPU 能不能立刻回收**：

- LMCache：attention 里 `save_kv_layer`，forward 结束 `wait_for_save`。`get_num_new_matched_tokens` 第二项固定 `False`（不当整段 async 占槽）。`request_finished` 返回 `False`，GPU 可以立刻 `free`。
- Dynamo：算完再 `enqueue_g1_to_g2`，`request_finished` 返回 `True` 推迟 `free`，避免引擎复用页覆盖还没卸完的 KV。可用 `OffloadFilter` 挑块。代价是 HBM 驻留更久。

跨 worker / PD 时，注册过的引擎页才能按 `block_id` 做 GPU 直连。LMCache 的 P2P 文档主路径是 CPU 内存；PD 另走 `NixlStorageBackend`。只做「卸到 host 再进远程 store」，不需要把 GPU 叫做 G1 层。

### 2.5 只做外部 cache 时，G1 层不是必须的

若目标只是「引擎管 HBM，插件把 KV 卸到 DRAM/SSD 再拉回来」，LMCache / UCM / vLLM `kv_offload` 就够。G1 作为 KVBM 的一层，是为了：算完再卸、对着引擎已分好的页 onboard、GPU 直连 PD。没有这三条，G1 就是多挂一份 `device_blocks`。

### 2.6 外观能力 vs HBM 归池

用户能看见的：前缀少算、请求送到 cache 所在 worker、PD/混部、GPU 卸到更慢层。Dynamo 都能做。P7.6 也显示：**本地命中 SLO 的主因是亲和选路，池侧预放置是加速和兜底。**

lake 文档里的 D-direct 更严：前缀被**存储池放置**到某节点 HBM，位置在权威元数据里，没有计算层私有 APC（[`../features/features.md`](../features/features.md) 执行模式节）。Dynamo 的 Device overlap 是「这张卡以前算过，引擎页还在」。表现可以像直跳，但池不能把热点前缀铺到一台**从没算过它**的 GPU 上。

HBM 归池是手段，服务的是：算力可扔、一本位置真相、向未计算节点预放置 L0。不是「为了也能 cache」。

## 3. 各家卸载怎么表现

列的是**接上引擎之后**谁管 HBM、谁管 DRAM/SSD、集群怎么看见、进程没了 KV 还在不在。不是比带宽。

| | GPU 槽谁发 | HBM 在卸载栈里是什么 | 何时离开 GPU | 本机 DRAM/SSD 谁索引 | 集群怎么看见 | 前缀账本 | 杀掉该 GPU 进程后 |
|--|------------|----------------------|--------------|----------------------|--------------|----------|-------------------|
| **vLLM APC only** | `KVCacheManager` | 引擎私有，不卸载 | 不离 | 无 | 可选 `kv_events`（`BlockStored` + `medium`） | 进程内 hash 表，无 radix | GPU KV 全丢 |
| **vLLM `kv_offload`** | 同上 | 引擎私有多层（CPU 网关 → FS/Obj） | Scheduler 进程内 cascade / promotion | `OffloadingManager` 在**同一引擎进程** | 事件可带 medium；无集群权威 | APC hash + `OffloadKey`(hash+group) | CPU/FS 层也在该进程里，一起没；Obj 层另说 |
| **SGLang HiCache** | 引擎 allocator | L1 实例私有；树上记 L1/L2 槽 | `write_backup` / 驱逐时 `write_back` / 或 write_through | **同一棵** `HiRadixTree`（`value` / `host_value`） | L3 **不**记位置，实时 `batch_exists` | 实例 radix；L3 只有 key | L1/L2 丢；L3 后端若独立则新实例可 prefetch |
| **LMCache** | vLLM 发槽 | 不当一等层；`register_kv_caches` 只当拷贝基址 | 前向 `save_kv_layer` | LMCache 自己的 chunk key / CPU·disk 后端 | controller `RegistryTree` best-effort，或共享 L2 | 引擎 APC + 顺序 `contains` | 引擎页丢；MP daemon / 远程 L2 可活 |
| **Dynamo KVBM** | vLLM 发槽 | G1 = 外拥句柄，无 `BlockManager<G1>` | 请求结束后 delay-free 再 G1→G2 | 本实例 `g2_manager` / `g3_manager` | KV event：Device 多来自引擎，Host/Disk 来自 KVBM `BlockRegistry` | 引擎 APC **加上** KVBM hash **加上** Router 索引 | G1+本机 G2/G3 随实例；G4 对象可活；Router 索引可能指向尸体 |
| **UCM** | 引擎发槽 | 不当层；connector dump/load | `UcmKVStoreBaseV1.dump` | 可插拔 store 的 key（常为 vLLM block hash） | 无全局 radix | 引擎 APC + store `lookup` | 引擎页丢；Mooncake/NFS 等 store 可活 |
| **Mooncake store** | 不参与 | 不解释 HBM | 调用方 put | master 上 exact-key，无前缀 | master 线性表 | 无 radix | 对象可活；前缀复用不在 store 里 |
| **Ascend MemCache** | 引擎/对接侧 | HBM/DRAM/SSD 对象池，exact-key | 池 put/get | Meta/Local | MetaService | 无内容寻址 radix | 池可活；不是前缀树 |
| **lake（目标）** | 池发 L0 槽 | L0 是真池，方案 Z 放置 | 池写回 / 预放置 / 驱逐，不是 connector 顺手拷 | 控制面 radix + 位置视图 | 权威内存，Router 只读镜像 | **只有**控制面这一棵 | 物理 HBM 随卡没；L2 是 F4 恢复点，位置仍在 |

补充：

- **HiCache** 最接近「一份树」——但树在**实例里**，L3 位置还不在树上。新机器要靠 L3 探测，不是全局位置视图。
- **vLLM kv_offload** 和 KVBM 一样是引擎旁边的降层，只是没有 Dynamo 那套集群 Router event。`OffloadingConnector` 把这套子系统露成 connector。
- **LMCache / UCM** 明确做「外部 cache 插件」，不把 GPU 编进自己的分层状态机。
- **Mooncake / MemCache** 是对象池，前缀和位置要上层自己搞。lake 只借 Mooncake 传输，不借 store 当控制面。

## 4. 对 lake 的含义

1. **不要把 Dynamo 的 G1 理解成「他们已经管了 HBM」。** 他们管的是句柄和卸载流水线；发槽的仍是 vLLM。
2. **不要把「Router 看见 Device overlap」理解成必须长期注册 GPU。** 看见靠 event；搬走靠注册；本机 G2/G3 靠 KVBM lookup。
3. **外观上的前缀复用 + 亲和选路 + 分层卸载，Dynamo/LMCache/HiCache 都能做。** 这不能单独证明必须 HBM 归池。P7.6 的本地命中数字也偏向亲和选路。
4. **HBM 归池仍然只对这几条有硬需求：** 算力进程可扔且 F4 从 L2 续推（一本杀不掉的位置）；向未计算过的 GPU 预放置 L0（方案 Z / warmup）；引擎不再持私有 APC。这些要单独用弹性和故障场景证明，不要说成「否则就不能 D-direct」。

## 5. 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| G1 外拥、无 G1 manager | `3rdparty/dynamo/lib/kvbm-engine/src/offload/engine.rs` 注释；`ExternalBlock` |
| 本机 G2/G3 精确命中 | `…/kvbm/src/block_manager/vllm/connector/leader/slot.rs`::`acquire_local_matches` |
| 推迟释放 GPU 页 | `…/connector/leader.rs`::`request_finished`（返回 `true`） |
| NIXL 注册 layout | `…/llm/src/block_manager/state/local.rs`::`layout.nixl_register` |
| Router 层枚举 | `…/kv-router/src/protocols.rs`::`StorageTier` |
| G2/G3 注册发 event | `…/llm/src/block_manager/block/registry.rs`::`BlockRegistry`（HostPinned/Disk） |
| LMCache 前向存、立刻放页 | `lmcache/integration/vllm/lmcache_connector_v1.py`::`get_num_new_matched_tokens` 第二项 `False`；adapter `request_finished` → `False` |
| LMCache 拷 GPU | `lmcache/v1/gpu_connector/gpu_connectors.py`::`GPUConnectorInterface.from_gpu` / `to_gpu` |
| vLLM 发槽再通知 connector | `vllm/v1/core/sched/scheduler.py`::`allocate_slots` → `update_state_after_alloc` |
| vLLM APC / 事件 | `KVCacheManager.get_computed_blocks`；`vllm/distributed/kv_events.py`::`BlockStored` |
| vLLM 进程内多层 offload | `vllm/v1/kv_offload/base.py`::`OffloadingManager` / `OffloadKey` |
| HiCache 一棵树记 L1/L2 | `sglang/.../radix_cache.py`::`TreeNode.value` / `host_value`；`hiradix_cache.py`::`write_backup` |
| UCM dump/load | `ucm/store/ucmstore_v1.py`::`UcmKVStoreBaseV1` |
