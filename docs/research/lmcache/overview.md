# LMCache — 总览

> 源码:`3rdparty/lmcache`(submodule),gitlink 固定 `9229067c`(2026-07-13;该快照下 `cache_engine.py`、`storage_backend/`、`token_database.py`、`cache_controller/` 均在)。本文的路径与符号对照上游 `58c11e38`(2026-11-13,`v0.5.8rc1` 之后 6 个 commit),与 gitlink 不是同一棵树。
> 上游默认分支是 `dev`(`main` 已改名 `main-deprecate`),外链一律指 `dev`。

## 一句话定位

LMCache 是一个 **vendor-neutral 的 LLM KV cache 管理层**,把 GPU 上一次性的 KV cache 变成可跨请求、跨实例、跨引擎持久存储/复用/观测/变换的"AI-native knowledge",目标是降 TTFT、提吞吐(尤其长上下文/agent/RAG 场景)。

## 时间线

| 日期 | commit | 事件 |
|---|---|---|
| 2025-11-17 | `7aad1f8e` (#1907) | 第一个独立 cache server:`Example LMCache independent cache server for multi-process mode`,MP 线起点 |
| 2026-05-13 | `b16de35f` (#3235) | `lmcache_mp_connector.py` 落地,引擎可连独立 server |
| 2026-06-03 | `84df850c` (#3488) | MP coordinator 骨架 |
| 2026-10-21 | `032e23da` (#5831) | vLLM in-process 集成隔离,挪进 `lmcache/inprocess/` |
| 2026-10-22 | `2d4f4bdf` (#5970) | `cache_controller/` → `inprocess/v1/` |
| 2026-11-06 | `5b9db86f` (#6254) | 树内 InProcess 实现移除,迁到独立仓 [`LMCache_inprocess`](https://github.com/LMCache/LMCache_inprocess) |
| 2026-11-12 | tag `v0.5.8rc1` | 撰写时最新发布 |

## 设计哲学

- **问题**:推理引擎(vLLM/SGLang)的 KV cache 是进程内临时状态,prefill 是 TTFT 瓶颈;引擎崩溃即丢失。
- **定位**:KV cache 从"临时状态"升级为"可管理资产"——stored(分层持久化)、reused(跨请求/会话/实例)、monitored(可观测)、transformed(压缩/丢弃/serde)。
- **与引擎的关系**:引擎侧一个 connector 插件 + 引擎旁一个独立服务。MP 模式下服务与引擎无 fate-sharing;同时声称 vendor-neutral,集成 vLLM、SGLang、TensorRT-LLM、NIXL 等。
- **核心目标**:跨请求/跨实例复用。支持 prefix caching、non-prefix reuse(CacheBlend)、跨实例 P2P 内存共享、PD disagg。

## 架构

### 运行模式

| 模式 | 说明 | 状态 |
|------|------|------|
| **MP**(多进程) | 独立服务 `MPCacheServer`,引擎经 ZMQ 或 gRPC 请求面访问;`StorageManager` 管 L1 与多个 L2 adapter | 树内唯一路径 |
| **InProcess**(单进程) | `LMCacheEngine` 跑在推理进程内,`CacheEngine → StorageManager → LocalCPUBackend + RemoteBackend` | 已移出主仓,见 [`LMCache_inprocess`](https://github.com/LMCache/LMCache_inprocess) |

### 新版结构

三个进程角色,上游各有专页:

| 角色 | 位置 | 职责 | 上游文档 |
|------|------|------|---------|
| 引擎侧 connector | 引擎进程内 | 实现引擎自己的 KV connector 接口,把 KV 存取翻译成对 server 的 RPC | [mp/index.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/index.rst) |
| MP server | 每节点一个独立进程 | L1/L2 存储与读写、chunk 哈希、prefetch 调度 | 同上 |
| MP coordinator | 独立进程,**可选** | fleet 级实例注册 + 事件派生的 key 目录 + 命令面 | [mp/coordinator.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/coordinator.rst) |

上游对 MP 的一句定义:**"LMCache multiprocess (MP) mode runs LMCache as a standalone service that vLLM instances reach through a configurable ZMQ or gRPC request transport. One LMCache server per node can serve multiple vLLM pods."** 上游列的收益是进程隔离、推理路径上没有 GIL 争用、同节点多 pod 共享一份 L1、CPU 内存可独立于显存扩缩。架构图见 [mp/architecture.png](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/architecture.png)。

引擎侧 connector 三家:

| 引擎 | 文件:符号 |
|------|-----------|
| vLLM | `lmcache/integration/vllm/lmcache_mp_connector.py`::`LMCacheMPConnector(KVConnectorBase_V1, SupportsHMA)` |
| SGLang | `lmcache/integration/sglang/unified_lmcache_mp_connector.py`::`UnifiedLMCacheMPConnector` |
| TensorRT-LLM | `lmcache/integration/tensorrt_llm/tensorrt_mp_adapter.py`::`LMCacheMPKvConnectorScheduler` / `LMCacheMPKvConnectorWorker` |

vLLM 侧另有 scheduler 适配层 `lmcache/integration/vllm/vllm_multi_process_adapter.py`::`LMCacheMPSchedulerAdapter`(持有 `server_urls`、`zmq.Context`)。

MP server 内部:`MPCacheServer` 只是 compositor(`_build_modules()` 按 `--engine-type` 与 `--supported-transfer-mode` 装配 `DiscoverableModule`),模块在 `lmcache/v1/multiprocess/modules/`:`blend`、`engine_driven_transfer`、`kv_events`、`lmcache_driven_transfer`、`lookup`、`management`、`p2p_controller`、`server_transfer`。HTTP 面在 `http_apis/`:`/cache/objects`(GET/DELETE)、`/cache/prefetches`、`/cache/clear`、`/cache/checksums`、`/quota/{cache_salt}`、`/status`、`/healthcheck`、`/config`、`/lmc_version`。**MP server 上没有 `/cache/pins`。**

Coordinator 的三条边界:

- **可选**。`lmcache/v1/multiprocess/config.py`::`CoordinatorConfig` 默认 `url = ""`,docstring 写明:"When :attr:`url` is empty, the server registers with no coordinator and runs exactly as before." 事件上报默认也关(`event_reporting = False`)。
- **pin 只在 coordinator 上。** 不接 coordinator 就只有 prefetch / delete / quota / clear,没有 pin(`/cache/pins` 由 `mp_coordinator/controllers/eviction_http_api.py`::`request_pin` 提供)。pin 的语义是"排除在该 coordinator 自己的 L2 驱逐计划外"的计数,见 `controllers/eviction_controller.py`::`FleetEvictionController._pin_counts`。
- **P2P 依赖 coordinator,且与 GDS/Device-DAX L1 互斥。** 上游写明 "A server started with `--p2p-advertise-url` but no `--coordinator-url` refuses to start";传输通道要注册一整段连续 L1,所以 `--gds-l1-path` 与 `--l1-devdax-path` 下拒绝启动。三条组件(Coordinator 只管成员、server 里的 P2P controller、RDMA transfer channel)见 [mp/p2p.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/p2p.rst)。

### 两种 connector(别混)

"connector" 在这套代码里指两个完全不同的东西:

| | 引擎侧 KV connector | 存储侧 native connector |
|---|---|---|
| 位置 | 引擎进程内 | C++ 库,pybind 暴露;MP 模式下由 server 进程加载 |
| 接口 | 引擎的 KV connector 接口(vLLM `KVConnectorBase_V1`、SGLang、TensorRT-LLM 各自一套) | `csrc/storage_backends/connector_base.h`::`ConnectorBase<T>` |
| 必须实现 | `get_num_new_matched_tokens` / `start_load_kv` / `wait_for_layer_load` / `save_kv_layer` / `request_finished` | `create_connection` / `do_single_get` / `do_single_set` / `do_single_exists`(可选 `do_single_delete`) |
| 版本耦合 | 有,跟引擎的 KV-cache API 走 | 无,只认 key 与字节 |
| 份数 | 每引擎一份 | 六份:`aerospike` `fs` `hf3fs` `mooncake` `nixl` `redis` |

native connector 的契约只有上面四个方法,`csrc/storage_backends/README.md` 的一句话是:"Write the connector once, get both modes for free"——同一份 C++ 实现经 `NativeConnectorL2Adapter` 服务 MP,另一条入口是 `ConnectorClientBase`(asyncio),随 InProcess 移出主仓。设计要点是四件事:GIL 在 pybind 层释放、批量请求按 tiling 分给线程、eventfd 唤醒而非轮询、提交非阻塞。详见 [native_connectors.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/developer_guide/extending_lmcache/native_connectors.rst)。

`lmcache/v1/gpu_connector/` 是第三个无关的东西:GPU KV 布局与 GDS backend 选择(`gds_backends/`、`gds_context.py`、`gpu_ops.py`),不属于上面任一类。

### 核心数据流

| 阶段 | 步骤 |
|------|------|
| 存 | 引擎逐层 `save_kv_layer` → server `STORE` → 写入 L1;`StoreController` 随后异步推 L2 adapter |
| 取 | `get_num_new_matched_tokens` → server `LOOKUP`(server 侧起 prefetch job)→ `QUERY_PREFETCH_STATUS` 或 `WAIT_PREFETCH_STATUS` 拿命中 token 数 → `start_load_kv` → `RETRIEVE` → `wait_for_layer_load` 逐层同步 |

两种传输模式由 `--supported-transfer-mode` 选:`lmcache_driven`(CUDA IPC,server 侧主动 copy)与 `engine_driven`(worker 走 `PREPARE_STORE`/`COMMIT_STORE`、`PREPARE_RETRIEVE`/`COMMIT_RETRIEVE`,字节内联或走 SHM)。上层模块因此分成 `lmcache_driven_transfer.py` 与 `engine_driven_transfer.py` 两份。

### 缓存键(内容寻址,但 rank 绑定)

- `ObjectKey` = `ObjectKeyIdentity{chunk_hash, kv_rank, model_name, object_group_id, cache_salt, tags}`(`lmcache/v1/distributed/api.py`);`extra_attributes` 显式排除在相等、哈希与 repr 之外。
- 切分与哈希:`TokenHasher`(`lmcache/v1/common/token_hasher.py`),默认 `chunk_size=256`、`hash_algorithm="blake3"`,**链式 prefix hash**(`compute_chunk_hashes`,每个 chunk 的 hash 依赖它前面全部 chunk)。
- **无 radix tree 做匹配**(radix 在引擎侧);LMCache 靠逐 chunk 顺序探测,断链即停。
- chunk size 每个 MP server 一份且不可变:connector 把各 KV group 的 token 跨度取 lcm 作为对齐要求,最终值是最小的 ≥ `--chunk-size` 的对齐倍数。多模型共享一个 server 要求该 size 也是新模型对齐的倍数,否则要另开 server(见 [chunk_size_negotiation.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/chunk_size_negotiation.rst))。
- L2 的 key 按 `encode_storage_key` 落成 `{model}@{rank}@{group}@{hash}` 加 tag 后缀(`lmcache/v1/distributed/key_codec.py`),内容寻址,同 model+rank+chunk 可被任意实例复用。

### 跨实例共享(两条路径)

1. **共享远程存储(L2 adapter)**:多实例共用 S3/HF3FS/Mooncake Store/Aerospike/Valkey/Bigtable/WQS 等,`chunk_hash` 内容寻址天然去重。上游把这批归为"适合跨节点共享"的一组,见 [l2_storage/remote_and_distributed.rst](https://github.com/LMCache/LMCache/blob/dev/docs/source/mp/l2_storage/remote_and_distributed.rst)。
2. **P2P 直传(L1 内存共享)**:`p2p_l2_adapter` 经 transfer channel 在实例间做单边 RDMA 读(`lmcache/v1/distributed/transfer_channel/`、`lmcache/v1/distributed/l2_adapters/p2p_l2_adapter.py`)。"谁有哪些 chunk"由 MP coordinator 的 `KeyDirectory` 提供(事件派生、最终一致)。

详见 [sharing-and-backends.md](sharing-and-backends.md)。

## 分布式模型

> 跨项目汇总对比见 [../distributed-models.md](../distributed-models.md);细节见 [sharing-and-backends.md](sharing-and-backends.md)。

- **拓扑**:两种并存。(a)共享存储为中心:多实例共用 Redis/Mooncake/S3 等 L2 adapter,`chunk_hash` 内容寻址天然去重,实例间无协调;(b)MP coordinator 星型:独立进程(`lmcache/v1/mp_coordinator/`)注册各 MP server、消费其 cache event、维护 fleet 级 `ObjectKey → placements` 目录,并暴露命令面(预取 / pin / 删除 / 配额)。它只在元数据面,不在数据路径上。
- **元数据权威**:`KeyDirectory`(`views/key_directory.py`)是**事件派生的最终一致视图**,模块 docstring 明确"lookups are hints to be validated at the owner"。不是权威记录——字节是否可用必须在 owner MP server 侧验证。
- **同步机制**:MP server 注册 + 心跳(`http_apis/instances_api.py`、`registrar.py::keep_registered`);cache event 批经 ingest gate 去重、排序、fencing 后喂给各 view(`ingest/event_gate.py::EventGate`);目录与配额可 checkpoint(`persistence/checkpoint.py`)并可回放(`events_replay.py::replay`)。
- **一致性**:无全局强一致。事件流可丢可乱,目录靠 gate 与 fencing 收敛;失效靠 L1 上报者 incarnation 与 L2 显式删除。
- **HA 与故障**:MP 模式下引擎崩溃 KV 不丢(无 fate-sharing);coordinator 可 checkpoint 恢复,未落盘的增量靠重放事件流补齐;coordinator 默认只监听回环,跨机部署需显式绑定并配 API key。
- **扩展性**:共享存储路径随后端扩展;coordinator 路径受单进程 `KeyDirectory`(全内存 dict + 反向索引)与事件总线扇出限制。
- **与 lake 对照**:LMCache 的 coordinator + MP server 形态最接近 lake 的 CP+agent,但一致性立场不同——LMCache 的目录是事件派生的 hint(允许错误,收敛即可),lake CP 为单写者权威(线性一致查询 + 镜像推送 + miss 回查)。此外 LMCache 的 KV 按 rank 入 key(部分实例私有),lake KV 不绑定 rank。

## 技术栈

- **语言**:Python(主体)+ C++/CUDA(`csrc/`,native storage connector + CacheGen/量化 kernel,带 SYCL 后端支持 AMD/Intel)+ **Rust**(`rust/raw_block/`)。
- **Rust 作用**:`rust/raw_block/` 是**低层裸设备 I/O 层**(PyO3 绑定),提供 `RawBlockDevice` 的 `pwrite_from_buffer`/`pread_into`,支持 `posix`(同步 pread/pwrite)与 `io_uring`(Rust 原生 syscall,含 `io_uring_cmd` NVMe passthrough)。上层是 `l2_adapters/raw_block/core.py`::`RawBlockCore`,由 `RawBlockL2Adapter` 适配成 L2 adapter。**是存储后端(裸块设备 SSD),不是传输层,生产可用**。
- **关键依赖**:vLLM(`KVConnectorBase_V1`)、NIXL(RDMA/NVLink 传输)、Mooncake、Redis/Valkey、msgspec、ZMQ、gRPC、PyTorch。
- **构建**:`pip install -e . --no-build-isolation`(需预装 torch);`NO_NATIVE_EXT=1`(纯源码)/`NO_GPU_EXT=1`(CPU-only C++)/`BUILD_WITH_HIP=1`(ROCm)/`BUILD_AEROSPIKE=1`(可选 Aerospike connector)。CMake + pybind。

## 代码索引

> 沿代码回溯用。符号名稳定锚定,行号会漂移——找不到时 `grep -n "符号名" <文件>`。

| 概念 | 文件:符号 |
|------|-----------|
| MP server(承载 KV 服务的进程) | `lmcache/v1/multiprocess/server.py`::`MPCacheServer`(compositor)+ `run_cache_server` |
| 存储管理器(串联 L1/L2) | `lmcache/v1/distributed/storage_manager.py`::`StorageManager` |
| L1 管理器 | `lmcache/v1/distributed/l1_manager/`::`DramL1Manager` / `GdsL1Manager` / `DevDaxL1Manager` / `ShadowL1Manager`(`base.py`::`BaseL1Manager`) |
| L1/L2 控制器 | `lmcache/v1/distributed/storage_controllers/`::`StoreController` / `PrefetchController` / `EvictionController` |
| L2 adapter 抽象 | `lmcache/v1/distributed/l2_adapters/base.py`::`L2AdapterInterface` |
| L2 adapter 注册 | `lmcache/v1/distributed/l2_adapters/config.py`::`register_l2_adapter_type`;`factory.py`::`register_l2_adapter_factory` |
| 配额 | `lmcache/v1/distributed/quota_manager.py`::`QuotaManager` |
| 缓存键(内容寻址) | `lmcache/v1/distributed/api.py`::`ObjectKey` / `ObjectKeyIdentity` |
| tier 枚举 | `lmcache/v1/distributed/api.py`::`Tier{L1,L2,ALL}` |
| token 切分 + 链式哈希 | `lmcache/v1/common/token_hasher.py`::`TokenHasher.compute_chunk_hashes` |
| L2 key 编码 | `lmcache/v1/distributed/key_codec.py`::`encode_storage_key` |
| P2P 跨实例 | `lmcache/v1/distributed/l2_adapters/p2p_l2_adapter.py`::`P2PL2Adapter` + `lmcache/v1/multiprocess/modules/p2p_controller.py`::`P2PController` |
| 传输通道 | `lmcache/v1/distributed/transfer_channel/`::`impl/nixl_impl.py`::`NixlTransferChannelClient`、`impl/mooncake_te_impl.py`::`MooncakeTeTransferChannelClient`;工厂 `factory.py`::`create_transfer_channel_context` |
| 协调器入口(元数据协调,非数据路径) | `lmcache/v1/mp_coordinator/app.py`::`create_app`;控制器基类 `mp_coordinator/controllers/base.py`::`Controller` |
| fleet key 目录 | `lmcache/v1/mp_coordinator/views/key_directory.py`::`KeyDirectory.lookup`(fencing:`fence_instance`) |
| 实例注册 / 心跳 | `lmcache/v1/mp_coordinator/views/instance_registry.py`::`InstanceRegistry`;`mp_coordinator/registrar.py`::`keep_registered` |
| 预取 / pin 命令面 | `mp_coordinator/http_apis/cache_api.py`::`request_prefetch`;`controllers/eviction_controller.py`::`FleetEvictionController`(`_pin_counts`)、`controllers/eviction_http_api.py`::`request_pin` |
| 目录查询 | `mp_coordinator/http_apis/directory_api.py`::`lookup_placements` |
| 事件管线 | `mp_coordinator/ingest/event_gate.py`::`EventGate`、`cache_events.py`::`CacheEventSink`、`events_replay.py`::`replay`、`persistence/checkpoint.py` |
| coordinator 可选开关 | `lmcache/v1/multiprocess/config.py`::`CoordinatorConfig.url=""` |
| vLLM 集成(拦截 KV) | `lmcache/integration/vllm/lmcache_mp_connector.py`::`LMCacheMPConnector` |
| vLLM scheduler 侧适配 | `lmcache/integration/vllm/vllm_multi_process_adapter.py`::`LMCacheMPSchedulerAdapter` |
| C++ native connector 抽象 | `csrc/storage_backends/connector_interface.h`::`IStorageConnector`(batch get/set/exists/delete + `drain_completions` + `close`) |
| C++ native connector 基类 | `csrc/storage_backends/connector_base.h`::`ConnectorBase<T>`(SQ/CQ + worker pool + tiling);`WorkerPoolConfig.per_op_workers` 按 lane 分池 |
| C++ 已实现后端 | `csrc/storage_backends/{aerospike,fs,hf3fs,mooncake,nixl,redis}/`(redis 为 RESP2 参考实现) |
| native connector 的 Python 桥 | `lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py`::`NativeConnectorL2Adapter` |
| Rust 裸设备 I/O | `rust/raw_block/src/lib.rs`::`RawBlockDevice`(posix/io_uring/io_uring_cmd);上层 `l2_adapters/raw_block/core.py`::`RawBlockCore` |
| serde | `lmcache/v1/distributed/serde/`::`fp8` / `aesgcm` / `turboquant` / `asym_k16_v8` / `multi`,经 `l2_adapters/serde_wrapper.py`::`SerdeL2AdapterWrapper` 包裹 |
| CacheBlend(non-prefix 复用) | `lmcache/v1/multiprocess/modules/blend/module.py`::`BlendModule` |

## 优势

1. **引擎解耦、无 fate-sharing** — MP 模式独立服务,引擎崩溃 KV 不丢;同节点多 pod 共享一份 L1。
2. **分层 + 可插拔后端丰富** — L1 有 DRAM/GDS/Device-DAX/Shadow 四种 manager,L2 是开放注册表(上游的 backend 表列了 19 行,含 `plugin` / `native_plugin` 两种 type);C++ native connector 写一次,两种运行模式都能用。
3. **内容寻址跨实例去重** — `ObjectKey.chunk_hash` 内容哈希,同 model+rank+chunk 自然全局共享,无需中心化协调数据归属。
4. **non-prefix 复用(CacheBlend)** — 选择性重算恢复质量,超出普通 prefix cache 能力。
5. **PD disagg 可组合** — 由 vLLM `MultiConnector` 同进程并列跑 `NixlConnector`(P→D 交接)与 `LMCacheMPConnector`(跨请求复用),复用顺序 local GPU → LMCache → 重算。
6. **C++ 后端性能工程** — GIL-free、eventfd 非轮询、批量 tiling、lookup/retrieve/store 分 lane 的工作池。
7. **生态广** — vLLM/SGLang/TensorRT-LLM/Dynamo/NIXL/Mooncake/Redis,已入 PyTorch Foundation。

## 劣势

1. **强依赖引擎的 hash/connector 抽象** — `TokenHasher._get_hash_func` 从 vLLM 载入 hash 实现,载不到退回 builtin 只 warn;版本兼容靠官方矩阵维护,不靠补丁目录。
2. **无全局强一致元数据** — coordinator 的 `KeyDirectory` 是事件派生的最终一致视图,答案只是 hint,必须在 owner 侧验证;事件丢失或乱序靠 gate 的重放与 fencing 兜底,不做全局事务。多实例并发写同 key 无锁/事务。
3. **共享靠存储或 P2P,非内存池** — 跨实例共享要么走共享远程存储(网络/序列化开销),要么 P2P 直传(需预分配 RDMA 注册区 + 控制器元数据),**非真正的全局共享内存池**。
4. **匹配粒度受限** — 链式 prefix hash + 顺序探测,断链即停;非 prefix 部分需 CacheBlend 重算(有质量损失);无真正的 radix tree 灵活匹配(radix 在引擎侧)。
5. **chunk size 每个 MP server 唯一且不可变** — 由 connector 与各 KV group 对齐后定死;异构模型共享一个 server 要求对齐相容,否则只能多开 server。
6. **C++ 后端构建门槛** — native connector 与 CacheGen kernel 需 CUDA/C++ 工具链,`NO_NATIVE_EXT` 退化为纯 Python 性能损失大。
7. **元数据规模隐患** — `KeyDirectory` 用单进程全内存 dict(`ObjectKey → _KeyRecord.placements`)+ 按 instance 的反向索引维护 fleet 视图,集群级 chunk 数量大时内存与事件扇出是瓶颈。

## 与本系统的关键对比

| 维度 | LMCache | 本系统 |
|------|---------|--------|
| 跨实例复用 | 是,但经(a)共享远程存储 或 (b)P2P 直传 + 中心元数据;非统一内存池 | 全局统一 KV 池 |
| 内容寻址 | 是:`ObjectKey.chunk_hash` 内容哈希;但 `kv_rank` 入 key | 纯内容寻址,同内容同 key |
| 全局元数据 | 弱:`KeyDirectory`(事件派生、最终一致,无强一致/事务) | radix + 位置视图由控制面强一致维护 |
| KV 归属 | 实例(rank)私有 + 可共享:`kv_rank` 入 key,同 chunk 不同 rank 是不同对象 | KV 不绑定 rank |
| 存算分离程度 | 部分:MP 模式 cache 服务与引擎分离,但 KV 仍按引擎的 rank/格式组织 | 格式无关的 KV 抽象 |

**本质**:LMCache 是"KV cache 的缓存层 + 跨实例协调器",仍依附推理引擎的 KV 切分与格式;不是"以 KV 为一等公民的存算分离存储系统"。详见 [3rdparty-reference.md](../3rdparty-reference.md)。卸载路径对照见 [`hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)。
