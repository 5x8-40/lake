# Mooncake — 总览

> 源码:`3rdparty/mooncake`(submodule)。FAST 2025 最佳论文(arXiv:2407.00079),Kimi(Moonshot AI)生产验证。

## 一句话定位

Mooncake 是 **KVCache-centric 的存算分离架构**:将 prefill/decode 集群解耦,利用集群中闲置的 CPU/DRAM/SSD 资源构建分布式 KVCache 池,辅以高性能零拷贝传输引擎。

## 设计哲学

- **KVCache-centric**:以 KVCache 为中心,"trading more storage for less computation"——用更多(廉价)存储换更少(昂贵)计算。
- **存算分离**:prefill 集群与 decode 集群物理分离,KVCache 经传输引擎在二者间搬迁。
- **全局 KV 池**:KVCache 不绑定单个实例的 HBM,池化到集群级 DRAM/SSD,使跨实例复用成为可能。
- **生产验证**:真实负载下使 Kimi 多处理 75% 的请求且满足 SLO。

## 架构(子项目职责)

| 子项目 | 语言 | 职责 |
|--------|------|------|
| `mooncake-transfer-engine/` | C++ | 核心数据传输引擎,零拷贝 RDMA/TCP/NVLink 多传输后端 |
| `mooncake-store/` | C++(+ Go/Rust/Python 绑定) | 分布式 KVCache 存储引擎,对象级 Put/Get/Remove + 元数据 |
| `mooncake-p2p-store/` | Go | P2P 对象共享(checkpoint 分发),无中心 master |
| `mooncake-ep/` | C++/CUDA | Expert Parallel MoE dispatch/combine + 容错 |
| `mooncake-pg/` | C++/CUDA | PyTorch `torch.distributed` ProcessGroup 后端,弹性 rank 恢复 |
| `mooncake-rl/` | Python | RL 场景示例 |
| `mooncake-common/` | C++ | 共享工具(配置加载、环境变量、ASIO) |
| `mooncake-integration/` | C++/Python | Python 扩展模块构建 + 脚本安装 |
| `mooncake-wheel/` | Python | pip 包打包,含 vLLM connector |

> 核心类型(`ObjectKey`/`Slice`/`Segment`/`ErrorCode`/`UUID`/`ReplicateConfig`)定义在 `mooncake-store/include/types.h`。`Status` 在 transfer-engine 的 `common/base/status.h`。

详见 [transfer-engine.md](transfer-engine.md) 与 [kv-store.md](kv-store.md)。

## 分布式模型

> 跨项目汇总对比见 [../distributed-models.md](../distributed-models.md);实现细节见 [kv-store.md](kv-store.md)。

- **拓扑**:星型。中心 `MasterService`(leader)管全部元数据与分配;每节点 `Client`(`RealClient`)贡献 DRAM/SSD segment(`MountSegment`/`UnmountSegment` 动态加减),`DummyClient` 无资源纯转发。控制流与数据流分离:Master 不经手数据,数据 Client↔Client 经 Transfer Engine RDMA 直传。
- **元数据权威**:leader 进程内存,`std::array<MetadataShard,1024>` 分片 + 每 shard SharedMutex,单写者内存权威;etcd 不存主元数据(仅用于选主与 OpLog)。
- **同步机制**:客户端每次操作 RPC 查询权威(`PutStart`/`PutEnd`/`GetReplicaList`/`Remove`),无镜像与订阅推送;leader 切换由客户端 `LeaderMonitorThreadMain` 自动重连。
- **一致性**:单对象强一致——`PutStart`→`PutEnd` 两阶段写,`Get` 只读 `COMPLETE` 副本,对象写后 immutable;per-object lease(默认 TTL 5s)防读写竞争。无跨对象事务,object group 为 best-effort 生命周期提示,副本数亦为 best-effort(空间不足可少于 `replica_num`)。
- **HA 与故障**:etcd/redis/k8s 三后端选主(`LeaderCoordinator`)+ OpLog 复制(`EtcdOpLogStore`)standby 热备 + fork-COW 周期快照;最后一次快照之后的变更在恢复窗口内丢失。client 死亡(`client_live_ttl_sec` 10s)由 `ClearInvalidHandles` 清其副本。
- **扩展性**:元数据全在 leader 内存(1024 shard),规模受单机内存限制,与 lake CP 同属中心内存权威方案的共同瓶颈。
- **附:另一种拓扑**:`mooncake-p2p-store`(Go)无中心 master,元数据存 etcd,`Register`/`GetReplica` 为 BitTorrent 式 seeding,用于 checkpoint 分发,与 store 的星型不同。
- **与 lake 对照**:同为星型 + 内存权威。差异:① Mooncake 客户端每次 RPC 查询 Master,lake 的 Router/agent 持推送镜像(热路径零 RPC),仅在搬 KV 时同步查 CP;② Mooncake 无内容寻址/radix(前缀索引在外部 Conductor),lake 索引内建于 CP;③ Mooncake 池化 DRAM/SSD,lake 的 L0(HBM)亦归池。

