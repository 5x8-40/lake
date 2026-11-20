# LMCache — 跨实例复用与存储后端

> 源码:`lmcache/v1/`、`csrc/storage_backends/`、`rust/`、`lmcache/v1/mp_coordinator/`(2026-11-13 快照 `58c11e38`)。submodule gitlink 同为 `58c11e38`;本文路径与符号以这棵树为准。

## 缓存键与匹配

- **键**:`ObjectKey` = `(chunk_hash, kv_rank, model_name, object_group_id, cache_salt, tags)`,见 `lmcache/v1/distributed/api.py`::`ObjectKeyIdentity`。`kv_rank` 入 key,同 chunk 不同 rank 是不同对象。
- **切分与哈希**:`TokenHasher`(`lmcache/v1/common/token_hasher.py`)按 `chunk_size`(默认 256)切 token,**链式 prefix hash**(`compute_chunk_hashes`:每 chunk hash 依赖它前面全部 chunk);默认算法 blake3,亦支持从 vLLM 载入 hash 实现。
- **匹配**:无 radix tree(在引擎侧);LMCache 靠逐 chunk 顺序探测,断链即停。`chunk_hash` 是内容哈希,故 L2 层内容寻址。
- **跨进程一致**:只有退回 builtin hash 时才需要 `PYTHONHASHSEED`(`TokenHasher._get_hash_func` 在此分支 warn);blake3 路径无此约束。
- **L2 key 编码**:`lmcache/v1/distributed/key_codec.py`::`encode_storage_key` → `{model}@{rank}@{group}@{hash}` + tag 后缀,`decode_storage_key` 可逆。

## 跨实例共享(两条路径)

