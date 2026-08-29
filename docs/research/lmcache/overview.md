# LMCache — 总览

> 源码:`3rdparty/lmcache`(submodule)。

## 一句话定位

LMCache 是一个 **vendor-neutral 的 LLM KV cache 管理层**,把 GPU 上一次性的 KV cache 变成可跨请求、跨实例、跨引擎持久存储/复用/观测/变换的"AI-native knowledge",目标是降 TTFT、提吞吐(尤其长上下文/agent/RAG 场景)。

## 设计哲学

- **问题**:推理引擎(vLLM/SGLang)的 KV cache 是进程内临时状态,prefill 是 TTFT 瓶颈;引擎崩溃即丢失。
- **定位**:KV cache 从"临时状态"升级为"可管理资产"——stored(分层持久化)、reused(跨请求/会话/实例)、monitored(可观测)、transformed(压缩/丢弃/serde)。
- **与引擎的关系**:**集成而非独立**。LMCache 通过 vLLM 的 `KVConnectorBase_V1` 插件接口注入,作为引擎旁的 drop-in KV 层;同时声称 vendor-neutral,也集成 SGLang、NVIDIA Dynamo、NIXL 等。独立 daemon 模式与引擎无 fate-sharing。
- **核心目标**:跨请求/跨实例复用。支持 prefix caching、non-prefix reuse(CacheBlend)、跨实例 P2P CPU 内存共享、PD disagg。

## 架构

### 两种运行模式

| 模式 | 说明 | 状态 |
|------|------|------|
| **Non-MP**(单进程) | `LMCacheEngine` 在推理进程内,`CacheEngine → StorageManager → LocalCPUBackend + RemoteBackend` | 逐步 deprecated |
| **MP**(多进程,生产推荐) | 独立 daemon(`MPCacheServer`),引擎经 IPC/HTTP/ZMQ 通信;`StorageManager` 管理 L1(CPU/disk)与多个 L2 adapter | 推荐 |

### 核心数据流

**存(store)**:引擎 attention 层逐层 `save_kv_layer` → `GPUConnector` 把 GPU paged KV 转 `MemoryObj` → `LMCacheEngine.store_tokens` → `TokenDatabase.process_tokens` 切 chunk + 算 key → `StorageManager` 异步提交。

**取(retrieve)**:`get_num_new_matched_tokens`(scheduler 侧)→ lookup 命中 chunk 数 → `start_load_kv` 异步从后端拉到 GPU paged buffer → `wait_for_layer_load` 逐层同步。

vLLM 集成点:`LMCacheConnectorV1Dynamic`(继承 `KVConnectorBase_V1`),委托 `LMCacheConnectorV1Impl`。逐层(layer-wise)异步 copy 实现流水线。

### 缓存键(内容寻址,但 rank 绑定)

- `CacheEngineKey` = `(model_name, world_size, worker_id, chunk_hash, dtype, request_configs/tags)`。
- 切分:`ChunkedTokenDatabase` 按固定 `chunk_size`(默认 256)切 token,**链式 prefix hash**(每 chunk hash = `hash(prev_hash, chunk_tokens)`)。
- **无 radix tree 做匹配**(radix 在 vLLM 侧);LMCache 靠逐 chunk 顺序 `contains` 探测,断链即停。
- MP 模式 `ObjectKey` = `(chunk_hash[内容哈希], model_name, kv_rank, object_group_id)` — L2 层内容寻址,同 model+rank+chunk 可被任意实例复用。

### 跨实例共享(两条路径)

1. **共享远程存储(L2 adapter)**:多实例共用 Redis/Mooncake/S3/Aerospike/NIXL-store,通过 `chunk_hash` 内容寻址天然去重共享。
2. **P2P 直传(CPU 内存共享)**:`P2PBackend` 经 `NixlChannel`(NIXL/RDMA/TCP)或 `PySocketChannel`(ZMQ 控制面 + 原生 socket 数据面)在实例间直传 KV bytes。需 `cache_controller` 维护"谁有哪些 chunk"元数据。

详见 [sharing-and-backends.md](sharing-and-backends.md)。

## 分布式模型

> 跨项目汇总对比见 [../distributed-models.md](../distributed-models.md);细节见 [sharing-and-backends.md](sharing-and-backends.md)。