## 技术栈与语言分布

| 子项目 | 主语言 | 备注 |
|--------|--------|------|
| mooncake-transfer-engine | C++ | 核心;Rust/Go 绑定可选 |
| mooncake-store | C++ 核心 + Go/Rust/Python 绑定 | Go(`go/mooncakestore/`)和 Rust 均为 C ABI(`store_c.h`)的 FFI |
| mooncake-p2p-store | Go | cgo 调 Transfer Engine C API;依赖 etcd go client |
| mooncake-ep | C++/CUDA | DeepEP 风格 kernel;pybind11 暴露为 `mooncake.ep` |
| mooncake-pg | C++/CUDA | `c10d::ProcessGroup` 后端;pybind11 暴露为 `mooncake.pg` |

**构建**:CMake。关键 option:`WITH_TE`/`WITH_STORE`/`WITH_STORE_GO`/`WITH_STORE_RUST`/`WITH_P2P_STORE`/`WITH_EP`/`USE_ETCD`/`STORE_USE_ETCD`/`STORE_USE_REDIS`/`STORE_USE_K8S_LEASE`/`USE_NOF`。Python 经 pybind11,序列化用 yalantinglibs。pip 安装含 CUDA/CUDA13/non-CUDA/NPU 变体。

**依赖**:RDMA(verbs)、etcd/Redis/HTTP 元数据、ASIO(TCP)、CUDA/CUDAToolkit(EP/PG)、libfabric(EFA/CXI)、cuFile(NVMe-oF)、可选 CacheLib/3FS/SPDK。

## 集成情况

| 系统 | 集成方式 |
|------|---------|
| **vLLM** | KV Connector(PD disaggregation + cross-instance KV),`mooncake_connector_v1.py::MooncakeConnector` |
| **SGLang** | HiCache L3 后端 + PD disaggregation 传输 + EP + P2P weight |
| **LMCache** | Remote connector |
| **TensorRT-LLM** | KVCache 传输 |
| **NIXL** | 后端插件 |
| **vLLM-Ascend / LMDeploy / xLLM / LightX2V** | 各自集成 |

## 代码索引

> 沿代码回溯用。符号名稳定锚定,行号会漂移——找不到时 `grep -n "符号名" <文件>`。

### 传输引擎(`mooncake-transfer-engine/`)

