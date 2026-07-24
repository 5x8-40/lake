# SGLang HiCache — 总览

> 源码:`3rdparty/sglang`(submodule)。本文聚焦 HiCache(分层 KV cache)。  
> **Worker / ModelRunner / 投机解码 / Overlap 异步调度 / 与 vLLM 对照(dummy、DP/TP/PP) / DP 双层管理(引擎 Controller + Gateway cache_aware) / 层 B 演进(`cache_aware_zmq` · SessionAware · KV Indexer)**见 [model-runner.md](model-runner.md)（Overlap 专节）;block 何时释放/彻底放弃见 [block-lifecycle.md](block-lifecycle.md);上游 issue/roadmap 痛点整理见 [pain-points.md](pain-points.md);**Agentic 分布式 KV 总设计(#21846)**见 [agentic-kv-roadmap.md](agentic-kv-roadmap.md);guided/structured decoding 与 overlap 同步见 [../guided-decoding.md](../guided-decoding.md);采样参数对照见 [../sampling-params.md](../sampling-params.md);Scheduler→Worker 字段全集 × vLLM 对照见 [../scheduler-worker-interface.md](../scheduler-worker-interface.md)。

## 一句话定位

HiCache 是在 RadixAttention 之上构建的三层(GPU HBM / 主机内存 / 分布式存储)KV cache 分层系统,通过 HiRadixTree 组织元数据、统一 `HiCacheStorage(ABC)` 接口对接多种 L3 后端,以 local-match / prefetch / write-back 三大操作扩大 KV 容量并跨实例共享前缀。

## 设计哲学

类比 CPU 的 L1/L2/L3 缓存:

| 层 | 介质 | 归属 |
|----|------|------|
| **L1** | GPU HBM | **私有于实例** |
| **L2** | 主机 pinned 内存 | **私有于实例** |
| **L3** | 分布式存储(SSD/远端) | **集群共享** |

`hicache_design.md` 原文:L1/L2 私有于每个推理实例,L3 在集群所有实例间共享——与 CPU L1/L2 私有于核、L3 共享一致。

硬约束(`pool_host/base.py`):host pool 容量必须 > device pool 容量,否则 L2 命中率受损。

## 架构

核心组件:

| 组件 | 文件 | 职责 |
|------|------|------|
| `HiRadixCache` | `python/sglang/srt/mem_cache/hiradix_cache.py` | HiCache 主入口,持有 controller 与 host pool,实现 match/prefetch/load_back/insert/evict/write-back |
| `HiCacheController` | `python/sglang/srt/managers/cache_controller.py` | L1↔L2 DMA、L2↔L3 prefetch/backup 线程、storage 后端生命周期、层传输事件 |
| `TreeNode` | `python/sglang/srt/mem_cache/radix_cache.py` | radix 树节点,记录 L1/L2 存储地址与 L3 key(见 [hicache.md](hicache.md)) |
| `HostKVCache` | `python/sglang/srt/mem_cache/pool_host/base.py` | L2 内存池抽象(alloc/free、布局、逐层 H2D/D2H、零拷贝元数据) |
| `HiCacheStorage(ABC)` | `python/sglang/srt/mem_cache/hicache_storage.py` | L3 后端统一接口(见 [storage-backends.md](storage-backends.md)) |

三大操作(详见 [hicache.md](hicache.md)):
- **local match**:沿 radix 树匹配前缀,返回 L1(前段)+ L2(后段)的连续命中,纯树遍历无数据拷贝。
- **prefetch from L3**:对未命中部分查 L3,命中超阈值则异步拉到 L2,与计算重叠。
- **write-back**:L1→L2(DMA)→ L3(后端写),按策略触发。

## 技术栈

- **语言**:Python(主体)+ C++/CUDA(sgl-kernel 的 kvcacheio,125+ 文件;JIT 核)。**HiCache 无 Rust**(Rust 仅用于 sglang 的 router/gateway,与 HiCache 无关)。
- **关键依赖**:torch、NCCL/gloo(TP/PP 同步)、zmq(跨实例 kv_events)、各 L3 后端 SDK。
- **构建**:sgl-kernel AOT 编译 CUDA;JIT 核运行时编译;原生 SHA256 经 OpenSSL + AVX2。

## 代码索引

> 沿代码回溯用。符号名稳定锚定,行号会漂移——找不到时 `grep -n "符号名" <文件>`。

| 概念 | 文件:符号 |
|------|-----------|
| 主入口 | `python/sglang/srt/mem_cache/hiradix_cache.py`::`HiRadixCache` (L75) |
| 控制器(DMA/prefetch/backup 线程) | `python/sglang/srt/managers/cache_controller.py`::`HiCacheController` (L202) |
| L3 后端统一抽象 | `python/sglang/srt/mem_cache/hicache_storage.py`::`HiCacheStorage` (L141) |
| 后端工厂(注册+惰性加载) | `python/sglang/srt/mem_cache/storage/backend_factory.py` |
| L2 内存池抽象 | `python/sglang/srt/mem_cache/pool_host/base.py`::`HostKVCache` (L81) |
| L2 MHA 布局实现 | `python/sglang/srt/mem_cache/pool_host/mha.py`::`get_page_buffer_meta` (L535,零拷贝页元数据) |
| radix 树节点(L1/L2/L3 字段) | `python/sglang/srt/mem_cache/radix_cache.py`::`TreeNode` (L217) |
| local match | `hiradix_cache.py`::`match_prefix` (L1557) |
| prefetch from L3 | `hiradix_cache.py`::`prefetch_from_storage` (L1590) |
| prefetch 终止策略 | `hiradix_cache.py`::`can_terminate_prefetch` (L1443) |
| L3 命中查询(实时查后端) | `cache_controller.py`::`_storage_hit_query` (L993) |
| write-back(L1→L2→L3) | `hiradix_cache.py`::`write_backup` (L789) |
| 链式哈希(Merkle-like key) | `python/sglang/srt/mem_cache/cpp_utils/native_hash.py` + `cpp_utils/hash_binding.cpp`::`hash_page` |
| CUDA I/O 核 | `sgl-kernel/csrc/kvcacheio/transfer.cu`(`transfer_kv_per_layer` 等) |
| JIT 核 | `python/sglang/jit_kernel/hicache.py` |
| PD 集成(decode 卸载) | `python/sglang/srt/disaggregation/decode_hicache_mixin.py` |
| 跨实例事件(旁路索引) | `python/sglang/srt/disaggregation/kv_events.py`(`BlockStored`/`BlockRemoved`) |
| 设计文档 | `docs/advanced_features/hicache_design.md` |

## 优势

1. **分层扩容,低成本提命中率** — L2 主机内存 + L3 分布式存储大幅扩容,尤利于多 QA / 长上下文。
2. **L3 跨实例共享** — 链式哈希同 key 的前缀可跨实例命中,提升集群级命中率。
3. **计算与传输重叠** — 三缓冲 `LayerDoneCounter` + 逐层 CUDA event,layer N 计算与 layer N+1 H2D 并行。
4. **零拷贝 L3 I/O** — `page_first` 布局整页连续,传裸指针给后端 RDMA 读写,无中间拷贝。
5. **GPU 辝助 I/O 核** — 自定义 CUDA/JIT 核较 `cudaMemcpyAsync` baseline 最高 3x。
6. **MLA 去重** — MLA 全 rank KV 相同,只 rank 0 回写 L3,省 tp_size 倍带宽与存储。
7. **异构 TP 复用** — `tp_lcm_size` + head split 使不同 TP size 集群共享 L3 KV。
8. **可插拔后端 + 运行时 attach/detach** — 8+ 后端统一接口,HTTP 热切换无需重启。
9. **策略灵活** — prefetch 三策略、write-back 三策略、6 种驱逐策略。
10. **异步流水线** — prefetch/backup 独立线程 + 队列,L3 I/O 与计算/调度解耦。

## 劣势

> 机制层摘要如下。**上游 issue / roadmap 暴露的组合痛点、可修 vs 难消掉的分类、与 lake 对照**见 [pain-points.md](pain-points.md)(调研快照 2026-07-17,submodule `37f94cb7a0`)。

1. **L1/L2 私有,跨实例无法共享** — 跨实例复用必须经 L3,L2 命中不跨实例,冷实例仍需 L3→L2→L1 全程。上游未做全局共享,而是分三类部分缓解(同机去重 #26691/#27370、同机 VMM 共享 #31435/#29326、跨机直传 #21591/#28515),详见 [pain-points.md 1.11](pain-points.md#111-l1l2-实例私有--跨机必经-l3的部分解进行中)。
2. **L3 元数据非强一致** — 实时查后端(`batch_exists`)而非同步元数据,存在窗口期:刚写入的页他实例可能未命中(后端最终一致);`MetadataCache` TTL 缓存可能陈旧。
3. **单实例视角** — HiRadixTree 每实例独立,无全局 radix 视图;跨实例前缀感知依赖外部 kv_events router(旁路)。
4. **链式哈希导致前缀耦合** — L3 key = H(parent‖page),非独立内容寻址;相同 token 串在不同前缀位置 key 不同,中间页驱逐/重算可能断链。
5. **MLA 去重是 rank 0 单点** — 只 rank 0 写 L3,rank 0 故障/慢则 L3 不更新,负载不均(源码有 load balancing todo)。
6. **idle 才能 attach/detach** — 运行时切换后端需严格 idle(无在途请求),生产切换需先排空流量。
7. **host 内存约束硬** — 强制 host pool > device pool;write_back 模式 host 不足时直接丢子树(数据丢失)。
8. **TP 同步开销** — 每个 prefill batch 多次 `all_reduce`(命中数/完成数/terminate/ack),TP 大时累积;序列不一致会死锁。
9. **layout/io 兼容性碎片** — `page_first` 仅配 `kernel`,`page_first_direct` 配 `direct`,ROCm 回退;后端零拷贝支持程度不一。
10. **无反向回传前缀生长** — decode 卸载是被动 write-back/through,新前缀需驱逐或阈值才回写;无主动把 decode 增量实时推给 prefill 集群的机制(依赖 kv_events 旁路 + 调度,非内建)。

## 与本系统的关键对比

| 维度 | SGLang HiCache | 本系统 |
|------|----------------|--------|
| L1/L2 归属 | **私有于实例** | L0-L3 全归存储池统一管理,非实例私有 |
| L3 元数据 | 弱一致,实时查后端 | radix + 位置视图由控制面强一致维护 |
| 内容寻址 | 链式哈希 H(parent‖page),前缀路径寻址 | 纯内容寻址,同内容同 key |
| 反向回传 | 无内建主动生长 | agent 多轮核心:decode 增量回传 → radix 生长 |
| 存算耦合 | 计算实例持有 L1/L2,L3 外挂 | KV 可独立于计算实例调度/迁移 |

详见 [3rdparty-reference.md](../3rdparty-reference.md) 的汇总对比。
