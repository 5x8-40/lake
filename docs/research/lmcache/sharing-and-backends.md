# LMCache — 跨实例复用与存储后端

> 源码:`lmcache/v1/`、`csrc/storage_backends/`、`rust/`、`lmcache/v1/mp_coordinator/`(2026-11-13 快照 `58c11e38`)。

## 缓存键与匹配

- **键**:`CacheEngineKey` = `(model_name, world_size, worker_id, chunk_hash, dtype, request_configs)`(non-MP);MP 的 `ObjectKey` = `(chunk_hash, model_name, kv_rank, object_group_id)`。
- **切分与哈希**:`ChunkedTokenDatabase` 按固定 `chunk_size`(默认 256)切 token,**链式 prefix hash**(每 chunk hash = `hash(prev_hash, chunk_tokens)`)。另有 `SegmentTokenDatabase`(按特殊分隔符切段)。
- **匹配**:无 radix tree(在 vLLM 侧);LMCache 靠 `batched_contains` 从第 0 个 chunk 起顺序探测,断链即停。MP 的 `chunk_hash` 是内容哈希,故 L2 层内容寻址。
- **跨进程一致**:`token_database.py` 强调需 `PYTHONHASHSEED=0`,否则哈希跨进程不一致。

## 跨实例共享(两条路径)

1. **共享远程存储(L2 adapter)**:多实例共用 Redis/Mooncake/S3/Aerospike/NIXL-store,通过 `chunk_hash` 内容寻址天然去重。实例只读写同一 key。
2. **P2P 直传(CPU 内存共享)**:`P2PBackend` 经 `CreateTransferChannel`:
   - `NixlChannel`:底层 NIXL(RDMA/NVLink/TCP)。
   - `PySocketChannel`:ZMQ 控制面 + 原生 socket 数据面。
   - "谁有哪些 chunk"由 MP coordinator 的 `KeyDirectory` 提供(事件派生、最终一致)。
   - PD disagg 走 `NixlStorageBackend`/`pd_backend(_async)`。

## 存储后端

### Python 后端抽象(`lmcache/v1/storage_backend/abstract_backend.py`)

| 接口 | 核心方法 |
|------|----------|
| `StorageBackendInterface` | `contains` / `batched_submit_put_task` / `get_blocking` / `get_non_blocking` / `pin` / `unpin` / `remove` / `touch_cache` |
| `AllocatorBackendInterface` | `allocate` / `batched_allocate` / `get_memory_allocator` |
| `StoragePluginInterface` | 可插拔后端,从 config 加载 |

工厂:`CreateStorageBackends` 按 config 组装 `LocalCPUBackend` / `LocalDiskBackend` / `P2PBackend` / `NixlStorageBackend` / `RemoteBackend`。

### Python 后端实例

| 后端 | 文件 | 说明 |
|------|------|------|
| LocalCPUBackend | `local_cpu_backend.py` | L1 热缓存,pinned CPU 内存,LRU/自定义 cache_policy |
| LocalDiskBackend | `local_disk_backend.py` | 本地 SSD |
| RemoteBackend | `remote_backend.py` | 远程存储,封装 `ConnectorClientBase`(asyncio) |
| P2PBackend | `p2p_backend.py` | 跨实例 P2P(NIXL/socket) |
| NixlStorageBackend | `nixl_storage_backend.py` | NIXL 抽象后端(PD disagg 传输) |
| GDSBackend | `gds_backend.py` | GPUDirect Storage |

### C++ 原生后端(`csrc/storage_backends/`)

- 抽象:`IStorageConnector`(`submit_batch_get/set/exists/delete` + `drain_completions` + `close`,eventfd 驱动)、`ConnectorBase<T>`(模板基类,SQ/CQ + worker thread pool + tiling 分片)。
- 已实现:`redis/`(RESP2 over TCP,参考实现)、`aerospike/`(`BUILD_AEROSPIKE=1`)、`mooncake/`、`fs/`(本地文件)。
- **C++ 原因**:GIL-free 真并发、批量+tiling 减少提交/完成次数、eventfd 非轮询完成。一次实现同时服务 non-MP 与 MP 两模式。

### MP 模式 L2 adapter(`lmcache/v1/distributed/l2_adapters/`)

`L2AdapterInterface`(`base.py`)抽象,`StorageManager` 编排多级 L2 + serde 包装(`SerdeL2AdapterWrapper`)。已注册:`aerospike / dax / fs / fs_native / hfbucket / mock / mooncake_store / native_connector / native_plugin / nixl_store / nixl_store_dynamic / p2p / plugin / raw_block / resp / s3`。