| 概念 | 文件:符号 |
|------|-----------|
| 用户门面 | `include/transfer_engine.h`::`TransferEngine` (L52;`submitTransfer` L116、`registerLocalMemory` L109、`allocateBatchID` L148) |
| 传输抽象 | `include/transport/transport.h`::`Transport` (L44;`TransferRequest`/`Slice`/`TransferTask`/`BatchDesc`) |
| 多传输路由(按 segment 协议) | `include/multi_transport.h`::`MultiTransport` (L25) |
| NUMA 拓扑 + 多 NIC 选择 | `include/topology.h`::`Topology` (L62) + `src/topology.cpp`::`selectDevice` (L706) |
| 元数据(Segment/Buffer/HandShake) | `include/transfer_metadata.h`::`TransferMetadata` (L43) |
| 元数据插件(etcd/redis/http) | `include/transfer_metadata_plugin.h`::`MetadataStoragePlugin` |
| RDMA 传输(零拷贝注册+分片) | `src/transport/rdma_transport/rdma_transport.cpp`::`RdmaTransport` |
| RDMA 端点(ibv_post_send) | `src/transport/rdma_transport/rdma_endpoint.cpp`::`RdmaEndPoint` |
| TCP 退化 | `src/transport/tcp_transport/tcp_transport.cpp`::`TcpTransport` |
| 传输后端集 | `include/transport/`(rdma/tcp/nvlink/efa/nvmeof/cxl/ascend/hip/... 16+) |

### KV 存储(`mooncake-store/`)

| 概念 | 文件:符号 |
|------|-----------|
| 对象键/切片/副本配置 | `include/types.h`(`ObjectKey`/`Slice`/`Segment`/`ErrorCode`/`UUID`) |
| 副本配置 | `include/replica.h`::`ReplicateConfig`/`Replica` |
| 客户端接口 | `include/client_service.h`::`Client` (L63) |
| 资源持有者 | `include/real_client.h`::`RealClient` (L73) |
| 无资源转发 | `include/dummy_client.h`::`DummyClient` (L17) |
| 元数据权威(1024 shard) | `include/master_service.h`::`MasterService` (L88) + `src/master_service.cpp` |
| 分配策略(5 种) | `include/allocation_strategy.h`::`AllocationStrategy` |
| Allocator(低碎片 offset) | `include/allocator.h`::`OffsetBufferAllocator` |
| HA 选主(etcd/redis/k8s) | `include/ha/leadership/leader_coordinator.h`::`LeaderCoordinator` |
| P2P store(无 master,Go) | `mooncake-p2p-store/src/`(BitTorrent 式,经 cgo 调 Transfer Engine C API) |
| Conductor(cache-aware router,外部) | `docs/source/design/conductor/` |

### 构建与集成

| 概念 | 位置 |
|------|------|
| 顶层构建 | `CMakeLists.txt`(option:`WITH_TE`/`WITH_STORE`/`WITH_STORE_GO`/`WITH_STORE_RUST`/`USE_ETCD`...) |
| Python 绑定 | `mooncake-integration/`(pybind11)+ `mooncake-wheel/`(pip 包) |
| Go/Rust 绑定 | `mooncake-store/go/`、`mooncake-store/rust/`(均 FFI 到 C ABI `store_c.h`) |
| vLLM 集成 | `mooncake-wheel/mooncake/mooncake_connector_v1.py`::`MooncakeConnector` |

## 优势

1. **传输引擎极强** — 16+ 传输后端(RDMA/TCP/NVLink/EFA/NVMe-oF/CXL/Ascend/HIP/CXI...),统一 `submitTransfer` API;RDMA 零拷贝经 `ibv_post_send` 硬件 offload;多 NIC 带宽聚合经 NUMA 感知随机分片;自动 TCP 退化。4×200Gbps RoCE 达 87GB/s,8×400Gbps 达 190GB/s。这是 Mooncake 最硬核的工程价值。
2. **控制流/数据流分离** — Master 只管元数据,数据 Client↔Client 直传 RDMA,Master 不成瓶颈。`PutStart`/`PutEnd` 两阶段保证写原子性。
3. **生态集成广泛** — vLLM/SGLang/TensorRT-LLM/LMCache/NIXL/LMDeploy 等,生产验证(Kimi)。
4. **弹性存储** — 动态加减 segment(`MountSegment`/`UnmountSegment`);DRAM→SSD 多层(`FileStorage` offload + promotion);5 种 allocation strategy;CXL 支持。
5. **HA 完善** — etcd/redis/k8s 三种选主后端;OpLog 复制 + fork-COW 快照;standby 状态机;leader 切换客户端自动重连。
6. **多语言客户端** — C++/Python/Go/Rust 统一 C ABI。
7. **EP/PG 容错** — `active_ranks` 感知 + 超时检测 + 弹性 rank 恢复,支持大规模 MoE 部分故障继续服务。
8. **零拷贝生态完整** — prefetch/write-back/weight sync 全链路零拷贝;`put_from`/`get_into`/`get_into_ranges`(多对象多片段聚合)。