1. **共享远程存储(L2 adapter)**:多实例共用 S3/HF3FS/Mooncake Store/Aerospike/Valkey/Bigtable/WQS 等,通过 `chunk_hash` 内容寻址天然去重。上游把这批归为"suited to sharing cache across nodes",见 [remote_and_distributed.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/l2_storage/remote_and_distributed.rst)。
2. **P2P 直传(L1 内存共享)**:`P2PL2Adapter` 经 transfer channel 做单边 RDMA 读:
   - `impl/nixl_impl.py`::`NixlTransferChannelClient` — 默认引擎,`lmcache[nixl]` extra。
   - `impl/mooncake_te_impl.py`::`MooncakeTeTransferChannelClient` — Mooncake transfer engine。
   - coordinator 的 `KeyDirectory` 记录 L1/L2 placement，`POST /directory/lookup` 一次就能拿到位置 hint；但 P2P 取数路径当前没接这份目录：本节点 L1 miss 后，仍向每一个活着的 MP server 建 P2P 连接并发 lookup，命中后由对端返回地址。节点越多，取数查询扇出越大，大规模应优先走共享 L2。
   - 前提:必须有 coordinator,且 L1 须为一整段连续区域 → 与 GDS L1、Device-DAX L1 互斥,见 [mp/p2p.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/p2p.rst)。
   - **P2P 的落点是本节点 L1(CPU pinned DRAM),不是显存；PD 的 NixlConnector P→D 不走这条路径。** Mooncake TE 可以注册 HBM 做 RH2D；这里两跳是 LMCache 的 L1 落点选择，不是底层做不到。 `p2p.rst`: "The transfer is a one-sided RDMA read from the requesting node into its own L1"。之后再由 H2D 进 HBM,所以 LMCache 的 L1 P2P 跨节点读是两跳。L1 的定义见 [`l1_manager/dram.py`](https://github.com/LMCache/LMCache/blob/dev/lmcache/v1/distributed/l1_manager/dram.py)::`DramL1Manager`("Pinned-DRAM L1 manager")。**Mooncake TE 可以注册 HBM 做 RH2D；这里的两跳是 LMCache 把落点选在 CPU L1，不是底层没有 RH2D。** LMCache 的 P2P 没有上报 VRAM 落点：`api.py` 的 `MemoryResidency.VRAM` 注释写 "Reserved for GDS/GDR L2 adapters; nothing reports it yet"。
3. **PD disagg**:由引擎侧 `MultiConnector` 并列 `NixlConnector` 与 `LMCacheMPConnector` 组合,不是 LMCache 自己的后端。P、D 每个 vLLM 实例各一个 MP server,不能共用,见 [mp/disaggregated_prefill.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/disaggregated_prefill.rst)。

## 存储后端

### L1(`lmcache/v1/distributed/l1_manager/`)

| manager | 介质 | 说明 |
|---------|------|------|
| `DramL1Manager` | CPU DRAM | 默认 |
| `GdsL1Manager` | NVMe slab(cuFile/hipFile) | `--gds-l1-path` |
| `DevDaxL1Manager` | Device-DAX | `--l1-devdax-path` |
| `ShadowL1Manager` | 借来的对象 | 对端 MP server 拥有字节,本端只持租约;P2P 落地处 |

一个 MP server 可同时挂多个 L1(各带 `type` + 唯一 `tag` + `size_gb`),每个有自己的容量、驱逐与 store controller,L2 adapter 按其 memory type 自动绑定到某个 L1。见 [mp/multi_l1.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/multi_l1.rst)。接口是 `lmcache/v1/distributed/internal_api.py`::`L1ManagerInterface`(`reserve_read` / `unsafe_read` / `finish_read` / `reserve_write` / `finish_write` / `delete` …),基类 `l1_manager/base.py`::`BaseL1Manager`。

### L2 adapter(`lmcache/v1/distributed/l2_adapters/`)

抽象:`base.py`::`L2AdapterInterface` — 事件驱动,分三条 lane:`submit_store_task` / `submit_lookup_and_lock_task` / `submit_load_task`,各自一对 eventfd 与 query 方法,另有 `submit_unlock`、`delete`、`get_usage`、`report_status`。

注册:`config.py`::`register_l2_adapter_type` + `factory.py`::`register_l2_adapter_factory`;`__init__.py` 按 `*_l2_adapter.py` 后缀 pkgutil 自动发现。已注册 type 见 [supported_storages.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/l2_storage/supported_storages.rst):`nixl_store` / `nixl_store_dynamic`、`nixl_native`、`fs` / `fs_native`、`raw_block`、`bigtable`、`sagemaker-hyperpod`、`s3`、`hfbucket`、`mooncake_store`、`hf3fs`、`resp`、`valkey`、`aerospike`、`wqs_store`、`dax`、`mock`、`fault_inject`、`plugin` / `native_plugin`。

serde 由 `l2_adapters/serde_wrapper.py`::`SerdeL2AdapterWrapper` 透明包裹,per-adapter opt-in。

### 存储侧 native connector(`csrc/storage_backends/`)

C++ 写一次,MP 与 InProcess 两种模式复用——这是与"引擎侧 KV connector"完全不同的东西(两者区分见 [overview.md](overview.md#两种-connector别混))。

| 契约 | 符号 |
|------|------|
| 顶层抽象 | `connector_interface.h`::`IStorageConnector` — `submit_batch_get` / `submit_batch_set` / `submit_batch_exists` / `submit_batch_delete` + `drain_completions` + `event_fd` + `close` |
| 实现基类 | `connector_base.h`::`ConnectorBase<ConnectionType>` — 覆盖 4 个必需方法 `create_connection` / `do_single_get` / `do_single_set` / `do_single_exists`,可选 `do_single_delete` |
| 工作池 | `connector_base.h`::`WorkerPoolConfig{per_op_workers}` — lane key(`lookup`/`retrieve`/`store`)→ 线程数,未列出的走共享池 |
| Python 桥(MP) | `lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py`::`NativeConnectorL2Adapter` — 3 个 eventfd + demux 线程 + `ObjectKey` 序列化 + 客户端侧锁跟踪 |
| Python 桥(InProcess) | `ConnectorClientBase`(asyncio),随 InProcess 移出主仓 |

六份实现:`aerospike/`、`fs/`、`hf3fs/`、`mooncake/`、`nixl/`、`redis/`(`redis/` 是 RESP2 over TCP 的参考实现,`aerospike/` 需 `BUILD_AEROSPIKE=1`)。上游 README 的一句话:["Build high-performance storage connectors in C++/Rust that work in both LMCache modes… Write the connector once, get both modes for free."](https://github.com/LMCache/LMCache/blob/dev/csrc/storage_backends/README.md)

**C++ 原因**:GIL 在 pybind 层释放、批量请求按 tiling 均分给线程、eventfd 唤醒而非轮询、SQ/CQ 非阻塞提交。

## 分层与配额

- **介质**:L1 默认是 **CPU pinned DRAM**(`l1_manager/dram.py`::`DramL1Manager`,docstring "Pinned-DRAM L1 manager"),另有 `GdsL1Manager`(NVMe slab,走 GPUDirect **Storage**)与 `DevDaxL1Manager`(Device-DAX slab)。**L1 不在 GPU 显存里**,引擎 HBM 只是 KV 的来源与去向(`gpu_connector/` 负责 H2D/D2H)。**LMCache 看不见 HBM/APC**；HBM 命中仍由引擎自己的 APC 判断。
- `distributed/storage_manager.py`::`StorageManager` + 多 `L1Manager` + 多 `L2Adapter` + `storage_controllers/` 下的 `StoreController` / `PrefetchController` / `EvictionController`。tier 词汇 `distributed/api.py`::`Tier{L1,L2,ALL}`。
- `QuotaManager`(`distributed/quota_manager.py`)按 `cache_salt` 记配额,HTTP 面在 MP server 的 `/quota/{cache_salt}`。
- `storage_controllers/store_policy.py`::`StorePolicy` 决定写哪些 adapter。
- 驱逐由 L1 水位触发(`EvictionController`),L1 逐出后 L2 副本仍在。

## MP coordinator(中央协调器)

`lmcache/v1/mp_coordinator/`(独立 FastAPI 进程,`lmcache coordinator` 启动,默认 `127.0.0.1:9300`,见 [mp/coordinator.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/coordinator.rst)):

- **职责**:**元数据协调,不是数据路径**。数据仍在 P2P 或共享存储上流动;P2P 文档原话是 "It only manages membership — it never sees KV data or participates in lookups."
- **可选**:`lmcache/v1/multiprocess/config.py`::`CoordinatorConfig.url` 默认 `""`,空则不注册、行为与没有 coordinator 一致;`event_reporting` 默认 `False`。不接 coordinator 就没有 `/cache/pins`(pin 计数由 `FleetEvictionController` 持有),只剩 prefetch / delete / quota / clear。
- **fleet 视图**:`views/key_directory.py::KeyDirectory` 是 **L1/L2 目录**,记录 `ObjectKey → placements(instance_id, tier, backend, size)`,由 cache event 构建,**最终一致,答案只是 hint,必须在 owner 侧验证**。L1 placement 随上报者 incarnation 失效,L2 placement 保留;目录可答“在哪”，但当前 P2P 取数仍向每个 MP server 查。
- **命令面**(HTTP):`POST /cache/prefetches`(指定 `instance_id` 预热)、`POST|GET|DELETE /cache/pins`(排除在该协调器自己的 L2 驱逐计划外)、`POST /cache/delete`、`/quota*`、`/directory/{lookup,blend-lookup,keys,stats}`、`/instances*`、`/events`、`/config/chunk-size`。**无 move 路由。**
- **组件**:`controllers/base.py::Controller`(控制器基类)、`controllers/prefetch_manager.py::PrefetchManager`(预取派发)、`controllers/eviction_controller.py::FleetEvictionController`(配额 / 驱逐 / pin 计数)、`views/instance_registry.py::InstanceRegistry`(注册与心跳)、`ingest/event_gate.py::EventGate`(事件准入、去重、incarnation fencing)、`discovery.py::Registry`(controller / view 发现)、`persistence/checkpoint.py`(目录与配额的 checkpoint)。
- **通信**:HTTP(FastAPI);事件可经 HTTP 或 Kafka 源进入(`ingest/{http,kafka}_event_source.py`)。

## 压缩/serde

`lmcache/v1/distributed/serde/`,per-adapter opt-in,经 `SerdeL2AdapterWrapper` 透明包裹。已注册 type:

| type | 做什么 | 关键配置 |
|------|--------|---------|
| `fp8` | 逐元素量化到 8-bit float,载入时反量化 | `fp8_dtype`(默认 `float8_e4m3fn`) |
| `turboquant` | TurboQuant preset 压缩后存储、载入重建 | `preset`(默认 `turboquant_k8v4`)、`head_dim`、`block_size` |
| `aesgcm` | L2 静态数据的 AES-GCM 认证加密,按 `cache_salt` 派生密钥 | `key_provider`、`master_key_path`、`aes_bits` |

另有 `asym_k16_v8`(K/V 非对称量化,`MultiSerializer` 族)与 `multi`(组合多个 serde)。统一接口 `Serializer.serialize(src,dst)→bytes` / `Deserializer`。

## 一致性

**无全局强一致**。fleet 视图靠 cache event 批次 + 实例心跳构建:事件经 `EventGate` 按 per-emitter 光标排序、去重、按 incarnation fencing 后才进各 view;目录可 checkpoint 并从事件流重放(`events_replay.py`)。答案只是 hint,字节可用性在 owner MP server 侧验证。数据层靠内容寻址天然去重;L2 靠显式删除,L1 由各 server 本地淘汰。

## Rust 组件

`rust/raw_block/`(PyO3 绑定)— 低层裸设备 I/O:
- `RawBlockDevice`:`pwrite_from_buffer`/`pread_into`。
- 后端:`posix`(同步 pread/pwrite)、`io_uring`(Rust 原生 syscall)、`io_uring_cmd`(NVMe passthrough 直通)。
- 上层 `lmcache/v1/distributed/l2_adapters/raw_block/core.py`::`RawBlockCore`,由 `RawBlockL2Adapter` 适配(`--l2-adapter '{"type":"raw_block",...}'`)。
- **是存储后端(裸块设备 SSD),非传输层,生产可用**。

印证 Rust 适合写存储层 I/O;可参考其 Rust/C++ 桥接(PyO3)与 FFI 模式。

## 代码索引

> 沿代码回溯用。符号名锚定,行号会漂移——找不到时 `grep -n "符号名" <文件>`。

| 机制 | 文件:符号 |
|------|-----------|
| 缓存键(内容寻址) | `lmcache/v1/distributed/api.py`::`ObjectKey` / `ObjectKeyIdentity` |
| token 切分 + 链式哈希 | `lmcache/v1/common/token_hasher.py`::`TokenHasher.compute_chunk_hashes`(默认 chunk_size=256, blake3) |
| L2 key 编码 | `lmcache/v1/distributed/key_codec.py`::`encode_storage_key` / `decode_storage_key` |
| Python 后端抽象 | `lmcache/v1/distributed/l2_adapters/base.py`::`L2AdapterInterface` |
| L2 adapter 注册 | `lmcache/v1/distributed/l2_adapters/config.py`::`register_l2_adapter_type`;`factory.py`::`register_l2_adapter_factory` |
| L1 manager 抽象 | `lmcache/v1/distributed/internal_api.py`::`L1ManagerInterface`;`l1_manager/base.py`::`BaseL1Manager` |
| L1 各介质 | `DramL1Manager` / `GdsL1Manager` / `DevDaxL1Manager` / `ShadowL1Manager`;工厂 `l1_manager/factory.py`::`create_l1_managers` |
| MP 存储管理器 | `lmcache/v1/distributed/storage_manager.py`::`StorageManager` |
| L1/L2 控制器 | `lmcache/v1/distributed/storage_controllers/`::`StoreController` / `PrefetchController` / `EvictionController` |
| tier 枚举 | `lmcache/v1/distributed/api.py`::`Tier{L1,L2,ALL}` |
| 配额管理 | `lmcache/v1/distributed/quota_manager.py`::`QuotaManager` |
| 写策略 | `lmcache/v1/distributed/storage_controllers/store_policy.py`::`StorePolicy` |
| serde 包装(adapter 之上) | `lmcache/v1/distributed/l2_adapters/serde_wrapper.py`::`SerdeL2AdapterWrapper` |
| serde 类型 | `lmcache/v1/distributed/serde/`::`fp8` / `aesgcm` / `turboquant` / `asym_k16_v8` / `multi` |
| 跨实例 P2P adapter | `lmcache/v1/distributed/l2_adapters/p2p_l2_adapter.py`::`P2PL2Adapter` |
| P2P 控制器(server 侧) | `lmcache/v1/multiprocess/modules/p2p_controller.py`::`P2PController` |
| 传输通道 | `lmcache/v1/distributed/transfer_channel/impl/nixl_impl.py`::`NixlTransferChannelClient` / `mooncake_te_impl.py`::`MooncakeTeTransferChannelClient`;工厂 `transfer_channel/factory.py`::`create_transfer_channel_context` |
| C++ native connector 抽象 | `csrc/storage_backends/connector_interface.h`::`IStorageConnector` |
| C++ native connector 基类 | `csrc/storage_backends/connector_base.h`::`ConnectorBase<T>`、`WorkerPoolConfig` |
| C++ 已实现后端 | `csrc/storage_backends/{aerospike,fs,hf3fs,mooncake,nixl,redis}/` |
| native connector 的 Python 桥 | `lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py`::`NativeConnectorL2Adapter` |
| coordinator fleet 目录（L1/L2） | `lmcache/v1/mp_coordinator/views/key_directory.py`::`KeyDirectory.lookup` / `fence_instance` |
| coordinator 控制器基类 | `lmcache/v1/mp_coordinator/controllers/base.py`::`Controller` |
| 预取派发 | `lmcache/v1/mp_coordinator/controllers/prefetch_manager.py`::`PrefetchManager.submit_prefetch` |
| 配额 / 驱逐 / pin | `lmcache/v1/mp_coordinator/controllers/eviction_controller.py`::`FleetEvictionController.pin` / `unpin` |
| 实例注册/心跳 | `lmcache/v1/mp_coordinator/views/instance_registry.py`::`InstanceRegistry` + `registrar.py`::`keep_registered` |
| 事件管线 | `lmcache/v1/mp_coordinator/cache_events.py`::`CacheEventSink`;`ingest/event_gate.py`::`EventGate` |
| HTTP 面 | `lmcache/v1/mp_coordinator/http_apis/{cache_api,directory_api,instances_api}.py`;`controllers/eviction_http_api.py` |
| 服务端入口 | `lmcache/v1/mp_coordinator/app.py`::`create_app` |
| coordinator 可选开关 | `lmcache/v1/multiprocess/config.py`::`CoordinatorConfig`(`url=""` 不注册) |
| Rust 裸设备 I/O | `rust/raw_block/src/lib.rs`::`RawBlockDevice`(`pwrite_from_buffer`/`pread_into`;posix/io_uring/io_uring_cmd) |
| Rust 上层 | `lmcache/v1/distributed/l2_adapters/raw_block/core.py`::`RawBlockCore` + `raw_block_l2_adapter.py`::`RawBlockL2Adapter` |