## 分层与配额

- Non-MP:`StorageManager` 串联 LocalCPU(L1)→ LocalDisk/Remote(L2)。
- MP:`distributed/storage_manager.py::StorageManager` + `L1Manager` + 多 `L2Adapter` + `StoreController`/`PrefetchController`/`L1EvictionController`/`L2EvictionController`。tier 词汇 `distributed/tiers.py::Tier{L1,L2,ALL}`。`QuotaManager` 管配额,`store_policy` 决定写哪些 adapter。

## MP coordinator(中央协调器)

`lmcache/v1/mp_coordinator/`(独立 FastAPI 进程,`lmcache coordinator` 启动,默认 `127.0.0.1:9300`,见 `docs/source/mp/coordinator.rst`):

- **职责**:**元数据协调,不是数据路径**。数据仍在 P2P 或共享存储上流动。
- **fleet 视图**:`views/key_directory.py::KeyDirectory` — `ObjectKey → placements(instance_id, tier, backend, size)`,由 cache event 构建,**最终一致,答案只是 hint,必须在 owner 侧验证**。
- **命令面**(HTTP):`POST /cache/prefetches`(指定 `instance_id` 预热)、`POST|GET|DELETE /cache/pins`(排除在该协调器自己的 L2 驱逐计划外)、`POST /cache/delete`、`/quota*`、`/directory/{lookup,blend-lookup,keys,stats}`、`/instances*`、`/events`、`/config/chunk-size`。**无 move 路由。**
- **组件**:`controllers/base.py::Controller`(控制器基类)、`controllers/prefetch_manager.py::PrefetchManager`(预取派发)、`controllers/eviction_controller.py::FleetEvictionController`(配额 / 驱逐 / pin 计数)、`views/instance_registry.py::InstanceRegistry`(注册与心跳)、`ingest/event_gate.py::EventGate`(事件准入、去重、fencing)、`discovery.py::Registry`(controller / view 发现)、`persistence/checkpoint.py`(目录与配额的 checkpoint)。
- **通信**:HTTP(FastAPI);事件可经 HTTP 或 Kafka 源进入(`ingest/{http,kafka}_event_source.py`)。

## 压缩/serde

- **Non-MP**(`lmcache/v1/storage_backend/naive_serde/`):`NaiveSerializer`(原始)、`CacheGenSerializer/Deserializer`(KV 流式压缩,CUDA kernel + 算术编码)、`KIVISerializer`(2-bit 量化)。
- **MP**(`lmcache/v1/distributed/serde/`):`fp8`、`asym_k16_v8`、`turboquant/`(store/decode kernel)、`multi`(组合)。经 `SerdeL2AdapterWrapper` 透明包裹 L2 adapter。
- 统一接口:`Serializer.serialize(src,dst)→bytes`、`Deserializer`。

## 一致性

**无全局强一致**。fleet 视图靠 cache event 批次 + 实例心跳构建:事件经 `EventGate` 按 per-emitter 光标排序、去重、fencing 后才进各 view;目录可 checkpoint 并从事件流重放(`events_replay.py`)。答案只是 hint,字节可用性在 owner MP server 侧验证。数据层靠内容寻址天然去重;失效靠 L1 上报者 incarnation 与 L2 显式删除。

## Rust 组件

`rust/raw_block/`(PyO3 绑定)— 低层裸设备 I/O:
- `RawBlockDevice`:`pwrite_from_buffer`/`pread_into`。
- 后端:`posix`(同步 pread/pwrite)、`io_uring`(Rust 原生 syscall)、`io_uring_cmd`(NVMe passthrough 直通)。
- 被 `RustRawBlockBackend`(non-MP)与 `RawBlockL2Adapter`→`RawBlockCore`(MP)共用。
- **是存储后端(裸块设备 SSD),非传输层,生产可用**。

印证 Rust 适合写存储层 I/O;可参考其 Rust/C++ 桥接(PyO3)与 FFI 模式。

## 代码索引

> 沿代码回溯用。符号名锚定,行号会漂移——找不到时 `grep -n "符号名" <文件>`。