## 劣势

1. **池化管理弱于传输** — Store 是对象级 KV(非 KVCache 语义感知)。无内置内容寻址/radix/前缀复用——前缀匹配完全依赖外部(SGLang RadixAttention 或 Conductor)。Store 只存 opaque bytes,key 是应用定义的字符串。
2. **池化 DRAM/SSD,非 HBM 池** — Store 池化的是 Client 贡献的 DRAM/SSD,不是 GPU HBM。HBM 仍属各实例私有。跨实例共享需经 RDMA 读 DRAM 池;真正"HBM 池"靠 PD disaggregation 时 prefill→decode 直传(Transfer Engine),不经 Store。
3. **无多模型配额/GC/碎片整理** — 配额仅 tenant-level,无 per-model;GC 是近似 LRU + lease + zombie 清理,**无主动碎片整理**(依赖 bin-based allocator 的低碎片特性,无 compaction 线程)。
4. **Master 中心化元数据** — 默认单 master(单点);HA 需 etcd 集群。元数据全在 leader 内存(1024 shard),规模受单机内存限制。快照周期性,最后一次快照后的变更恢复有窗口。
5. **C++ 门槛高** — 核心数万行(`master_service.cpp` 单文件 ~8000 行),构建依赖重。Python/Go/Rust 是薄绑定层,深度定制需改 C++。
6. **一致性模型偏弱** — 对象写后 immutable(强一致),但无跨对象事务。Object group 是 best-effort 生命周期提示,非原子。`Get` 保证读完整数据,但"not necessarily the latest"。
7. **best-effort 副本** — 副本数不保证满足,空间不足时尽力分配(至少 1 个)。`replica_num=3` 可能只分到 1 个。
8. **无内置 KVCache 压缩/量化** — 存原始 bytes,压缩由引擎侧负责。

## 与本系统的关键对比

| 维度 | Mooncake | 本系统 |
|------|----------|--------|
| KV 池位置 | DRAM/SSD 池,HBM 仍实例私有 | L0-L3 全归存储池,含 HBM |
| 内容寻址/radix | Store 无;Conductor(外部)有 prefix cache table | Store 内置 radix + 内容寻址 |
| 前缀复用 | 依赖外部(SGLang/vLLM/Conductor) | Store 原生支持 |
| 多模型配额 | 仅 tenant-level | per-model 配额 |
| GC/碎片整理 | 近似 LRU + zombie 清理,无 compaction | 主动碎片整理 |
| PD 分离 KV 搬迁 | Transfer Engine 零拷贝 RDMA 直传 | 直接复用 |
| 传输层 | 极强(16+ 后端,多 NIC 聚合) | 直接复用 |
| HA | etcd/redis/k8s + OpLog + 快照 | 参考 |
| KV 语义感知 | 无(opaque bytes) | layer/head/token 感知 |

**关键结论**:Mooncake 核心竞争力在**传输引擎**,其 Store 是"对象级分布式 KV cache"而非"KVCache 语义感知的池"。前缀复用/内容寻址/radix 由引擎侧(SGLang HiCache)或外部 Conductor 负责。若构建"以 KV 为中心的存算分离",Mooncake 传输引擎可直接复用,Store 层需在其上增加 KVCache 语义(radix、layer/head 寻址、per-model 配额、主动 GC)——这正是 SGLang HiCache + Mooncake Store 的分工模式。详见 [3rdparty-reference.md](../3rdparty-reference.md)。
