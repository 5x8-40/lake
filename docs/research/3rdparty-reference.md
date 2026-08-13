# 3rdparty 源码参考

本仓库在 `3rdparty/` 以 git submodule 引入项目源码,作为设计与实现的直接参考。本文是**汇总对比**;各项目的深度分析见分目录:

- [`sglang/`](sglang/) — SGLang HiCache:[总览](sglang/overview.md) · [分层机制](sglang/hicache.md) · [存储后端](sglang/storage-backends.md) · [block 生命周期](sglang/block-lifecycle.md) · [MoE 通信](sglang/moe-communication.md) · [权重加载与外部集成](sglang/loading-and-integrations.md) · [thinking 控制](sglang/thinking-control.md) · [上游痛点](sglang/pain-points.md) · [Agentic KV 路线图 #21846](sglang/agentic-kv-roadmap.md) · [Elastic Memory Pool](sglang/elastic-memory-pool.md)
- [`lmcache/`](lmcache/) — LMCache:[总览](lmcache/overview.md) · [跨实例复用与后端](lmcache/sharing-and-backends.md)
- [`mooncake/`](mooncake/) — Mooncake:[总览](mooncake/overview.md) · [传输引擎](mooncake/transfer-engine.md) · [KV 存储与池化](mooncake/kv-store.md)
- [`vllm/`](vllm/) — vLLM:[总览](vllm/overview.md) · [计算层抽象与存算分离接入点](vllm/compute.md) · [block 生命周期](vllm/block-lifecycle.md) · [上游痛点与 lake 对照](vllm/pain-points.md) · [Q3 KV/Session 调度 #48168/#48501](vllm/kv-session-roadmap.md)
- [`transformers/`](transformers/) — Hugging Face Transformers:[总览](transformers/overview.md) · Qwen3 `nn.Module` 模型定义、HF config 字段、decoder/model/causal-lm 分层源码参考
- [`dynamo/`](dynamo/) — Dynamo(NVIDIA):[总览](dynamo/overview.md) · 数据中心级推理编排(KV-aware router + KVBM 三层 + Rust 控制面)
- [`tilert/`](tilert/) — TileRT:[总览](tilert/overview.md) · [vLLM PD 插件](tilert/pd-vllm.md) · [痛点与 lake 对照](tilert/pain-points.md)
- [`memcache/`](memcache/) — Ascend MemCache:[总览](memcache/overview.md) · [架构](memcache/architecture.md) · [痛点与 lake 对照](memcache/pain-points.md)
- [`ucm/`](ucm/) — UCM(ModelEngine):[总览](ucm/overview.md) · [架构](ucm/architecture.md) · [痛点与 lake 对照](ucm/pain-points.md)
- [guided-decoding.md](guided-decoding.md) — **Guided / structured decoding**(SGLang × vLLM):xgrammar/llguidance 库边界、overlap/async 下能否消同步、spec+grammar 硬缺口
- [sampling-params.md](sampling-params.md) — **Sampling 参数对照**(SGLang × vLLM):核心/独有字段、`n`≠beam、spec 禁 min_p/logit_bias；penalty 空泡与 V2；采样状态归属 / Spec 兼容矩阵 / `n` 与前缀 KV 共享
- [scheduler-worker-interface.md](scheduler-worker-interface.md) — **Scheduler→Worker 字段全集**(SGLang × vLLM):`SchedulerOutput` vs `ScheduleBatch`/`ForwardBatch`、差异表、架构根因、对 lake D1 含义
- [nvidia-cmx.md](nvidia-cmx.md) — **NVIDIA CMX 目标栈与成熟度审计**：Dynamo 当前代码、open NIXL `DOCA_MEMOS`、双端 BF4/STX 公布架构、VAST 当前 G3 与未来 G3.5 的边界；含模型布局字节和 offered-load 计算器 `tools/cmx-sim/`

本文把它们的关键组件与本系统(`docs/architecture/`)逐层对应,并标注**借鉴点**与**关键差异**(我们的设计更彻底)。

## submodule 清单

| 路径 | 来源 | 检出 | 主要参考 |
|------|------|------|----------|
| `3rdparty/sglang` | [sgl-project/sglang](https://github.com/sgl-project/sglang) | main HEAD (`37f94cb7a0`, 2026-07-17) | 分层 + HiRadixTree + prefetch/write-back;痛点见 [sglang/pain-points.md](sglang/pain-points.md) |
| `3rdparty/lmcache` | [LMCache/LMCache](https://github.com/LMCache/LMCache) | nightly | 跨实例复用 + 多后端 + Rust I/O |
| `3rdparty/mooncake` | [kvcache-ai/Mooncake](https://github.com/kvcache-ai/Mooncake) | main HEAD | 传输引擎 + 对象级 KV 池 |
| `3rdparty/vllm` | [vllm-project/vllm](https://github.com/vllm-project/vllm) | main HEAD (ab132ee98) | **计算层**(PagedAttention/worker/connector/spec decode) |
| `3rdparty/transformers` | [huggingface/transformers](https://github.com/huggingface/transformers) | main HEAD | **模型定义**:Qwen3 `nn.Module`/HF config/decoder 分层,作为 vLLM/SGLang 模型实现的上游对照 |
| `3rdparty/dynamo` | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo) | main HEAD | **编排层/控制面**:KV-aware router + KVBM 三层 offload + Rust 编排 + 多后端通信 |
| `3rdparty/tilert` | [tile-ai/TileRT](https://github.com/tile-ai/TileRT) | main HEAD (`a8368a6`, v0.1.5) | **超低延迟 decode** + **vLLM PD 插件**(`TileRTConnector`);见 [tilert/](tilert/) |
| `3rdparty/memcache` | [Ascend/memcache](https://github.com/Ascend/memcache) | master HEAD (`14b4e35`) | **昇腾 KV 对象池** Meta/Local + MemFabric;见 [memcache/](memcache/) |
| `3rdparty/ucm` | [ModelEngine-Group/unified-cache-management](https://github.com/modelengine-group/unified-cache-management) | main HEAD (`37af15e`) | **统一缓存框架** store+connector+PD-via-pool;见 [ucm/](ucm/) |

> 生态相连:vLLM `KVConnectorBase_V1` 被 LMCache/Mooncake/NIXL/FlexKV/**TileRT**/**UCM** 等实现;vLLM-Ascend 另将 **MemCache** 列为 KV Pool backend;SGLang HiCache 把 Mooncake 作 L3;Dynamo 编排 vLLM/SGLang;UCM 主推经统一池做 PD;TileRT 做低延迟 decode。我们站在其上做更彻底的存算分离。

---

## 1. SGLang HiCache → 我们的 L0-L3 分层 + 放置 + 冷热

源码入口:`3rdparty/sglang/docs/advanced_features/hicache_design.md`、`python/sglang/srt/mem_cache/`。

> SGLang **同时是计算层参考**:spec decode(drafter 共置串行、DSPARK/DFLASH/MTP/EAGLE、`PoolName.DRAFT`)、DP/TP/PP 控制面(每 GPU 一 Scheduler + 请求广播 / PP proxy;对照 vLLM 一 Scheduler + Executor 扇出)。research 专文见 [`sglang/model-runner.md`](sglang/model-runner.md);lake 落点见 [`../architecture/compute-layer.md`](../architecture/compute-layer.md) "投机解码"。  
> **Agentic 分布式 KV 总路线**([#21846](https://github.com/sgl-project/sglang/issues/21846)：增量 PD、Host 直传、UnifiedRadix、router hint)见 [`sglang/agentic-kv-roadmap.md`](sglang/agentic-kv-roadmap.md)。

### 借鉴点

| HiCache 设计 | 我们对应 | 说明 |
|--------------|----------|------|
| **HiRadixTree**:radix 节点记录 KV 存在哪层(GPU/CPU/L3/多层) | radix tree 归存储池 + block 的 `locations` 多层位置集合 | 见 [`../architecture/kv-cache-pool.md`](../architecture/kv-cache-pool.md)、[`../architecture/storage-layer.md`](../architecture/storage-layer.md)。HiCache 的"节点记位置"正是我们 `locations` 元数据的原型 |
| **prefetch 三策略**:best_effort / wait_complete / timeout | 迁移触发的"被动兜底"读 miss 回填 + 主动预放置 | 见 storage-layer "迁移触发"。timeout 的 `base + per_ki_token` 公式可直接借鉴为我们的 prefetch 预算模型 |
| **write-back 三策略**:write_through / write_through_selective / write_back | decode 增量写回频率 N 的策略 | 见 execution-modes "decode 写回频率"。selective(按访问频次只回写热数据)对应我们"前缀生长"的写回取舍 |
| **page-first / page_first_direct 布局** | block 粒度 + 分块流水线 | 见 kv-cache-pool "分块流水线"。page_first_direct 让同层同 page 连续,可零拷贝传 L3——我们 Rust transfer 层可照搬 |
| **计算-传输重叠**:算 layer N 时传 layer N+1 | 分块流水线(page_first_direct 子块)与 prefill 层数对齐 | **部分对应**:SGLang 是引擎驱动(每层 `wait_event`,破坏 graph);我们只取**生产侧层级重叠**(池 agent 逐层 publish,引擎无感),拒绝引擎驱动的消费侧 intra-step 重叠。见 kv-cache-pool "无引擎驱动的 intra-step 重叠" + "分块流水线" |
| **MLA write-back 去重**:多 TP rank 只一个 rank 回写 | (未来 TP 支持) | 留作 compute-layer 细节参考 |
| **统一 `HiCacheStorage(ABC)` 接口** + 多后端(file/mooncake/hf3fs/nixl/aibrix) | 存储池后端抽象 | 我们存储池统一管理 L0-L3,后端可抽象;Mooncake/NIXL 等可作为 L1 DRAM 池(远端载体)的物理实现 |

### 关键差异(我们更彻底)

- **HiCache 的 L1/L2 私有于推理实例,L3 才共享**;我们 **L0-L3 全归存储池统一管理,L1/L2 也是池的物理载体而非 worker 私有**。计算节点不拥有任何内存,"本地命中"是存储池放置决策的结果,不是实例私有缓存。这是我们与 HiCache 的根本分野——HiCache 仍是"实例私有分层 + 共享 L3",我们是"全层共享、放置归一"。
- HiCache 不持续同步 L3 元数据,访问时实时查后端;我们 radix + 位置视图权威在控制面进程内存(单写者线性一致)、etcd 降频 checkpoint,Router 一跳拿前缀复用 + 本地命中(守 5ms 预算)。
- HiCache 无"反向回传增强未来前缀"的显式机制(它的 write-back 是为跨实例共享,非为多轮前缀生长);我们把它作为 agent 多轮的核心(见 execution-modes 时序二反向)。

---

## 2. Mooncake → 我们的 KV Pool 数据面 + Transfer Bus

源码入口:`3rdparty/mooncake/mooncake-transfer-engine/`、`mooncake-store/`、`mooncake-p2p-store/`、`docs/`。

### 借鉴点

| Mooncake 组件 | 我们对应 | 说明 |
|---------------|----------|------|
| **mooncake-transfer-engine**:RDMA + 多 NIC 零拷贝传输 | Transfer Bus(RDMA 数据面,TCP 退化) | 见 overview "数据面:KV 跨节点传输"。直接参考其传输 API 与零拷贝设计 |
| **mooncake-store**:KVCache 全局池、按 segment 寻址 | KV Pool(L1 DRAM 池远端载体) | 见 kv-cache-pool "物理布局"。Mooncake 的 KVCache store 是我们 L1 DRAM 池(远端)的工业级原型 |
| **mooncake-p2p-store**:P2P 存储拓扑 | KV Node 分片 + 一致性哈希 | 见 kv-cache-pool "空间分配与扩缩容"。参考其节点组织与扩缩 |
| **KVCache-centric disaggregation**(prefill/decode 分离 + KV 池) | 整体架构立地 | Mooncake 是我们"以 KV 为中心"的直接灵感来源(见 overview)。但 Mooncake 仍以实例为中心做 P/D 分离,我们进一步把 HBM 也剥离 |
| **PD disaggregation via TransferEngine** | 时序二正向(P→D 跨节点传输) | 见 execution-modes。Mooncake 的 P/D KV 搬运即我们时序二正向 |

### 关键差异

- Mooncake 的 KVCache 池服务于"实例间共享/迁移",实例仍拥有本地 HBM;我们连 HBM 放置都归存储池(方案 Z)。
- Mooncake 无 radix 前缀树的内容寻址复用(按 segment ID 存取);我们用内容寻址 `(model_id, layer, block_hash)` + radix 实现前缀复用,SGLang RadixAttention 的思路补上这一块。
- Mooncake 无"统一管理 L0-L3 + 冷热生命周期 + 多模型配额/GC/碎片整理"——这些是我们的存储池增量(F11)。

---

## 3. LMCache → 跨请求/跨实例 KV 复用 + 多存储后端

源码入口:`3rdparty/lmcache/lmcache/`、`csrc/storage_backends/`、`rust/`、`examples/`。

### 借鉴点

| LMCache 设计 | 我们对应 | 说明 |
|--------------|----------|------|
| 跨请求/跨实例 KV 复用,降 TTFT | 前缀复用 + D-direct | 见 features F1。LMCache 的"长 system prompt / RAG / 多轮"复用场景与我们 agent 多轮定位一致 |
| 多存储后端:CPU memory / local disk / Redis | L1-L3 分层后端 | 见 storage-layer 分层表。LMCache 的后端抽象可作 L2/L3 实现参考 |
| `csrc/storage_backends`(C++ 后端) | Rust 存储层后端 | 我们用 Rust 重写存储层,但后端策略(分片、压缩、传输)可参考 LMCache 的 C++ 实现思路 |
| `rust/` 目录(LMCache 已有 Rust 组件) | 存储层 Rust 技术栈 | 印证 Rust 适合写存储层;可参考其 Rust/C++ 桥接与 FFI 模式 |
| 与 vLLM 集成的 KV manager 拦截 | 计算层 worker ↔ 存储池 client | 见 compute-layer。LMCache 作为 vLLM 的 drop-in 优化,其"拦截 KV 读写"的模式可参考我们 Python worker 的 runtime client 设计 |

### 关键差异

- LMCache 是 vLLM 的**附加层**,不改变 vLLM 实例私有 HBM 的归属;我们是**重做存算分离架构**,HBM 归存储池。
- LMCache 无全局 radix 内容寻址(靠 prefix hash 匹配);我们 radix tree + 内容寻址 + 位置视图一跳返回。
- LMCache 无执行模式选择(PD分离/混部/D-direct);这些是我们的调度层增量。

---

## 4. vLLM → 计算层(PagedAttention / worker / KV connector 接口)

源码入口:`3rdparty/vllm/vllm/v1/`、`vllm/distributed/kv_transfer/kv_connector/`、`vllm/model_executor/`。

vLLM 是本系统**计算层(Python + Triton)**的直接参考。SGLang/LMCache/Mooncake/MemCache/UCM 提供存储/传输与引擎侧缓存框架(MemCache 昇腾对照;UCM 为引擎插件层),vLLM 提供计算面,Dynamo 提供编排面。详见 [`vllm/`](vllm/)。

### 借鉴点

| vLLM 设计 | 我们对应 | 说明 |
|-----------|----------|------|
| **PagedAttention**(block 分页 + block table) | 存储池 KV block + worker block table | 见 compute-layer。block 粒度对齐,worker 仍用 block table 做 paged attention,物理位置由存储池元数据定 |
| **`KVConnectorBase_V1`** 外部 KV 插件接口(scheduler/worker 双侧 + metadata + layer-wise mixin) | 计算层 worker ↔ 存储池 client | 见 vllm/compute.md。接口形态直接参考;LMCache/Mooncake/NIXL/FlexKV 均已实现为 connector,印证接入路径可行 |
| **`SupportsHMA`**(hybrid memory allocator 能力标记) | (无对应——方案 Z 是本系统增量) | `SupportsHMA` 声明 connector 支持 HMA(多 KV cache group 混合架构,如 Mamba+attention),需 `request_finished_all_groups` 与多 group 释放对齐。**注意:它不是"外部管 HBM"**——vLLM 的 HBM 始终引擎自分配,connector 只借做传输。方案 Z 的"池管 HBM 放置"vLLM 无对应标记,需自设计(见 [`../architecture/compute-layer.md`](../architecture/compute-layer.md)) |
| **`ExternalBlockHash`**(跨实例外部哈希) | 存储池内容寻址 `(model_id,layer,block_hash)` | worker 向存储池查前缀的自然键,与存储池内容寻址直接对接 |
| **`GPUModelRunner`**(load_model + execute_model + block table 维护) | 计算层 worker 生命周期与执行循环 | worker 生命周期(Warm→Serving→Drain)、execute_model、block table 维护直接借鉴 |
| **权重 offloader**(UVA + `_prefetch_checkpoint`) | 权重归存储池 + 计算层流式加载 | vLLM 的权重流式喂 GPU 是"权重存算分离"的原型 |
| **spec decode**(proposer↔speculator + rejection sampling) | Draft 池↔Decode 池 | proposer/speculator 划分参考 |

### 关键差异

> 注:vLLM 自 2026 Q3 roadmap([#48168](https://github.com/vllm-project/vllm/issues/48168))起主动向存算分离演进:原生多层 KV offload(`vllm/v1/kv_offload/`:CPU/FS/Obj)+ KV Events(`vllm/distributed/kv_events.py`)已落地 HEAD `ab132ee98`;`session_id`/`continuation_id` 跨 session 协调(#48501)、layerwise/sparse offload API(#48203)仍是 RFC。差距已收窄,详见 [`vllm/overview.md`](vllm/overview.md) "KV 大规模管理演进"。

- vLLM 的 KV/调度/元数据仍 **per-instance、单实例视角**(`KVCacheManager`/`BlockPool`/`Scheduler` + `kv_offload` 全在引擎进程内,tier 私有);我们归存储池/控制面**集群权威**。
- vLLM 多层 offload 是**单实例内级联**(GPU↔CPU↔NVMe/Obj 同机);我们是**跨节点池**统一管 L0–L3。
- vLLM **无 radix tree**(APC hash 顺序匹配 + `OffloadKey` 平铺键)、**无集群位置视图/本地命中**(KV Events `medium` 仅单实例介质标记);我们 radix + 位置视图 + D-direct。
- vLLM connector 是**可选 per-instance 插件**;我们是**必经集群级路径**(存储池 client 常驻)。
- vLLM HBM **引擎自分配**(offload/connector 只借传输);我们**池管 HBM 放置**(方案 Z,vLLM 无对应)。
- vLLM attention 主路径 **C++/CUDA**(FlashAttention);我们选 **Python + Triton**(自定义核门槛与生态不同)。
- vLLM worker **有状态**(加载模型 + HBM KV,崩溃丢 KV);我们 **无状态**(状态全剥离,秒级伸缩,F4 续推)。

---

## 5. Dynamo → 我们的编排层 / 控制面 / KVBM 分层

源码入口:`3rdparty/dynamo/lib/`(Rust 核心)、`components/`、`lib/runtime/`。Dynamo 是 NVIDIA 的"推理引擎之上的编排层",把 vLLM/SGLang/TRT-LLM 作为可插拔 worker 协调成多节点系统。**Rust 写核心性能路径 + 控制面**,是 lake 三语言分层(Rust 存储/Go 控制/Python 计算)中 Rust 控制面/编排的直接参照系。深度分析见 [`dynamo/overview.md`](dynamo/overview.md)。

### 借鉴点

| Dynamo 设计 | 我们对应 | 说明 |
|------|----------|------|
| **`StorageTier`(Device/HostPinned/Disk/External)** | L0/L1/L2/L3 | tier=介质非位置,与 lake"层=介质非位置"一致;`from_kv_medium` 字符串→tier 映射可参考 wire 格式 |
| **`Placement { owner, tier }`** | `locations` 多层位置 | `PlacementOwner`(LocalWorker/Shared)区分私有/共享,对应 lake"副本归池非 worker 私有" |
| **`ExternalSequenceBlockHash`(父哈希链式)** | `block_hash = hash(parent ‖ tokens)` | 注释明示 parent block hash 编入,印证 lake 链式哈希防误复用 |
| **`PlacementEvent` 位置变更推送** | 位置视图权威变更触发推送 | 事件驱动镜像推送 |
| **KVBM logical/physical/engine 三层** | 存储池 元数据/传输/运行时 | 分层组织可借鉴(`kvbm-logical`=radix+locations,`kvbm-physical`=布局+传输,`kvbm-engine`=运行时+offload+object) |
| **KV-aware router(`overlap_blocks` 量化)** | Router 命中感知选路 | 命中 overlap 量化,比 lake"命中/不命中"更细 |
| **transports 多后端可插拔**(etcd/nats/tcp/zmq) | 通信选型(见 #3) | 按部署形态选通信栈;KV 事件走 NATS、discovery 走 K8s CRD/etcd——"控制面存储 vs 事件面"分离 |

### 关键差异(我们更彻底)

- **存算分离彻底度**:Dynamo 的 KV 仍由 engine 持有,KVBM 是"offload 层"(把 engine 的 KV 卸到 CPU/SSD/远端);lake **HBM 归池、worker 不拥有任何内存**,KVBM 式 offload 在 lake 是池的统一放置(方案 Z),非引擎私有缓存的延伸。
- **控制面一致性**:Dynamo KV 事件走 NATS(best-effort 事件流)、discovery 多后端,无全局强一致位置视图;lake **位置视图权威在存储控制面进程内存**(单写者线性一致),etcd 降频 checkpoint,Router 本地镜像一跳命中(见 [`../architecture/control-plane.md`](../architecture/control-plane.md)、[`../architecture/consistency.md`](../architecture/consistency.md) §1)。Dynamo 偏"事件流编排",lake 偏"内存强一致权威 + 镜像"。
- **radix 归属**:Dynamo KV-aware router 用 block 哈希 overlap,radix 前缀树/内容寻址仍依赖底层 engine;lake 把 radix 归存储池统一管。
- **执行模式**:Dynamo 侧重 PD/E-P-D 物理隔离;lake 多 D-direct(本地命中零传输)与混部,Dynamo 无明确对应。

---

## 6. Ascend MemCache → KV 对象池异构对照（昇腾）

源码入口:`3rdparty/memcache/`（嵌套 `memfabric_hybrid`：recursive init 会拉下；非 recursive 时才不拉，审计 OneCopy 需确认已 init）。深度分析见 [`memcache/`](memcache/)。

MemCache 与 **Mooncake store 同层**：分布式 **exact-key** KV 对象池 + 异构传输（MemFabric OneCopy），**不是** radix/控制面参考。昇腾 / vLLM-Ascend 路径上对照 Meta/Local 拆分与多层介质；lake 生产数据面仍以 Mooncake TE（复用 A）为主。

### 借鉴点

| MemCache 设计 | 我们对应 | 说明 |
|---------------|----------|------|
| **MetaService / LocalService** | 存储控制面 + KV Node 贡献内存 | 元数据面与贡献 HBM/DRAM 的数据面分离，形态可对照 |
| **HBM / DRAM / SSD + 高低水位淘汰** | L0–L3 冷热 | 多层与降层思想；lake 另有 L3 SSOT、引用冻结、前缀亲和 |
| **MemFabric OneCopy**（RH2D / D2RH …） | Transfer Bus（Ascend 路径） | 协议/介质路径分类可借鉴；NVIDIA 侧仍走 Mooncake TE |
| **ObjectStore put/get/exist** | kv-pool 字节 API | API 形近；lake 键为内容寻址 + radix，非应用 string key |
| **vLLM-Ascend KV pool backend** | worker↔池 | 外部集成样板（代码不在本仓） |

### 关键差异

- MemCache 为 **exact-key 对象店**；lake 为 **内容寻址 + radix 前缀复用 + 强一致位置视图**。
- MemCache HA 文档偏 **尽力而为**；lake 位置视图权威在控制面内存、失败走 F4 重路由，不照搬其 Lease 语义作权威模型。
- MemCache 深度绑定 **昇腾 / MemFabric**；lake 默认传输栈仍是 Mooncake TE，MemCache 作异构对照而非默认复用 A 的替代。

---

## 7. UCM → 引擎侧统一缓存框架（PD-via-pool）

源码入口:`3rdparty/ucm/`。深度分析见 [`ucm/`](ucm/)。

UCM 与 **LMCache 同层**：挂在 vLLM 等引擎上的 **KVStore + connector + 可选稀疏**；PD 文档列出直传 / DRAM 中介 / 统一池，**统一池是首选而非唯一**，叙事主推经池中转（三种路径释义见 [`ucm/architecture.md`](ucm/architecture.md) §4）。可消费 Mooncake store。lake 借鉴其 store 原语、工厂与 PD-via-pool 叙事；不照搬「引擎补丁中心 + 可选插件」权威模型。

### 借鉴点

| UCM 设计 | 我们对应 | 说明 |
|----------|----------|------|
| **`UcmKVStoreBaseV1`** lookup/prefetch/load/dump | kv-pool / Transfer Bus 客户端原语 | API 边界清晰；键语义仍换成内容寻址 |
| **`UcmConnectorFactoryV1`** 多后端 | L2/L3 后端注册 | 工厂懒加载对照 |
| **`UCMConnector` / LayerWise / PD** | worker↔池、P5 流水线 | vLLM connector 工业集成样板 |
| **PD via unified storage pool** | 存算分离 + PD/混部 | 叙事同向；lake 另补 D-direct 与方案 Z |
| **`UcmSparseBase`** | （Could）长上下文 | 算法与存储解耦可记；非 P0–P4 必做 |

### 关键差异

- UCM 是**引擎插件框架**；lake 是**池权威 + 无状态 worker**。  
- UCM 前缀靠 block hash + store probe；lake 靠控制面 radix + 位置视图一跳。  
- UCM 无三模式 Router / D-direct；稀疏/Blend 范围大于 lake 近期范围。

---

## 设计取舍:站在参考项目之上

| 我们的设计层 | 主要参考 | 我们多做的(更彻底) |
|--------------|----------|---------------------|
| **计算层**(worker/attention/runner) | **vLLM**(PagedAttention/`GPUModelRunner`/spec decode) | worker 无状态化(模型/KV 从存储池读写);attention 核用 Triton |
| **worker↔存储池接入** | **vLLM `KVConnectorBase_V1`** + **UCM connector** 集成样板 | connector 从可选插件升为存储池必经路径;集群级权威。注:`SupportsHMA`(HMA,多 KV group)≠ 方案 Z,方案 Z 为本系统增量 |
| L0-L3 分层 | SGLang HiCache | L1/L2 也归存储池(非实例私有);统一冷热/生命周期 |
| KV Pool 数据面 | Mooncake transfer-engine + store；**Ascend MemCache**(异构对照) | 内容寻址 + radix + 多模型配额/GC/碎片整理；MemCache 作 Meta/Local·多层·OneCopy 对照(exact-key,非 radix) |
| 前缀复用 | SGLang RadixAttention + LMCache | radix 归存储池 + 位置视图一跳 + 反向回传生长 |
| 执行模式 | DistServe/Splitwise + HiCache PD + **UCM PD-via-pool** | 三模式逐请求选路 + D-direct(本地命中直跳) |
| 放置/调度边界 | (我们的方案 Z,原创) | 存储池主动放置 + 调度器单向消费 |
| **编排层/控制面** | **Dynamo**(KVBM logical/physical/engine + KV-aware router) | 位置视图权威在存储控制面进程内存、etcd 降频 checkpoint(Dynamo 走 NATS 事件流);HBM 归池而非 engine offload;Rust 存储控制面 + Go 调度控制面分工(Dynamo 单一 Rust 编排) |
| **超低延迟 decode / PD 胶水** | **TileRT**(`TileRTConnector` + NIXL/Mooncake;核闭源) | 借鉴 connector claim、MTP-aware 传 KV、双传输;拒绝单槽/bs=1/引擎私有 HBM。详见 [tilert/](tilert/) |

## 8. TileRT → 超低延迟 decode + vLLM PD 插件样板

源码入口:`3rdparty/tilert/tilert/`、`tilert/pd_vllm/`。深度文档:[tilert/overview.md](tilert/overview.md) · [pd-vllm.md](tilert/pd-vllm.md) · [pain-points.md](tilert/pain-points.md)。

### 借鉴点

| TileRT 设计 | 我们对应 | 说明 |
|-------------|---------|------|
| `TileRTConnector` 选择性 claim + `MultiConnector` 共存 | worker↔池 / 多执行模式旁路 | 见 [pd-vllm.md](tilert/pd-vllm.md);对照 [`vllm/compute.md`](vllm/compute.md) |
| 控制 TCP/HTTP × RDMA 数据面拆分 | Transfer Bus + agent 控制 RPC | 与 Mooncake TE 正交 |
| MTP draft 层 KV 一等传输 | `pool_kind=DRAFT` + PD 写回 | prefill 必须物化 draft KV 的约束可借鉴 |
| 模型 profile 缝(extract/convert/握手版本) | 池不解释布局、按不透明块 + 注册元数据 | 布局版本化可借鉴;身份仍用内容寻址 |

### 关键差异

- TileRT **核闭源**、钉 8×B200、公开树无 radix/分层池/`bs>1`——**不是**存储面或通用计算层蓝图。
- PD 是 **vLLM block_id → 单槽 inject**；lake 是池权威 + 混合执行（含 D-direct）。
- 过载 429 在小路由器内；lake 过载归 gateway。

## 代码级复用策略（按模块，互不替代）

`3rdparty/` 默认是**设计/算法参考**。进入实现后，**优先真复用**的只有两条，且落在**不同 lake 模块**——不能互相顶替，也不等于整仓依赖 submodule：

| # | 复用来源 | 落点（lake 模块） | 复用什么 | 不复用 / 自建 |
|---|----------|-------------------|----------|---------------|
| A | **Mooncake transfer-engine**（[+ 可选 mooncake-store 作介质后端]） | `rust/transfer`（P4.4:`Transport`+`TcpTransport`+`TransferServer`；`TcpDataService` 正名自 SkeletonKv）；字节后端可挂 `rust/kv-pool` / `tiered-store`；真 RDMA→P5 | RDMA/多 NIC 零拷贝传输骨架；store 仅作 L2/L3 **不透明字节**载体 | 内容寻址、radix、L0 归属、位置视图权威；P4 不接 Mooncake FFI |
| B | **Dynamo KVBM logical**（`BlockRegistry` / `PositionalRadixTree` / `BlockManager` 等；宜 fork 抽 crate） | `rust/kv-pool` + `rust/controlplane`（池内索引、分层块管理、presence） | 前缀 radix、每层 block 池、presence markers、可插拔驱逐索引 | G1「引擎外拥 HBM」、demote-only offload、NATS 弱一致事件；lake 补 **L0 归池** + **promote**；**位置视图权威在控制面进程内存，etcd 降频 checkpoint** |

关系（正交）：

```
请求路径上的 KV 字节搬迁  ──(A)──►  Mooncake TE     →  rust/transfer
前缀命中 / 层内块生命周期 ──(B)──►  Dynamo kvbm-*   →  rust/kv-pool · controlplane
```

- **A 不管「这块 KV 算不算前缀命中」**；**B 不管「跨节点怎么 RDMA 搬字节」**。
- Dynamo `kvbm-physical::TransferManager` 是**抽象**，生产数据面仍优先接 A（Mooncake TE），避免两套传输栈。

**A 行补充：为什么是 TE 而不是 NIXL**。注意 dynamo 自己没有传输引擎，它的 KV 传输生产后端是 NIXL——所以真正的备选是 NIXL。

- 两者形态不同：TE 是为 KV 搬迁写的专用引擎，segment 注册、slice 切分、拓扑选 NIC、多 NIC 聚合、故障重传、TCP 退化都是现成的；NIXL 是"统一 API + 可插后端"的抽象层。lake 自己的抽象已经有了（`rust/transfer` 的 `Transport` trait），再引入一层抽象没有增量。
- 寻址模型合拍：TE 的 `(SegmentID, offset, len)` 就是 lake `Location{segment_id, offset}` 的形状，接起来不用翻译；NIXL 的 agent/descriptor 风格对不上。
- 多 NIC 开箱即用：TE 自带 NUMA 拓扑发现和带宽聚合（`Topology::selectDevice`）；NIXL 要多网卡聚合得自己配 `UCX_NET_DEVICES`。
- 生产实证和生态：TE 在 Kimi 生产环境跑 PD 分离的 KV 搬迁；SGLang HiCache、vLLM `MooncakeConnector`、LMCache、TileRT 都接它，将来 lake 与任何引擎对接，TE 是共同语言。
- 但不是终身绑定：P5 若发现 FFI 成本超预期，NIXL 仍可作为 `Transport` trait 的另一个实现。

**B 行补充：为什么 physical/engine 不能像 logical 那样 vendor**：

- logical 层不关心"KV 归谁"——它就是块池、链式哈希、presence、驱逐索引这些纯数据结构，拿过来改一张小表（见 `rust/vendor/UPSTREAM.md`）就能用，还白拿 480 个单测。physical 和 engine 不一样：它们整个建在"KV 归引擎、引擎发起、只往下沉（demote-only）"的假设上，这正是 lake 要推翻的模型。
- 控制流对不上：dynamo 里传输由引擎内的 leader 在 offload 编排里发起；lake 是池做调度、agent 执行、双向流动（预放置、读 miss 回填、节点间直传）。physical 层能不能用，取决于 engine 层的控制模型能不能用——而 engine 层那套 leader/collectives/per-instance 协调，是 [`../architecture/control-plane.md`](../architecture/control-plane.md) 明确拒绝的。
- 布局也对不上：lake 用 page-first（整块所有层连续、一条 `(ptr, len)` 传给引擎，对齐 SGLang），和 dynamo 的 layout 假设不同。
- vendor 的主要收益——现成的测试——在这两层拿不到：它们的正确性依赖真实环境（NIXL、RDMA 网卡、GPU、S3），拷过来单测跑不起来，只剩下钉工具链和 re-vendor 合并的成本。
- 语言边界：engine 层管对接推理引擎，lake 的引擎对接在 Python 计算层，用不上它。
- 推论：就算传输底座改选 NIXL，也只能救回 physical 的 transfer 半边，engine 层依然不可用。真正的决策点是"KV 归谁、谁发起传输"，传输底座的选择是它的下游。
- 其余（SGLang HiCache 策略、LMCache 后端思路、vLLM `KVConnectorBase_V1`、Dynamo kv-router 选路公式）继续以**借鉴/对照**为主，默认不链进依赖树；计算层（P5）再评估 vLLM/SGLang 引擎经 connector 接入。

**现状（P4.2 合入 / P4.3）**：复用 **B** vendor + controlplane radix/驱逐。**P4.3**：`LocalTierEngine`（写回**只 L2**；L3 仅 demote/cap XOR；`pin`≈`lock_ref`；`TierSideEffects`）+ `TierPipeline` + `apply_location_events`；PutEnd=Mooncake COMPLETE。对齐 `storage-layer.md` 写回路径与 SGLang demotion-only L1。真 NVMe/跨机 defer P5；满块顺便写 L1 留 P7。

锚点（复用时回溯）：

- A：`3rdparty/mooncake/mooncake-transfer-engine/`（见 [`mooncake/transfer-engine.md`](mooncake/transfer-engine.md)）
- B：`rust/vendor/kvbm-logical/`（源：`3rdparty/dynamo/lib/kvbm-logical/`）— `PositionalRadixTree` / `BlockManager`（符号表见下节）

## 实现参考顺序建议

P4(KV Pool 原型,Rust)时按此顺序；**1 与「Dynamo KVBM」分属上表 A/B，并行不替代**：

1. **Mooncake transfer-engine**（代码复用 A）：RDMA 零拷贝 → `rust/transfer` Transfer Bus。
2. **Dynamo kvbm-logical**（代码复用 B，fork 抽 crate）：radix + `BlockManager`/`presence_markers` → `kv-pool` / controlplane；补 L0 + promote。
3. **Mooncake store + LMCache storage_backends**：KV store 分片/后端 → L3 + L2/NVMe（字节层；寻址仍归 B）。
4. **Ascend MemCache**（对照，非默认复用）：Meta/Local、多层淘汰、OneCopy 路径分类 → 昇腾部署与对象池 API 形态对照；不替代 A/B。
5. **UCM `UcmKVStoreBaseV1` + connector**（对照，偏 P5）：store 原语/工厂、PD-via-pool 叙事、layer-wise connector → worker↔池形态；不替代 A/B，不引入稀疏必选项。
6. **SGLang HiCache HiRadixTree + page_first_direct**：节点记位置 + 布局策略 → `locations` 元数据 + 分块流水线（对照 B，非第二套树）。
7. **SGLang HiCache prefetch/write-back 策略**：迁移触发与写回频率 → 冷热迁移 + decode 写回 N。
8. **LMCache rust/ + 跨实例复用**：Rust 存储层工程模式 + 复用场景验证。
9. **vLLM `KVConnectorBase_V1` + `GPUModelRunner`**（偏 P5）：worker ↔ 存储池 client 形态 + layer-wise 流水线。
10. **TileRT `pd_vllm`**（偏 P5 PD 胶水对照）：`TileRTConnector` claim / MTP-aware 传 KV / NIXL·Mooncake；**不**复用闭源 `.so`。

### Dynamo 参考补充(编排层/控制面,跨阶段)

Dynamo 跨 **P4(存储)** 与 **控制面(选路/通信)**,不进上面"抄源码顺序",按职责分块借鉴(符号锚点:`grep -n "符号名" 3rdparty/dynamo/<路径>`)。

**P4 存储层(Rust)— KVBM 三层结构:**

| lake 需求 | Dynamo 可复用符号 | 文件:行 | 改造点 |
|---|---|---|---|
| 强一致位置视图 | `presence_markers: HashMap<TypeId,u32>` + `mark_present/absent/has_block/has_any_block` | `lib/kvbm-logical/src/registry/handle.rs:128-188` | `T` 换 L0–L3;补 L0 跟踪(Dynamo G1 不进) |
| 每层一个池 | `BlockManager<T>` + `BlockStore<T>`(单 mutex + `SlotState` 状态机) | `lib/kvbm-logical/src/manager/mod.rs:31`、`pools/store.rs:110` | L0 也实例化(Dynamo 不实例化 G1) |
| radix 索引 | `BlockRegistry` + `PositionalRadixTree<Weak>` | `lib/kvbm-logical/src/registry/mod.rs:110` | 几乎直接用 |
| 驱逐策略可插拔 | `InactiveIndex` + backends | `lib/kvbm-logical/src/pools/store.rs:54` | lake Authority：`LineageBackend::with_frequency`（叶子+TinyLFU）；`MultiLruBackend` 对照；不照搬纯 `Lru`/`Fifo`（见 `rust/vendor/UPSTREAM.md`） |
| 冷热迁移策略链 | `OffloadPolicy<T>` + `Either<Ready,BoxFuture>`(零分配;`PresenceAndLFUFilter`≈LFU) | `lib/kvbm-engine/src/offload/policy.rs:219,47` | 加 promote 方向(Dynamo demote-only) |
| demote pipeline | `Pipeline<Src,Dst>` 5 段 + `auto_chain` | `lib/kvbm-engine/src/offload/pipeline.rs:65`、`engine.rs:537` | 镜像出 promote pipeline |
| L3 对象存储 SSOT | `ObjectBlockOps` + `ObjectLockManager`(`.meta`/`.lock` 分布式锁) + `KeyFormatter` | `lib/kvbm-engine/src/object/mod.rs:152,280,39` | 直接用 |
| 布局/介质解耦 | `PhysicalLayout{layout,location}` + `StorageKind` | `lib/kvbm-physical/src/layout/physical.rs:30` | 直接用 |
| 传输引擎抽象 | `TransferManager` + `execute_transfer` + `export/import_metadata` | `lib/kvbm-physical/src/manager/mod.rs:42,227,112` | 池级共享视图替代 worker 本地 |
| 跨层 block 携带 | `TieredBlock` + `BlockAccessor::find` | `lib/kvbm-engine/src/leader/accessor.rs:20,96` | 扩成 L0–L3 四态 |

**控制面(选路/通信)— kv-router + transports/discovery:**

| lake 需求 | Dynamo 可复用符号 | 文件:行 |
|---|---|---|
| 本地命中视图镜像(前缀匹配→每 worker 命中块数) | `RadixTree::find_match_details` | `lib/kv-router/src/indexer/radix_tree.rs:89` |
| 命中感知选路纯函数 | `DefaultWorkerSelector::worker_logit`(`adjusted_prefill = raw - Σ(weight×overlap)`) | `lib/kv-router/src/scheduling/selector.rs:110` |
| 分层 overlap 加权 | `cache_hit_estimates_from_tiered_matches` + `OverlapSignals` | `lib/kv-router/src/scheduling/overlap.rs:201,24` |
| 链式哈希递推 + parent 校验 | `compute_next_sequence_hash` + `apply_stored`(含自引用检查) | `lib/tokens/src/lib.rs:77`、`lib/kv-router/src/indexer/radix_tree.rs:227` |
| 拓扑传输约束(硬/软) | `KvTransferEnforcement`(Required/Preferred) | `lib/kv-router/src/protocols.rs:232` |
| 位置视图事件流→索引 + gap/replay | `ListenerLoop::apply_live_batch` | `lib/kv-router/src/services/indexer/listener.rs:320` |
| 服务发现统一接口 + 多后端 | `Discovery` trait + `Arc<dyn Discovery>`(etcd/memory/file/k8s) | `lib/runtime/src/discovery/mod.rs:781`、`distributed.rs:57` |
| etcd key space 隔离 | `v1/<类别>/<namespace>/<component>/...` bucket 前缀 | `lib/runtime/src/discovery/kv_store.rs:21-23` |
| worker 存活自动收敛 | etcd lease 绑定实例生命周期 | `lib/runtime/src/transports/etcd/lease.rs:17` |
| 权威 etcd vs 高频事件流分离 | `Store/Bucket`(etcd) + `EventTransportTx/Rx`(NATS/ZMQ) | `lib/runtime/src/storage/kv.rs:116`、`transports/event_plane/transport.rs:28` |

**关键差异(lake 更彻底,需自补):**
- **L0 归池**:Dynamo G1(HBM)无 `BlockManager`,KV 由 engine 外部拥有(`lib/kvbm-engine/src/offload/engine.rs:468-470` "G1 is externally owned");lake 必须有 `BlockManager<L0>` + `presence_markers` 覆盖全四层。
- **双向 pipeline**:Dynamo offload demote-only(G1→G2→G3/G4),promote 走 leader session 拉取;lake 要自建 promote pipeline(L3→L2→L1→L0 预放置)。
- **控制面一致性**:Dynamo KV 事件走 NATS(best-effort,`lib/runtime/src/distributed.rs:483` 丢了即跳过);lake 位置视图权威在存储控制面进程内存、etcd 降频 checkpoint,Router 本地镜像一跳命中。

> 参考源码时注意:五个 submodule 各带自己的 `.claude/` 规则(如 sglang 的 modify-component-must-read、mooncake 的 skills),那些是**修改它们自身代码**的约束,与我们参考其设计无关,忽略。

## submodule 使用约定

- `3rdparty/` 只读参考,**不修改** submodule 内代码。如需改造,fork 后换 URL。
- clone 本仓库后需 `git submodule update --init --recursive` 拉取。
- 升级 submodule:在对应目录 `git checkout <ref>` 后回根目录 `git add` 提交指针更新;在本文"检出"列同步记录。
- submodule 体积较大(SGLang/Mooncake/vLLM/Dynamo 各数百 MB),磁盘紧张或 CI 提速用浅克隆:`git clone --recurse-submodules --depth 1 --shallow-submodules <repo>`(浅克隆后无法在 submodule 内切换 ref,升级需先 `git submodule deinit -f <path>` 再深克隆 init)。

## 跨项目专题对比

跨多个 submodule 的机制专题(非单项目分目录):

- **PD 分离的控制机制**:vLLM(KVConnector 插件 + 外部 proxy)vs SGLang(固定角色 + 内建队列状态机)对比——角色划分、配对握手、KV 传输推进、失败处理,及与 lake"KV 归池 + 逐请求选模式"的差异。见 [`pd-disaggregation.md`](pd-disaggregation.md)。

## 非 submodule 文献参考

除上述五个源码 submodule 外,本系统还参考了 **DualPath**(论文 arXiv:2602.21548v2,非 submodule,未引入源码)——双网络隔离下的双路径 KV 加载,直接对应 [`../architecture/kv-cache-pool.md`](../architecture/kv-cache-pool.md) "双网络路径"与 [`../architecture/data-flow.md`](../architecture/data-flow.md) §3.4 D→P 流。分析(机制/借鉴点/关键差异)见 [`dualpath.md`](dualpath.md),文献总览见 [`references.md`](references.md)。
