# 07 — 相关工作与文献

本仓库的设计大量借鉴以下工作。理解它们是评估本系统假设的基础。

## 源码深度参考(3rdparty submodule)

项目源码已引入 `3rdparty/`(submodule),各有分目录的深度分析文档:

- **SGLang HiCache** → [`sglang/`](sglang/):[总览](sglang/overview.md) · [分层机制](sglang/hicache.md) · [存储后端](sglang/storage-backends.md) · [block 生命周期](sglang/block-lifecycle.md) · [thinking 控制](sglang/thinking-control.md) · [上游痛点](sglang/pain-points.md)
  - L1/L2/L3 三层(L1/L2 私有、L3 共享)、HiRadixTree、prefetch/write-back 策略;block 何时释放/彻底放弃见 block-lifecycle;issue/roadmap 痛点与 lake 对照见 pain-points。
- **LMCache** → [`lmcache/`](lmcache/):[总览](lmcache/overview.md) · [跨实例复用与后端](lmcache/sharing-and-backends.md)
  - 跨实例 KV 复用、内容寻址去重、多存储后端、Rust 裸设备 I/O。
- **Mooncake** → [`mooncake/`](mooncake/):[总览](mooncake/overview.md) · [传输引擎](mooncake/transfer-engine.md) · [KV 存储与池化](mooncake/kv-store.md)
  - RDMA 零拷贝传输引擎、对象级 KV 池、PD 分离。
- **vLLM** → [`vllm/`](vllm/):[总览](vllm/overview.md) · [计算层抽象与存算分离接入点](vllm/compute.md) · [block 生命周期](vllm/block-lifecycle.md) · [上游痛点与 lake 对照](vllm/pain-points.md)
  - **计算层参考**:PagedAttention、worker/model runner、`KVConnectorBase_V1` 接口、spec decode。
  - **KV 大规模管理演进**(Q3 2026 roadmap):原生多层 KV offload(`vllm/v1/kv_offload/`)+ KV Events 已落地;`session_id`/`continuation_id`、layerwise offload 仍是 RFC。
- **Dynamo** → [`dynamo/`](dynamo/):[总览](dynamo/overview.md)
  - **编排层/控制面参考**(NVIDIA,Rust):推理引擎之上的编排层,KV-aware router + KVBM(GPU→CPU→SSD→远端 三层 offload)+ 多后端通信(etcd/nats/tcp/zmq)。Rust 写控制面/编排,是 lake Rust 存储控制面的直接参照系。
- **TileRT** → [`tilert/`](tilert/):[总览](tilert/overview.md) · [vLLM PD 插件](tilert/pd-vllm.md) · [痛点与 lake 对照](tilert/pain-points.md)
  - **超低延迟 decode**(tile runtime,核在闭源 `.so`)+ **vLLM prefill→TileRT decode**(`TileRTConnector`、NIXL/Mooncake、MTP-aware 传 KV)。不作存储池/radix 参考。
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

## KV Cache 复用与传输

- **vLLM / PagedAttention** (SOSP'23): 块状 KV 内存管理，本系统 block 粒度的原型。
- **CacheGen** / **CacheBlend**: KV cache 的压缩与复用。
- **AttentionStore** (Meta): 把 KV cache 当作可复用的缓存层。
- **SGLang**: RadixAttention 前缀复用，本系统 radix tree 索引的来源。其 **HiCache**(L1 GPU / L2 host / L3 distributed 分层)是本系统 L0-L3 分层的主要参考;**源码已引入** `3rdparty/sglang`,对应与差异见 [`3rdparty-reference.md`](3rdparty-reference.md)。
- **LMCache**: 跨请求/跨实例 KV 复用,多存储后端(CPU/disk/Redis)。**源码已引入** `3rdparty/lmcache`,对应见 [`3rdparty-reference.md`](3rdparty-reference.md)。
- **DualPath** (DeepSeek-AI/PKU/THU, arXiv:2602.21548v2): 双网络(compute/storage NIC 隔离)下的双路径 KV 加载——借 decode 闲置 storage NIC 从存储加载 KV,再经 compute network RDMA 回传 prefill。针对 agentic 多轮(KV 命中 ≥95%,瓶颈是存储 I/O 而非计算)。本系统**原生支持**(D→P 流,见 [`../architecture/data-flow.md`](../architecture/data-flow.md) §3.4)且更彻底:NIC 带宽归池统一分配,非引擎"借用";并有 D 侧 KV 已在 HBM 的零存储读取特例。分析见 [`dualpath.md`](dualpath.md)。

## 弹性与冷启动

- **ServerlessLLM** (OSDI'24): serverless 场景下 LLM 的快速加载与冷启动优化。
- **dLoRA / PetS**: serverless 推理的弹性调度。

## 存储分层

- **Lakehouse / Paimon / Iceberg**: 存算分离的数据层范式（本仓库命名 "lake" 的由来），但其面向分析负载；本仓库把类似分层理念迁移到推理 KV。

## 通用参考

- **DeepSpeed-Inference**, **TensorRT-LLM**, **Orca** (continuous batching): 推理引擎基线，本系统在其上做存算分离的解耦。

> 注：以上为方向性参考。SGLang/Mooncake/LMCache/vLLM/Dynamo 五者源码已引入 `3rdparty/`(submodule),与本项目设计的逐层对应、借鉴点与关键差异见 [`3rdparty-reference.md`](3rdparty-reference.md)。