| 机制 | 文件:符号 |
|------|-----------|
| 缓存键(non-MP) | `lmcache/utils.py`::`CacheEngineKey` |
| token 切分 + 链式哈希 | `lmcache/v1/token_database.py`::`ChunkedTokenDatabase`(默认 chunk_size=256) |
| 段切分 | `lmcache/v1/token_database.py`::`SegmentTokenDatabase` |
| MP 分布式键(内容寻址) | `lmcache/v1/distributed/api.py`::`ObjectKey` |
| 顺序探测匹配 | `abstract_backend.py`::`StorageBackendInterface.batched_contains` |
| Python 后端抽象 | `lmcache/v1/storage_backend/abstract_backend.py`::`StorageBackendInterface` / `AllocatorBackendInterface` / `StoragePluginInterface` |
| 后端工厂 | `lmcache/v1/storage_backend/__init__.py`::`CreateStorageBackends` |
| L1 CPU 热缓存 | `lmcache/v1/storage_backend/local_cpu_backend.py`::`LocalCPUBackend` |
| L2 本地盘 | `lmcache/v1/storage_backend/local_disk_backend.py`::`LocalDiskBackend` |
| 远程后端(asyncio) | `lmcache/v1/storage_backend/remote_backend.py`::`RemoteBackend` / `ConnectorClientBase` |
| P2P 跨实例 | `lmcache/v1/storage_backend/p2p_backend.py`::`P2PBackend`::`CreateTransferChannel` |
| 传输通道 | `lmcache/v1/transfer_channel/nixl_channel.py`::`NixlChannel` / `py_socket_channel.py`::`PySocketChannel` |
| NIXL/PD 传输后端 | `lmcache/v1/storage_backend/nixl_storage_backend.py`::`NixlStorageBackend` + `pd_backend.py` |
| GDS 后端 | `lmcache/v1/storage_backend/gds_backend.py`::`GDSBackend` |
| C++ 后端抽象 | `csrc/storage_backends/connector_interface.h`::`IStorageConnector` |
| C++ 后端基类(SQ/CQ+tiling) | `csrc/storage_backends/connector_base.h`::`ConnectorBase<T>` |
| C++ 已实现后端 | `csrc/storage_backends/{redis,aerospike,mooncake,fs}/` |
| MP L2 adapter 抽象 | `lmcache/v1/distributed/l2_adapters/base.py`::`L2AdapterInterface` |
| MP 存储管理器 | `lmcache/v1/distributed/storage_manager.py`::`StorageManager` + `L1Manager` + `StoreController`/`PrefetchController`/`L1EvictionController`/`L2EvictionController` |
| tier 枚举 | `lmcache/v1/distributed/tiers.py`::`Tier{L1,L2,ALL}` |
| 配额管理 | `lmcache/v1/distributed/`(QuotaManager / `store_policy`) |
| serde 包装(adapter 之上) | `lmcache/v1/distributed/`(SerdeL2AdapterWrapper) |
| Non-MP serde | `lmcache/v1/storage_backend/naive_serde/`::`NaiveSerializer` / `CacheGenSerializer` / `KIVISerializer` |
| MP serde | `lmcache/v1/distributed/serde/`::`fp8` / `asym_k16_v8` / `turboquant` / `multi` |
| coordinator fleet 目录 | `lmcache/v1/mp_coordinator/views/key_directory.py`::`KeyDirectory.lookup` |
| coordinator 控制器基类 | `lmcache/v1/mp_coordinator/controllers/base.py`::`Controller` |
| 预取派发 | `lmcache/v1/mp_coordinator/controllers/prefetch_manager.py`::`PrefetchManager.submit_prefetch` |
| 配额 / 驱逐 / pin | `lmcache/v1/mp_coordinator/controllers/eviction_controller.py`::`FleetEvictionController.pin` / `unpin` |
| 实例注册/心跳 | `lmcache/v1/mp_coordinator/views/instance_registry.py`::`InstanceRegistry` + `registrar.py`::`keep_registered` |
| 事件管线 | `lmcache/v1/mp_coordinator/cache_events.py`::`CacheEventSink`;`ingest/event_gate.py`::`EventGate` |
| HTTP 面 | `lmcache/v1/mp_coordinator/http_apis/{cache_api,directory_api,instances_api}.py`;`controllers/eviction_http_api.py` |
| 服务端入口 | `lmcache/v1/mp_coordinator/app.py`::`create_app` |
| Rust 裸设备 I/O | `rust/raw_block/src/lib.rs`::`RawBlockDevice`(`pwrite_from_buffer`/`pread_into`;posix/io_uring/io_uring_cmd) |
| Rust I/O 后端(non-MP/MP 共用) | `RustRawBlockBackend`(non-MP) + `RawBlockL2Adapter`→`RawBlockCore`(MP) |