- **拓扑**:两种并存。(a)共享存储为中心:多实例共用 Redis/Mooncake/S3 等 L2 adapter,`chunk_hash` 内容寻址天然去重,实例间无协调;(b)controller 星型:`LMCacheControllerManager`(中心)+ 每实例 `LMCacheWorker`(代理),ZMQ 通信,维护"谁有哪些 chunk"的元数据(`RegistryTree`:(instance,worker)→location→set[chunk_hash]),仅协调元数据,不在数据路径上。
- **元数据权威**:中心 controller 全内存 `RegistryTree`,定位为 best-effort 协调器而非权威:心跳 + 序列号追踪(`WorkerNode.seq_tracker`),full sync 完成阈值 0.8、超时 300s。
- **同步机制**:worker 心跳上报 + 命令通道(lookup/pin/move/clear/compress/health/full_sync);P2P 直传路径(`P2PBackend` 经 NIXL/socket)需 controller 元数据定位对等端。
- **一致性**:无全局强一致。多实例并发写同 key 无锁/事务;数据层靠内容寻址去重;失效靠 TTL + 显式 clear/evict。`KVController` lookup 在实例增多时退化 O(n²)(源码注释自承认)。
- **HA 与故障**:MP daemon 模式下引擎崩溃 KV 不丢(无 fate-sharing);controller 本身无 HA,元数据丢失后靠 full sync 重建。
- **扩展性**:共享存储路径随后端扩展;controller 路径受全内存 `RegistryTree` 与 O(n²) lookup 限制。
- **与 lake 对照**:LMCache 的 controller+worker 形态最接近 lake 的 CP+agent,但一致性立场不同——LMCache 为 best-effort(允许错误,周期 full sync 收敛),lake CP 为单写者权威(线性一致查询 + 镜像推送 + miss 回查)。此外 LMCache 的 KV 按 rank 入 key(部分实例私有),lake KV 不绑定 rank。

## 技术栈

- **语言**:Python(主体)+ C++/CUDA(`csrc/`,高性能后端 + CacheGen/内存 kernel,带 SYCL 后端支持 AMD/Intel)+ **Rust**(`rust/raw_block/`)。
- **Rust 作用**:`rust/raw_block/` 是**低层裸设备 I/O 层**(PyO3 绑定),提供 `RawBlockDevice` 的 `pwrite_from_buffer`/`pread_into`,支持 `posix`(同步 pread/pwrite)、`io_uring`(Rust 原生 syscall)、`io_uring_cmd`(NVMe passthrough)。被 non-MP 的 `RustRawBlockBackend` 和 MP 的 `RawBlockL2Adapter` 共用。**是存储后端(裸块设备 SSD),不是传输层,生产可用**。
- **关键依赖**:vLLM(`KVConnectorBase_V1`)、NIXL(RDMA/NVLink 传输)、Mooncake(分布式 KV store)、Redis/Valkey、msgspec、ZMQ、PyTorch。
- **构建**:`pip install -e . --no-build-isolation`(需预装 torch);`NO_NATIVE_EXT=1`(纯源码)/`NO_GPU_EXT=1`(CPU-only C++)/`BUILD_WITH_HIP=1`(ROCm)。CMake + pybind。

## 代码索引

> 沿代码回溯用。符号名稳定锚定,行号会漂移——找不到时 `grep -n "符号名" <文件>`。

| 概念 | 文件:符号 |
|------|-----------|
| 主引擎 | `lmcache/v1/cache_engine.py`::`LMCacheEngine` (L83) |
| 存储管理器(串联 L1/L2) | `lmcache/v1/storage_backend/storage_manager.py`::`StorageManager` (L219) |
| 后端抽象(Python) | `lmcache/v1/storage_backend/abstract_backend.py`::`StorageBackendInterface` (L27) |
| 后端工厂 | `lmcache/v1/storage_backend/__init__.py`::`CreateStorageBackends` |
| 缓存键(non-MP) | `lmcache/utils.py`::`CacheEngineKey` (L399) |
| token 切分 + 链式哈希 | `lmcache/v1/token_database.py`::`ChunkedTokenDatabase` (L298) |
| MP 分布式键(内容寻址) | `lmcache/v1/distributed/api.py`::`ObjectKey` |
| MP 存储管理器(多 L2 adapter) | `lmcache/v1/distributed/storage_manager.py`::`StorageManager` |
| L2 adapter 抽象 | `lmcache/v1/distributed/l2_adapters/base.py`::`L2AdapterInterface` |
| P2P 跨实例 | `lmcache/v1/storage_backend/p2p_backend.py`::`P2PBackend` (L160) |
| 传输通道(NIXL/socket) | `lmcache/v1/transfer_channel/`(`nixl_channel.py`、`py_socket_channel.py`) |
| 控制器(元数据协调,非数据路径) | `lmcache/v1/cache_controller/worker.py`::`LMCacheWorker` + `controller_manager.py`::`LMCacheControllerManager` + `controllers/kv_controller.py` |
| 元数据树 | `lmcache/v1/cache_controller/utils.py`::`RegistryTree` |
| vLLM 集成(拦截 KV) | `lmcache/integration/vllm/lmcache_connector_v1.py`::`LMCacheConnectorV1Dynamic` (L30) |
| C++ 后端抽象 | `csrc/storage_backends/connector_interface.h`::`IStorageConnector` |
| C++ 后端基类(SQ/CQ+tiling) | `csrc/storage_backends/connector_base.h`::`ConnectorBase<T>` |
| C++ 已实现后端 | `csrc/storage_backends/{redis,mooncake,fs,aerospike}/` |
| Rust 裸设备 I/O | `rust/raw_block/src/lib.rs`::`RawBlockDevice`(posix/io_uring/io_uring_cmd) |
| serde/压缩 | `lmcache/v1/storage_backend/naive_serde/`(CacheGen/KiVi) + `lmcache/v1/distributed/serde/`(fp8/turboquant) |
| CacheBlend(non-prefix 复用) | `lmcache/v1/compute/blend/blender.py`::`LMCBlender` |

