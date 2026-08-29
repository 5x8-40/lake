# Dynamo — 数据中心级分布式推理编排框架

> 源码:`3rdparty/dynamo/`(NVIDIA ai-dynamo/dynamo,Apache-2.0)。Rust(~52%)+ Python(~34%)+ Go(~12%,K8s 相关)。本文为**核实后的分析**(读 README + `lib/` 源码结构与关键符号)。

## 定位

Dynamo 是"**推理引擎之上的编排层**"——不替代 vLLM/SGLang/TensorRT-LLM,而是把它们作为可插拔 worker,协调成多节点推理系统。这与 lake 的"控制面 + 存储池编排计算 worker"定位**高度同构**,且 Dynamo 用 Rust 写核心性能路径——是 lake 三语言分层(Rust 存储 / Go 控制 / Python 计算)的直接参照系,尤其 Rust 控制面/编排这一块。

## 整体架构

```
client → Frontend(OpenAI 兼容 HTTP) → Router(KV-aware) → workers(vLLM/SGLang/TRT-LLM)
                                          │
                           KV 事件(NATS / 预测式 fallback)
                                          │
                          KVBM(KV Block Manager)三层 offload
```

两种路由拓扑:
- **Dynamo-native Frontend 路由**:`client → Frontend → Router → workers`,Frontend 内置 router,无外部 gateway。
- **Gateway API + GAIE**:K8s Gateway API Inference Extension → Endpoint Picker Plugin(EPP)→ Frontend sidecar(direct router 模式)。

## 主要组件

| 组件 | 角色 | lake 对应 |
|------|------|----------|
| **Frontend** | OpenAI 兼容 HTTP 入口 | Gateway |
| **Router**(KV-aware) | 按 worker 负载 + KV cache overlap 选 worker,省重算 prefill | Router |
| **KVBM**(KV Block Manager) | GPU→CPU→SSD→远端 多层 KV offload | Tiered Store(L0-L3) |
| **Planner** | SLA 驱动 autoscaler,profiling 后 right-size GPU 池 | (弹性,远期) |
| **ModelExpress** | NIXL/NVLink GPU 间流式传权重,7x 冷启动 | 权重加载(远期) |
| **Grove** | K8s operator,拓扑感知 gang 调度(机架/主机/NUMA) | (部署,远期) |
| **Fault Tolerance** | canary 健康检查 + 在途请求迁移 | F4(部分对应) |

## KV 路由(kv-router)

源码入口:`lib/kv-router/src/`。核心数据结构在 `protocols.rs`:

- **`LocalBlockHash(pub u64)` / `ExternalSequenceBlockHash(pub u64)`**:block 哈希。`ExternalSequenceBlockHash` 注释明示"engine 从 token IDs + 可选 metadata + **parent block hash** 计算"——即**前缀链式哈希**,与 lake `block_hash = hash(parent || 本块 tokens)` 同构。这是 lake 链式哈希防误复用的直接工业印证。
- **`Placement { owner: PlacementOwner, tier: StorageTier }`**:block 的位置 = owner + tier。
  - `PlacementOwner`:`LocalWorker(WorkerWithDpRank)` | `Shared`——区分"某 worker 私有"vs"共享池"。
  - `StorageTier`:`Device`(GPU)| `HostPinned`(CPU)| `Disk`(NVMe)| `External`(远端/网络)。`from_kv_medium` 把字符串 medium 映射到 tier。**这正是 lake L0/L1/L2/L3 四层的对应**——且 tier=介质非位置,与 lake"层=介质非位置"原则一致。
- **`PlacementEvent { placement, event: KvCacheEvent }`**:位置变更事件,推送用。对应 lake"位置视图权威变更触发推送(放置/驱逐/迁移/满块注册)"。
- **`RouterRequest`/`RouterResponse`**:Router 的 RPC 协议,`RouterResponse` 含 `overlap_blocks`(命中重叠块数)、`effective_overlap_blocks`——KV-aware 路由的命中量化。
- **`KvTransferEnforcement`**:强制 KV 传输的策略枚举(对应 lake PD 分离/D-direct 的模式选择边界)。

