# 07 — 相关工作与文献

本仓库的设计大量借鉴以下工作。理解它们是评估本系统假设的基础。

## 源码深度参考(3rdparty submodule)

下列项目源码已引入 `3rdparty/`(submodule),各有分目录的深度分析文档:

- **SGLang HiCache** → [`sglang/`](sglang/):[总览](sglang/overview.md) · [分层机制](sglang/hicache.md) · [存储后端](sglang/storage-backends.md) · [block 生命周期](sglang/block-lifecycle.md) · [MoE 通信](sglang/moe-communication.md) · [权重加载与外部集成](sglang/loading-and-integrations.md) · [thinking 控制](sglang/thinking-control.md) · [上游痛点](sglang/pain-points.md) · [Agentic KV 路线图 #21846](sglang/agentic-kv-roadmap.md)
  - L1/L2/L3 三层(L1/L2 私有、L3 共享)、HiRadixTree、prefetch/write-back 策略;block 何时释放/彻底放弃见 block-lifecycle;MoE 的 DeepEP normal/low-latency 分派见 moe-communication;权重多线程/远端加载与依赖集成边界见 loading-and-integrations;issue/roadmap 痛点与 lake 对照见 pain-points;Agentic/PD 增量传输与 hint 总设计见 agentic-kv-roadmap。
- **LMCache** → [`lmcache/`](lmcache/):[总览](lmcache/overview.md) · [跨实例复用与后端](lmcache/sharing-and-backends.md)
  - 跨实例 KV 复用、内容寻址去重、多存储后端、Rust 裸设备 I/O。
- **Mooncake** → [`mooncake/`](mooncake/):[总览](mooncake/overview.md) · [传输引擎](mooncake/transfer-engine.md) · [KV 存储与池化](mooncake/kv-store.md)
  - RDMA 零拷贝传输引擎、对象级 KV 池、PD 分离。
- **vLLM** → [`vllm/`](vllm/):[总览](vllm/overview.md) · [计算层抽象与存算分离接入点](vllm/compute.md) · [block 生命周期](vllm/block-lifecycle.md) · [上游痛点与 lake 对照](vllm/pain-points.md) · [Q3 KV/Session 调度](vllm/kv-session-roadmap.md)
  - **计算层参考**:PagedAttention、worker/model runner、`KVConnectorBase_V1` 接口、spec decode。
  - **KV 大规模管理演进**(Q3 2026 roadmap #48168):原生多层 KV offload + KV Events 已落地;session 坐标编排见 #48501 / [kv-session-roadmap.md](vllm/kv-session-roadmap.md)。
- **Dynamo** → [`dynamo/`](dynamo/):[总览](dynamo/overview.md)
  - **编排层/控制面参考**(NVIDIA,Rust):推理引擎之上的编排层,KV-aware router + KVBM(GPU→CPU→SSD→远端 三层 offload)+ 多后端通信(etcd/nats/tcp/zmq)。Rust 写控制面/编排,是 lake Rust 存储控制面的直接参照系。
- **TileRT** → [`tilert/`](tilert/):[总览](tilert/overview.md) · [vLLM PD 插件](tilert/pd-vllm.md) · [痛点与 lake 对照](tilert/pain-points.md)
  - **超低延迟 decode**(tile runtime,核在闭源 `.so`)+ **vLLM prefill→TileRT decode**(`TileRTConnector`、NIXL/Mooncake、MTP-aware 传 KV)。不作存储池/radix 参考。
- **Ascend MemCache** → [`memcache/`](memcache/):[总览](memcache/overview.md) · [架构](memcache/architecture.md) · [痛点与 lake 对照](memcache/pain-points.md)
  - 昇腾分布式 KVCache **对象池**(MetaService/LocalService、HBM/DRAM/SSD、MemFabric OneCopy);与 Mooncake store 同层对照,非 radix 控制面。
- **UCM** → [`ucm/`](ucm/):[总览](ucm/overview.md) · [架构](ucm/architecture.md) · [痛点与 lake 对照](ucm/pain-points.md)
  - ModelEngine 统一缓存框架:可插拔 KVStore、vLLM connector、稀疏插件、**PD-via-pool**；与 LMCache 同属引擎插件层。
- **FlexKV** → [`flexkv/`](flexkv/):[总览](flexkv/overview.md) · [架构与 HBM](flexkv/architecture.md) · [痛点与 lake 对照](flexkv/pain-points.md)
  - TACO 多层 KV 卸载:引擎持有 GPU 槽,FlexKV 索引 CPU/SSD/REMOTE,IPC 映射 HBM 做 D2H/H2D;vLLM/SGLang/Dynamo/TRT 均有 connector。
- **Guided / structured decoding** → [`guided-decoding.md`](guided-decoding.md)
  - SGLang × vLLM:xgrammar/llguidance 仅 GPU apply、FSM 仍在 CPU;overlap/async 近零 vs spec+grammar / pending token 的同步气泡;与 lake 重叠契约及抢占时 FSM 游标交接。
- **Sampling 参数** → [`sampling-params.md`](sampling-params.md)
  - SGLang × vLLM 字段对照;`n`≠beam;spec 兼容矩阵;penalty 空泡与 V2;采样状态归属(不进 KV 池)与 `n` 前缀共享。
- **Scheduler→Worker 接口** → [`scheduler-worker-interface.md`](scheduler-worker-interface.md)
  - vLLM `SchedulerOutput` 与 SGLang `ScheduleBatch`/`ForwardBatch` 字段全集、差异与架构根因;供 lake `SchedulerOutput` D1 对照。

与本系统逐层对应、借鉴点、关键差异见 [`3rdparty-reference.md`](3rdparty-reference.md)。

---

## 存算分离 / Disaggregated Serving

- **Mooncake** (Moonshot AI): KVCache-centric disaggregated architecture，把 KV cache 作为独立分离资源池。本仓库 KV Pool 的直接灵感来源。**源码已作为 submodule 引入** `3rdparty/mooncake`,逐层对应见 [`3rdparty-reference.md`](3rdparty-reference.md)。
- **TileRT** (Tile-AI): 超低延迟 tile runtime + **vLLM prefill → TileRT decode** PD 插件。**源码 submodule** `3rdparty/tilert`,见 [`tilert/`](tilert/)。
- **DistServe** (OSDI'24): Disaggregating prefill and decoding，物理隔离 Prefill/Decode 以分别优化吞吐与延迟。
- **Splitwise** (ISCA'24): Efficient generative LLM inference with phase-based disaggregation，按 phase 分离并建模资源。
- **TensorCast** (tensorcast-ai): 张量状态基础设施层——把权重/KV/checkpoint/RL 参数从应用进程抽取为分布式 artifact,Global Store(控制面)规划放置与 fanout,Store Daemon(数据面)持本地张量内存 + CUDA IPC 同机零拷贝 + RDMA/TCP P2P 跨机;policy 预设(cache/durable/ha/cold/warm/pinned)定放置与持久化,retrieval source(local/disk/p2p)定取数路径。**源码已引入** `3rdparty/tensorcast`,与 lake 存储层 + 权重缓存同构,见 [`tensorcast/overview.md`](tensorcast/overview.md)。

## KV Cache 复用与传输

- **Ascend MemCache**: 昇腾分布式 KVCache 对象池（Meta/Local + MemFabric OneCopy）。**源码已引入** `3rdparty/memcache`，见 [`memcache/`](memcache/)。
- **UCM** (ModelEngine): 统一缓存管理——可插拔 KVStore、vLLM connector、PD-via-pool。**源码已引入** `3rdparty/ucm`，见 [`ucm/`](ucm/)。
- **FlexKV** (taco-project / 腾讯云 TACO): 引擎旁 CPU/SSD/远端 KV 卸载，GPU 由引擎持有。**源码已引入** `3rdparty/flexkv`，见 [`flexkv/`](flexkv/)。
- **vLLM / PagedAttention** (SOSP'23): 块状 KV 内存管理，本系统 block 粒度的原型。
- **CacheGen** / **CacheBlend**: KV cache 的压缩与复用。
- **AttentionStore** (Meta): 把 KV cache 当作可复用的缓存层。
- **SGLang**: RadixAttention 前缀复用，本系统 radix tree 索引的来源。其 **HiCache**(L1 GPU / L2 host / L3 distributed 分层)是本系统 L0-L3 分层的主要参考;**源码已引入** `3rdparty/sglang`,对应与差异见 [`3rdparty-reference.md`](3rdparty-reference.md)。
- **LMCache**: 跨请求/跨实例 KV 复用,多存储后端(CPU/disk/Redis)。**源码已引入** `3rdparty/lmcache`,对应见 [`3rdparty-reference.md`](3rdparty-reference.md)。
- **DualPath** (DeepSeek-AI/PKU/THU, arXiv:2602.21548v2): 双网络(compute/storage NIC 隔离)下的双路径 KV 加载——借 decode 闲置 storage NIC 从存储加载 KV,再经 compute network RDMA 回传 prefill。针对 agentic 多轮(KV 命中 ≥95%,瓶颈是存储 I/O 而非计算)。本系统**原生支持**(D→P 流,见 [`../architecture/data-flow.md`](../architecture/data-flow.md) §3.4)且更彻底:NIC 带宽归池统一分配,非引擎"借用";并有 D 侧 KV 已在 HBM 的零存储读取特例。分析见 [`dualpath.md`](dualpath.md)。
- **NVIDIA CMX** (Context Memory Storage): GPU、Dynamo、NIXL、Memos、BF4 和共享 flash 的目标架构。研究文档区分公开实现、伙伴声明和未知项；lake 将相关路径映射为 L2 backend。见 [`nvidia-cmx.md`](nvidia-cmx.md)、[`tools/cmx-sim/`](../../tools/cmx-sim/)、[#22](https://gitlab.com/BeeBreeze/lake/-/issues/22) 和 [#23](https://gitlab.com/BeeBreeze/lake/-/issues/23)。
- **HBM 归属与 KV 卸载**：Dynamo G1、LMCache、FlexKV、HiCache、vLLM `kv_offload`、UCM 对照，以及前缀位置合一与 F4。见 [`hbm-tier-and-offload.md`](hbm-tier-and-offload.md)。
- **Agentic cache workload**: 匿名 Cursor 用量、provider cache 留存、ChatGPT File Library 与 Pool quota 的边界，以及 90%/95% 仿真所需输入。见 [`agentic-cache-workload.md`](agentic-cache-workload.md)。

## 弹性与冷启动

- **ServerlessLLM** (OSDI'24): serverless 场景下 LLM 的快速加载与冷启动优化。
- **dLoRA / PetS**: serverless 推理的弹性调度。

## 存储分层

- **Lakehouse / Paimon / Iceberg**: 存算分离的数据层范式（本仓库命名 "lake" 的由来），但其面向分析负载；本仓库把类似分层理念迁移到推理 KV。

## 通用参考

- **DeepSpeed-Inference**, **TensorRT-LLM**, **Orca** (continuous batching): 推理引擎基线，本系统在其上做存算分离的解耦。

> 注：以上为方向性参考。SGLang/Mooncake/LMCache/vLLM/Dynamo/TileRT/MemCache/UCM/FlexKV 等源码已引入 `3rdparty/`(submodule),与本项目设计的逐层对应、借鉴点与关键差异见 [`3rdparty-reference.md`](3rdparty-reference.md)。