## 优势

1. **引擎解耦、无 fate-sharing** — 独立 daemon(MP 模式),引擎崩溃 KV 不丢。
2. **分层 + 可插拔后端丰富** — `StorageBackendInterface`/`L2AdapterInterface` 双层抽象,后端覆盖 CPU/disk/Redis/Mooncake/S3/Aerospike/NIXL/GDS/raw_block,C++ 原生后端一次实现两模式复用。
3. **内容寻址跨实例去重** — `ObjectKey.chunk_hash` 内容哈希,同 model+rank+chunk 自然全局共享,无需中心化协调数据归属。
4. **non-prefix 复用(CacheBlend)** — 选择性重算恢复质量,超出普通 prefix cache 能力。
5. **PD disagg 原生支持** — NIXL/NVLink/RDMA/TCP 传输层 + `pd_backend`。
6. **C++ 后端性能工程** — GIL-free、eventfd 非轮询、批量 tiling。
7. **生态广** — vLLM/SGLang/Dynamo/NIXL/Mooncake/Redis,已入 PyTorch Foundation。

## 劣势

1. **强依赖 vLLM 的 hash/connector 抽象** — `token_database.py` 大量 vLLM 版本兼容代码,hash 取自 vLLM;深度耦合 vLLM 演进。
2. **无全局强一致元数据** — controller 是 best-effort(心跳超时 + 0.8 阈值 full sync);`KVController` lookup 在实例多时退化到 O(n²)(源码注释自承认)。多实例并发写同 key 无锁/事务。
3. **共享靠存储或 P2P,非内存池** — 跨实例共享要么走共享远程存储(网络/序列化开销),要么 P2P 直传(需预分配 NIXL buffer + 控制器元数据),**非真正的全局共享内存池**。
4. **匹配粒度受限** — 链式 prefix hash + 顺序 contains,断链即停;非 prefix 部分需 CacheBlend 重算(有质量损失);无真正的 radix tree 灵活匹配(radix 在 vLLM 侧)。
5. **架构双轨复杂** — non-MP(逐步 deprecated)与 MP 两套代码路径并存,认知与维护成本高。
6. **C++ 后端构建门槛** — Redis/Mooncake 原生后端 + CacheGen kernel 需 CUDA/C++ 工具链,`NO_NATIVE_EXT` 退化为纯 Python 性能损失大。
7. **元数据规模隐患** — `RegistryTree` 用 `dict[str, set[int]]` 全内存维护 chunk→location,集群级 chunk 数量大时元数据内存与 lookup 延迟是瓶颈。

## 与本系统的关键对比

| 维度 | LMCache | 本系统 |
|------|---------|--------|
| 跨实例复用 | 是,但经(a)共享远程存储 或 (b)P2P 直传 + 中心元数据;非统一内存池 | 全局统一 KV 池 |
| 内容寻址 | 仅 L2/MP 的 `ObjectKey.chunk_hash`;non-MP 的 `CacheEngineKey` 含 `world_size/worker_id` → **KV 部分实例私有(按 rank 切分)** | 纯内容寻址,同内容同 key |
| 全局元数据 | 弱:controller `RegistryTree`(best-effort,无强一致/事务) | radix + 位置视图由控制面强一致维护 |
| KV 归属 | 实例(rank)私有 + 可共享:`kv_rank` 入 key,同 chunk 不同 rank 是不同对象 | KV 不绑定 rank |
| 存算分离程度 | 部分:MP 模式 cache daemon 与引擎分离,但 KV 仍按引擎的 rank/格式组织 | 格式无关的 KV 抽象 |

**本质**:LMCache 是"KV cache 的缓存层 + 跨实例协调器",仍依附推理引擎的 KV 切分与格式;不是"以 KV 为一等公民的存算分离存储系统"。详见 [3rdparty-reference.md](../3rdparty-reference.md)。卸载路径对照见 [`hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)。