KV-aware 路由:Router 维护各 worker 的 KV block 哈希集合,新请求按 prompt 前缀哈希找 overlap 最多的 worker,省重算。无 KV 事件时降级为"预测式路由"(按负载)。

## KVBM(KV Block Manager)三层架构

源码入口:`lib/kvbm-{logical,physical,engine}/`。分层清晰,是 lake 存储池分层最直接的参考:

| 层 | 职责 | lake 对应 |
|----|------|----------|
| **kvbm-logical** | 逻辑 block 管理(blocks/pools/manager/events)、pubsub 事件 | radix + locations 元数据 + 位置视图 |
| **kvbm-physical** | 物理布局(layout)、传输(transfer)、manager | 分块流水线 + 传输引擎 |
| **kvbm-engine** | 运行时(runtime)、offload、leader、collectives、object(S3/Azure) | 池运行时 + L3 SSOT + 副本/leader |

KVBM offload 路径:`GPU → CPU → SSD → 远端存储(S3/Azure blob)`,1.0 新增"global KV events for cluster-wide cache visibility"(集群级缓存可见性)——对应 lake 控制面强一致位置视图。

## 运行时与通信(transports / discovery)

源码:`lib/runtime/src/transports.rs` + `discovery/`。Dynamo 的通信后端是**多后端可插拔**,对 lake 通信选型(见 [`../architecture/` #3] 讨论)极有参考价值:

- **transports**:`etcd` / `nats` / `tcp` / `zmq` / `event_plane`——多套通信栈并存,按部署形态选。
- **discovery**:`kube`(K8s CRD + EndpointSlices,无外部服务)/ `etcd`(Slurm 等)/ `kv_store` / `mock`(本地 file-based,无 etcd/NATS)。
- **组件间请求面**:TCP 为主。
- **KV 事件面**:NATS JetStream(可选),无 NATS 时降级预测式路由。

关键洞察:Dynamo **不把 etcd 当唯一控制面存储**——K8s 部署用 CRD/EndpointSlices 做 discovery,etcd 只在非 K8s 部署用;KV 事件走 NATS 而非 etcd。这与 lake"etcd 专属存储层位置视图"不同——lake 把位置视图权威放 etcd 强一致,Dynamo 把事件流放 NATS(更适合高频事件流,etcd 不善长高频写)。

## PD 分离 / E/P/D

PD 分离为"独立可伸缩的 GPU 池",三后端(vLLM/SGLang/TRT-LLM)都支持。多模态扩展为 **E/P/D**(encode/prefill/decode)三路 + embedding cache。对应 lake PD 分离 + 混部 + D-direct 三模式(但 lake 多一档 D-direct 本地命中,Dynamo 侧重 PD 物理隔离)。

## 借鉴点(对应 lake 设计)

| Dynamo 设计 | lake 对应 | 说明 |
|------|----------|------|
| `ExternalSequenceBlockHash`(父哈希链式) | `block_hash = hash(parent ‖ tokens)` | lake 链式哈希防误复用的工业印证,见 [`../architecture/kv-cache-pool.md`](../../architecture/kv-cache-pool.md) "Block 寻址" |
| `StorageTier`(Device/HostPinned/Disk/External) | L0/L1/L2/L3 | tier=介质非位置,与 lake"层=介质非位置"一致;`from_kv_medium` 字符串映射可参考 wire 格式 |
| `Placement { owner, tier }` | `locations` 多层位置 | owner 区分私有/共享,对应 lake"副本归池非 worker 私有" |
| `PlacementEvent` 位置变更推送 | 位置视图权威变更触发推送 | 事件驱动推送镜像,见 [`../../architecture/scheduling.md`](../../architecture/scheduling.md) §1 |
| KVBM logical/physical/engine 三层 | 存储池 元数据/传输/运行时 | 分层组织可直接借鉴,见 [`../../architecture/storage-layer.md`](../../architecture/storage-layer.md) |
| KV-aware router(overlap 量化) | Router 命中感知选路 | `overlap_blocks` 命中量化,见 [`../../architecture/scheduling.md`](../../architecture/scheduling.md) "缓存命中感知调度" |
| transports 多后端可插拔 | 通信选型(见 #3) | etcd/nats/tcp/zmq 按部署形态选,印证"控制面存储 vs 事件面"可分离 |
| KV events 走 NATS 而非 etcd | (lake 待定) | 高频事件流用 NATS、权威元数据用 etcd 的分工,值得 lake 评估 |

## 关键差异(lake 更彻底)

G1 是传输句柄（无 `BlockManager<G1>`）、与 KV event / 本机 G2/G3 的分工，以及和 Mooncake / MemCache / FlexKV 等的 HBM 对照，见 [`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)。Dynamo 也可把 FlexKV 当 worker connector（`--connector flexkv`），与 KVBM 不是同一套本机索引。

- **存算分离彻底度**:Dynamo 的 KV 仍由 engine(vLLM/SGLang)持有,KVBM 是"offload 层"(把 engine 的 KV 卸到 CPU/SSD/远端);G1 无 `BlockManager`。lake **HBM 归池、worker 不拥有任何内存**,KVBM 式 offload 在 lake 是池的统一放置(方案 Z),非引擎私有缓存的延伸。
- **控制面一致性**:Dynamo KV 事件走 NATS(best-effort 事件流)、discovery 多后端,无全局强一致位置视图;lake 位置视图进 etcd 强一致,Router 一跳命中(比 LMCache/Mooncake 都强,见 [`../architecture/consistency.md`](../../architecture/consistency.md) §1 参考对照)。Dynamo 更偏"事件流编排",lake 更偏"强一致权威 + 镜像"。
- **radix 前缀复用**:Dynamo KV-aware router 用 block 哈希 overlap,但 radix 前缀树/内容寻址复用仍依赖底层 engine(SGLang RadixAttention);lake 把 radix 归存储池统一管。
- **执行模式**:Dynamo 侧重 PD/E-P-D 物理隔离 + KV-aware 路由;lake 多 D-direct(本地命中零传输直跳)与混部,Dynamo 无明确对应。
- **多模型/池生命周期**:Dynamo 以单集群服务为主;lake 存储池是长期存续、模型无关的独立基础设施(F11),配额/GC/碎片整理/多模型命名空间。

## 代码索引

> 沿代码回溯用。符号名锚定,行号会漂移——找不到时 `grep -n "符号名" 3rdparty/dynamo/<文件路径>`。

| 机制 | 文件:符号 |
|------|-----------|
| Router 协议(Placement/StorageTier/RouterRequest 等) | `lib/kv-router/src/protocols.rs`::`StorageTier` / `PlacementOwner` / `Placement` / `PlacementEvent` / `RouterRequest` / `RouterResponse` / `KvTransferEnforcement` |
| 链式 block 哈希 | `lib/kv-router/src/protocols.rs`::`LocalBlockHash` / `ExternalSequenceBlockHash`(注释含 parent block hash) |
| KV-aware 路由命中量化 | `lib/kv-router/src/protocols.rs`::`WorkerSelectionResult`(`overlap_blocks`/`effective_overlap_blocks`) |
| KV 路由主循环 | `lib/kv-router/src/{active_set,lookup_update,scheduling}/` |
| KVBM 逻辑层 | `lib/kvbm-logical/src/`(`blocks`/`pools`/`manager`/`events`/`pubsub`) |
| KVBM 物理层(布局/传输) | `lib/kvbm-physical/src/`(`layout`/`transfer`/`manager`) |
| KVBM 引擎层(运行时/offload/leader/object) | `lib/kvbm-engine/src/`(`runtime`/`offload`/`leader`/`collectives`/`object`) |
| 通信后端(多后端) | `lib/runtime/src/transports.rs`::`etcd`/`nats`/`tcp`/`zmq`/`event_plane` |
| 服务发现 | `lib/runtime/src/discovery/`(`kube`/`kv_store`/`mock`) |
| 组件抽象 | `lib/runtime/src/component.rs` / `pipeline/` |
| worker/LLM 抽象 | `lib/llm/src/`(`backend.rs`/`block_manager.rs`/`block_manager.md`) |
