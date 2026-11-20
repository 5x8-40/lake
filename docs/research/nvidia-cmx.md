# NVIDIA CMX（Context Memory Storage；原 ICMS）

> 状态快照：**2026-08-14**。证据标签区分公开代码、厂商公告、伙伴声明、推导和未知项。
>
> CMX 是 NVIDIA 公布的 KV cache 存储目标架构，覆盖 GPU、Dynamo/KVBM、NIXL、DOCA Memos、BlueField-4、Spectrum-X 和共享 flash。公开软件尚未形成可复现的端到端实现。lake 只把共享 flash 数据路径映射为候选 L2 backend。

模型字节、容量、Prefill KV 加载带宽、90%/95% 命中率对比和用户提供的 APC 实测拟合在 [`../../tools/cmx-sim/`](../../tools/cmx-sim/)；Agentic trace 与 provider cache 留存见 [`agentic-cache-workload.md`](agentic-cache-workload.md)。

后续工作见 [#22 可移交 Context 合同和数据面能力](https://gitlab.com/BeeBreeze/lake/-/issues/22) 与 [#23 Portable Context ABI](https://gitlab.com/BeeBreeze/lake/-/issues/23)。MR !7 不修改核心 proto/runtime。

## 0. 证据标签与范围

本文使用六类标签：

| 标签 | 含义 |
|---|---|
| **[代码]** | 已进入当前公开源码，能定位到文件和符号 |
| **[NVIDIA 宣布]** | NVIDIA 产品页、技术博客或 GTC 会话描述的目标能力 |
| **[伙伴声明]** | VAST 等伙伴自报架构、测试或产品计划，未独立复现 |
| **[推导]** | 从公开参数直接算术推导 |
| **[分析]** | 为补齐契约而提出的设计判断，不代表产品现状 |
| **[未知]** | 公开材料没有给出实现、协议或性能数据 |

vLLM、SGLang 和 Dynamo 用于核对布局、接口和公开代码状态。

## 1. 公开成熟度

| 组成 | 公开状态 | 能确认什么 | 不能确认什么 |
|---|---|---|---|
| Rubin GPU、BlueField-4、STX、Spectrum-X | **[NVIDIA 宣布]** | 硬件角色、参考 POD、CMX/G3.5 定位 | 量产 CMX 的持续带宽、延迟、可用容量 |
| Dynamo Router / KVBM / Grove | **[代码]** | 通用 KV-aware routing、G1–G4、offload、静态拓扑部署 | 一等 CMX tier、实时 CMX 柜亲和、完整 prestage 路径 |
| DOCA Memos | **[NVIDIA 宣布] + open PR** | NVIDIA 公布 KV API/双端 BlueField 目标；NIXL PR 实现最长 16-byte key | 稳定公开 SDK、最终错误/恢复/放置契约 |
| NIXL `DOCA_MEMOS` | **open PR** | 插件原型如何调用 `doca_kv`、对象 key、内存注册和 miss 开关 | 已发布/受支持的生产路径 |
| VAST 当前 Dynamo 集成 | **[伙伴声明]** | G3/NFS-oRDMA/GDS 路径及自报测试 | Rubin CMX 性能 |
| VAST G3.5/CMX 集成 | **[伙伴声明：upcoming]** | CNode/DNode/DASE 方向和容量方法 | virtio-fs 与 Memos KV API 最终如何收口 |

上述组件不能从公开仓库拼成完整 CMX 路径。性能数字必须注明数据路径，不能混用 VAST G3 测试、STX 声明和 Rubin CMX 目标。

## 2. 目标架构与组件职责

### 2.1 目标数据路径

```text
request
  │
  ▼
Dynamo Frontend / Router
  │  request-time: overlap、load、P/D worker 选择
  ▼
Prefill / Decode engine on GPU
  │  解释 tensor/page/layout；消费 G1 KV
  ▼
Dynamo KVBM + transfer orchestration
  │  block lifecycle、tier visibility、offload/onboard/prestage
  ▼
NIXL
  │  统一 GPU↔GPU 与 memory↔storage 的传输抽象
  ▼
DOCA Memos initiator on compute-side BlueField-4
  │  隔离、inline services、发起远端 KV I/O
  ▼
Spectrum-X / RoCE
  ▼
DOCA Memos target on storage-side BlueField-4
  │  终止协议、靠近介质的 KV I/O
  ▼
STX / CMX enclosure + NVMe flash
```

这是目标关系，不是当前开源调用时序：

- Grove 当前公开能力是 Kubernetes workload 的拓扑感知放置，不是从 KVBM 实时位置图做逐请求 CMX 柜亲和。
- 官方材料同时把 recall/pre-staging 归给 KV manager 和存储处理器；最终“谁决定、谁排队、谁执行”的边界仍未公开。
- 当前 NIXL Memos 路径和 VAST 当前 GDS/virtio-fs 路径是两个集成候选，不能画成同一条已经打通的 zero-copy 路径。

### 2.2 责任矩阵

| 问题 | 目标责任方 | 当前公开证据 |
|---|---|---|
| 请求去哪张 GPU，P/D 怎么选 | Dynamo Router / serving control plane | `StorageTier` overlap 权重已有；无 CMX 专用类型 |
| worker 放在哪个拓扑域 | Grove | 有静态拓扑部署；无 CMX live-affinity 证据 |
| block 在哪一层、何时移动 | KVBM / engine | G1–G4 设计和 offload 已有；G3.5 状态机未见 |
| 字节如何跨 GPU、内存和存储 | NIXL | 通用 transfer API 已有；Memos 插件未合并 |
| KV key 如何变成存储 I/O | DOCA Memos / storage software | 目标 API 已宣布；稳定公开契约不足 |
| value 中的 tensor/page/layout | 推理引擎 | CMX 只存 opaque bytes |
| key 到柜/盘、介质队列、数据保护 | BlueField 上的软件 / 存储伙伴 | 功能方向已宣布，算法未公开 |

CMX 的 fast path 不要求完整的 POSIX、对象元数据和企业耐久服务；伙伴实现仍可保留文件、对象、NVMe-oF 或其他数据服务。

## 3. Dynamo 在 CMX 中的角色

### 3.1 当前代码能确认的部分

以 submodule revision `f5b1c1cceaee8374e3e6134f43f8aa1a0a225f9c` 为准：

1. **Router 的层枚举没有 G3.5。**
   `lib/kv-router/src/protocols.rs::StorageTier` 只有 `Device | HostPinned | Disk | External`。共享/网络/远端介质被归入泛化的 `External`。

2. **Disk 与 External 当前使用相同命中权重。**
   `lib/kv-router/src/scheduling/overlap.rs::cache_hit_weight_for_tier` 把二者都映射到 `disk_cache_hit_weight`。所以公开 Router 不能表达“本地盘、CMX 共享闪存、远端对象”三个不同成本。

3. **共享缓存类型没有 CMX。**
   `lib/kv-router/src/scheduling/config.rs::SharedCacheType` 只有 `None | Hicache`。

4. **KVBM/Block Manager 已有分层骨架。**
   `lib/llm/src/block_manager/offload.rs::OffloadManager` 管 offload/onboard；`filter.rs::FrequencyFilter` 是可复用的写入过滤点；`storage/object.rs::ObjectStorage` 把 G4 对象当 opaque region，但 key 仍是 `u64`。

5. **Event Plane 已有代码骨架，但外部 provider/advisor 合同仍在演进。**
   `lib/llm/src/block_manager/events.rs::{EventManager,DynamoEventManager}` 已把 block store/remove 事件送入 consolidator，`kv_consolidator/publisher.rs::KvEventConsolidatorPublisher` 也能发布聚合视图；这不是“只有概念”。但 `docs/design-docs/kvbm-design.md` 中由外部 storage provider/advisor 消费事件、反向驱动 placement 的接口仍在 finalization，示例 `StoreEvent` 不能当成稳定公开 schema。

### 3.2 目标上 Dynamo 要完成什么

**[NVIDIA 宣布]** CMX 目标里，Dynamo 负责：

- 根据可复用 KV、worker load 和拓扑做请求级 placement。
- 管理 G1/G2/G3/G3.5/G4 之间的 block 生命周期。
- 在消费前协调 onboard/prestage。
- 用 NIXL 发起实际传输。

但当前代码缺口意味着至少还要补：

- CMX 独立成本/延迟模型，而不是复用 `disk_cache_hit_weight`。
- CMX 可见性和失效语义。
- 从 shared tier 到 G2/G1 的明确状态转换。
- per-block miss/hole 结果回传。
- Router、KVBM、storage provider 之间可版本化的 key/layout contract。

### 3.3 Dynamo 不负责什么

- 不解释 opaque value 中的 MLA/SWA/KDA/page layout。
- 不执行 NVMe-KV、flash FTL 或 inline crypto。
- 不应在主机为 PB 级闪存维护 inode 式 per-object 物理目录。
- 不应把 `retrieve not found` 当成系统级错误；缓存可丢，恢复由引擎/控制面完成。

## 4. DOCA、NIXL 与双端 BlueField-4

### 4.1 DOCA Memos 的目标角色

**[NVIDIA 宣布]** DOCA Memos 把 KV 作为独立数据类：

- block 大而且对某个部署布局近似固定；
- 写后不可变；
- 丢失可重算；
- 需要 POD 级共享；
- fast path 不要求企业存储的全套耐久服务。

目标上，计算侧 BlueField-4 是 initiator，存储侧 BlueField-4 是 target。DPU 适合承载 RDMA/NVMe-oF 终止、隔离、inline crypto/integrity 和靠近介质的队列；GPU/引擎仍负责 tensor 语义。

“双端 BlueField”描述的是角色分工，不保证两端是完全相同的 SKU，也不证明所有伙伴都采用同一软件路径。

### 4.2 NIXL `DOCA_MEMOS` 的真实状态

截至 2026-08-13，[NIXL PR #1717](https://github.com/ai-dynamo/nixl/pull/1717) 仍是 **open**（页面最后更新 2026-08-05），不能写成 released backend。

PR 中可以确认：

- 可选链接 `doca_kv`；
- 有 `nixlDocaMemosEngine`、progress engine、显式 QUERY 和 read-not-found 处理；
- 支持 DRAM/object segment 的注册和本机内存到远端 KV object 的传输；
- Memos key 是**最长 16 字节**，不是强制恰好 128 bit；
- 32 hex 字符是某些上层集成的命名约定，不是 Memos 的通用唯一格式。

当前 LMCache/NIXL 公开用法仍使用 CPU/host memory staging。它不能支持“GPU 到 flash 端到端 zero-copy 已完成”的结论。VAST 当前 GDS 路径可以是 GPU-direct，但那是另一条数据路径。

### 4.3 key、miss 与 context hole

三种身份目前没有公开统一合同：

| 层 | 当前公开形态 | 用途 |
|---|---|---|
| Dynamo Router | `ExternalSequenceBlockHash`，链式 `u64` | overlap / routing |
| KVBM G4 object | `u64` | NIXL OBJ region |
| DOCA Memos | 最长 16-byte key | KV storage object |

**[未知]** 谁把 model revision、prefix、layout、rank/group 和 Router 身份稳定映射到 16 字节，公开材料没有钉死。

官方会话要求应用：

- 不要把 `exist` 当租约；
- retrieve miss 是正常控制流；
- 能处理 context hole，而不是一块缺失就丢弃后续所有命中。

但 PR 的 `ignore_read_not_found=true` 只会抑制 READ miss 的整体失败，**不会提供缺块 bitmap，也不会保证 miss 对应的目标 buffer 内容有效**；当前 batched READ completion 也没有逐 key 状态可供上层“追踪每个 retrieve”。配置 `query_mem_mode=actual` 后，显式 QUERY 可对 found key 返回参数、对 absent key 返回空结果；它不是同一批 READ 的 partial-result 合同，QUERY backend error 仍会让整次查询失败。

所以今天要可靠处理 context hole，只能选择较贵的显式 QUERY→READ、拆成单 key READ，或扩展插件/API 返回 per-key found/missing/error；仅打开 `ignore_read_not_found` 不足以只重算缺块。

## 5. VAST 的伙伴落法

### 5.1 VAST 提供的不是新 DPU，而是存储软件

VAST 公开材料给出了以下组件分工：

| 名称 | 位置/角色 | 可确认的职责 |
|---|---|---|
| CNode | 计算/客户端侧 data service，目标可下沉到 BlueField | 全局命名空间、数据服务、元数据和 I/O 控制 |
| DNode | 靠近 SSD/JBOF | 暴露/路由 drive，承载 NVMe-oF 数据路径 |
| DASE | Disaggregated Shared-Everything | CNode 可访问共享 SSD namespace，减少传统存储头和东西向协调 |

不要把 DNode 写成全局 placement policy engine；VAST 的数据服务和元数据逻辑主要在 CNode。也不要把 CNode-X 和“跑在 GPU 服务器 BlueField 上的 CNode”混为一谈：CNode-X 是另一款带 GPU 的 VAST AI OS 服务器。

### 5.2 当前 G3 结果与未来 G3.5 必须拆开

**当前/已展示路径 [伙伴声明]**

- VAST 被 Dynamo 当作 G3/G4 类存储接入。
- VAST 的产品路径支持 NFS-oRDMA/GDS 等现有接口；但下述公开 benchmark 明确使用 NFS/TCP，不能把产品能力写成该次测试配置。
- 自报 benchmark：Llama 3.1 405B、约 128K context、8× Hopper、2×100 Gb/s；约 62 s 重算与约 3–3.5 s retrieve 对比，由此宣传约 20× TTFT、约 90% GPU compute time avoided。
- `1.4×` KV data reduction 是同一篇伙伴文章里的另一项声明，没有公开该缩减测试的 workload，不能归入上面的 Llama/NFS benchmark。

该 20× 结果只适用于上述 G3 测试配置。

**未来 G3.5 [伙伴声明：upcoming]**

- 计算节点 BlueField-4 上运行 CNode；DNode/CMX controller 路径访问共享 NVMe namespace。DNode 与 storage-side BlueField-4 在最终产品中的封装和职责边界尚未公开。
- VAST 公开图出现 virtio-fs；NVIDIA Memos 叙事是 KV API。
- 二者可能并存，也可能分别服务旧路径和新路径；公开材料没有给出最终统一接口。
- 没有 Rubin + CMX 的独立持续带宽、TTFT 或故障恢复 benchmark。

### 5.3 大容量长上下文 sizing

VAST 公开材料使用以下会话容量公式：

```text
users = 10,000
KV per retained session = 32 GB   # planning knob，不是某个模型的精确公式
capacity = users × 32 GB × sessions retained per user
```

这里沿用原材料的十进制 `GB/TB/PB`，且假设每个用户的每份 retained session 都独立占满 32 GB；若有共享前缀、去重或增量快照，物理容量会不同。

| 目标 | 每用户保留会话 | 容量 [伙伴/二手会话记录] |
|---|---:|---:|
| Instant resume | 1 | 320 TB |
| Multi-turn | 5–15 | 1.6–4.8 PB |
| Agentic memory | 150 | 48 PB |

这张表是业务保留策略，不是 CMX SKU。`48 PB` 的 scope 是 10,000 用户 × 150 份会话/用户，不是一个 Rubin POD 的标称容量；它也没有计入格式化/可用容量、冗余、metadata、版本和去重。

另一个独立的发布会算术是：

```text
4 BlueField-4/enclosure × 150 TB = 600 TB/enclosure
16 TB/GPU × 72 GPUs/rack = 1.152 PB/rack
16 TB/GPU × 1,152 GPUs/POD = 18.432 PB/POD
```

这两组数字可以做数量级核对，不能当作已验证的 raw、formatted 或 usable 容量，也不是某个 VAST 型号保证。`1.152 PB/rack`、`18.432 PB/POD` 与上面的 10,000-user retention 表是不同 scope，不能相加或互换。

### 5.4 VAST 额外提供的服务

**[伙伴声明]** VAST 还讨论了去重/缩减、可选纠删码、加密、多租户、审计和冷热层服务。这些是伙伴能力，不是 CMX 标准能力。

## 6. 数字证据账本

| 数字 | 证据等级 | 正确读法 |
|---|---|---|
| “up to 5× throughput / power efficiency” | NVIDIA vendor claim | 相对 traditional storage；未公开可复现实验配置 |
| STX “4× efficiency / 2× ingest” | NVIDIA vendor claim | STX 存储架构声明，与上面的 5× 不是同一测试 |
| VAST 20× TTFT / 90% compute avoided | 伙伴自报实验 | Hopper + 200G + 当前 G3 路径；不是 CMX SLA |
| VAST 1.4× KV data reduction | 伙伴自报实验 | 当前 KV workload 的数据缩减结果；未独立复现，不是 Memos 默认能力 |
| VAST “70% power/footprint reduction” | 2024 BlueField-3 伙伴声明 | 相对其原 x86-backed VAST infrastructure；同文称端到端系统净节能超过 5%。这不是 CMX/BF4 benchmark |
| 600 TB/enclosure、16 TB/GPU、1.152 PB/rack、18.432 PB/POD | 发布会参数 + 推导 | reference-capacity arithmetic；材料未把它定义成 raw、formatted 或 usable SKU |
| ConnectX-9 1.6 Tb/s | endpoint peak | 不能直接当成每 GPU 独占、持续的 CMX 200 GB/s |
| DeepSeek V4-Pro BF16 9.62 GiB | vLLM blog + 代码核对 | `1,048,576 token` 的 paged KV 为 9.6246 GiB，含 7.625 MiB SWA；不含 compressor、engine page 或 CMX wire |
| CMX 持续 read/write、p99 latency、queue depth | **未知** | 公开材料没有可用于仿真的数据表 |

计算器不预设“200 GB/s/GPU”。req/s 表示访问请求吞吐，GB/s 只表示 Prefill 侧 Pool KV 加载需求，不代表可达吞吐。字节公式和校验点以 [`../../tools/cmx-sim/README.md`](../../tools/cmx-sim/README.md) 为准。

## 7. 同一模型 P/D KV 布局不同怎么办

CMX 不解释 value。相同 prefix hash 对应不同布局时，retrieve 甚至可能“成功”但产生静默错误。

会变化的维度包括：

- block/page size；
- BF16、FP8、`fp8_ds_mla`、FP4 payload；
- layer-first/page-first、跨层打包；
- TP/CP 分片、复制和 rank 顺序；
- MLA/indexer/SWA/KDA state 的组合；
- engine 和 layout version。

可行策略：

| 策略 | 效果 | 代价 |
|---|---|---|
| P/D 强制同一 `KVCacheSpec` | 原样复用 | 限制部署拓扑 |
| key 绑定 layout identity | 不同布局不会串读 | 跨布局不复用 |
| CMX 存 canonical layout，边界转换 | 支持异构 P/D | conversion CPU/GPU 成本 |
| 两个 volume / 两份对象 | 隔离明确 | 容量和写流量增加 |

**[分析] 推荐合同：**

```text
layout_id = H(
  model_id, revision, engine_format_version,
  dtype, block_size, page_layout,
  tp/cp partition, kv_group schema, recurrent_state schema
)

object_key = H(namespace, layout_id, parent_prefix_hash, block_ordinal)[0:16]
```

P/D 建链时先交换 `layout_id`：

- 相同：原样传；
- 不同但有 converter：转换；
- 不同且无 converter：明确 miss/拒绝共享，绝不能用同 key 读错 bytes。

布局 descriptor 应按 volume/version 保存，不应在主机为每个 object 复制一份大元数据。

## 8. 架构价值与公开缺口

### 8.1 架构价值

1. KV 成为 immutable、recomputable、large-I/O 的独立数据类，fast path 可裁掉不需要的 durable services。
2. POD 级共享解除本地 SSD 对请求和 GPU 的长期绑定。
3. 双端 DPU 将 initiator 和 storage target 放到网络与介质侧。
4. Dynamo 管编排和生命周期，NIXL/DOCA 管数据移动与存储。
5. miss/hole 成为上层正常控制流，而非系统异常。

### 8.2 未公开的关键能力

| 难点 | 当前缺口 |
|---|---|
| Prestaging deadline | 没有公开持续带宽、p99 latency 或 overlap SLA |
| POD 并发与 tail latency | endpoint peak 不能替代 fabric/DPU/flash queue 模型 |
| 一等 G3.5 控制面 | Dynamo 仍把它折叠进 generic External |
| key 与 layout versioning | u64 routing identity 到 16-byte storage key 的合同未知 |
| context hole | 当前插件开关不提供 partial-result bitmap |
| host staging | 公开 NIXL/LMCache 路径仍有 CPU buffer |
| 双 DPU 故障域 | retry、幂等、重算预算和 failover 未公开 |
| raw/usable capacity | 冗余、reserve、GC、metadata、fragmentation 未公开 |
| 功耗账 | 省掉 durable services，但增加 DPU 基线和 miss 重算；无公开 workload 无法闭合 |

## 9. 软件边界

### 9.1 已有或已宣布

| 能力 | 状态 |
|---|---|
| RDMA/NVMe-oF 终止、inline crypto/integrity | BlueField 已有通用硬件能力 |
| KV API、靠近介质的 metadata/placement/queueing | NVIDIA 宣布的 Memos/STX 目标 |
| DASE 全局 namespace、CNode/DNode 分工 | VAST 伙伴架构声明 |
| Dynamo overlap、offload filter、generic external tier | 当前代码 |

### 9.2 可实现的扩展

- NVIDIA/Dynamo 目标栈可增加 CMX serving/cost class；lake 仅增加 L2 backend profile 和 path capability。
- 用 `OffloadFilter` 类扩展点控制 shared-tier admission。
- 为 prefetch 加 `best_effort / wait_complete / timeout` 语义和 deadline accounting。
- 定义 per-block batch retrieve result，而不是靠 `ignore_read_not_found` 猜测。
- 定义 layout descriptor、canonical serialization 和 P/D handshake。
- 把 storage-side placement、queueing 和 tenant policy 暴露成可观测指标。

### 9.3 尚未公开的算法

NVIDIA/VAST 没有公开以下实现：

- key 到 enclosure/drive 的一致性映射和故障改向；
- placement/eviction/GC 的数据结构；
- prefetch queue 的 deadline/QoS 调度；
- raw flash 的数据保护策略；
- 物理 NAND packing、FTL、erase-block 共置和 SSD GC。

“按 KV format/layout 写盘”不能证明已实现 prefix-aware NAND packing 或专用 FTL。

### 9.4 与 lake 的关系

CMX 是 GPU 到共享 flash 的目标栈；lake 是 Pool 管理全部状态的推理系统。CMX 在 lake 中对应 L2 backend capability，不是第五个介质层。

lake 不固化 CNIC/SNIC 双网拓扑。NIXL、Memos、BF4 和其他数据路径通过 endpoint、transport、backend、path capability 描述；位置仍由 Rust control plane 单点权威管理。

| 维度 | CMX 目标栈 | lake | 采用方式 |
|---|---|---|---|
| 范围 | GPU、编排、传输、DPU、fabric、介质 | 推理执行 + Pool 权威 | CMX 可成为 backend，不是 lake 整体替代品 |
| 位置/生命周期 | Dynamo/KVBM 目标上管理 G1–G4/G3.5 | Rust CP 单一位置权威，pool-owned L0–L3 | 部署必须选一个权威；不能双写 |
| HBM 所有权 | KVBM 当前仍从 engine-owned G1 起步 | HBM 也是 Pool 物理载体 | 不照搬 engine-private cache |
| 模式 | CMX 解决共享 Context I/O | Router 逐请求选 PD/混部/D-direct | 不建立 mode fallback chain |
| failure | Memos/DPU/backend 细节未公开 | F4 失败后按最新状态重跑 Router | backend error 上报 F4，不在 worker 降级 |
| overload | Dynamo/存储 queue 能暴露信号 | gateway 管准入/shedding | Pool 只回报容量/背压 |

lake 需要补充：

- Context API 的逐 block/component `found | missing | error`；
- 可选 DPU endpoint，以及注册、integrity 和进度引擎能力；
- 拓扑、带宽、延迟和 deadline-aware prestaging 信号；D-direct 选路总预算仍须 `<5ms`；
- Router、Pool authority 与 transfer backend 的明确分层。

以下内容不进入 lake 合同：

- 把 `External/G3.5` 同时当介质、位置和产品层；
- 第二套位置权威；
- 将某个引擎的 HBM layout 设为通用 wire ABI；
- 将伙伴 benchmark、端点峰值或页面默认值当作 CMX SLA。

### 9.5 后续工作

- [#22：可移交 Context 合同和数据面能力](https://gitlab.com/BeeBreeze/lake/-/issues/22)：定义 identity、representation、component、partial result、P/D handshake、object key 和 capability。
- [#23：Portable Context ABI](https://gitlab.com/BeeBreeze/lake/-/issues/23)：孵化跨引擎 schema、bindings、adapters 和 conformance。
- 通用 Context Store 暂缓，需先证明相对现有方案有至少 20% 的可复现收益。
- DPU/NVMe target 依赖真实硬件和厂商 API。
- 当前不实现完整 CMX appliance。

## 10. 尚待厂商回答的问题

1. `DOCA_MEMOS` 何时合并、对应哪个公开 DOCA SDK/version？
2. 最终 key 是任意 1–16 bytes，还是某个上层强制 16-byte digest？
3. batched retrieve 如何逐块返回 found/missing/error？
4. Router/KVBM 如何区分 local disk、CMX 和 remote object 的成本？
5. Grove 是否会消费实时 KV/CMX topology，还是只做部署期 placement？
6. Prestaging 的 decision、queue 和 completion 分别归谁？
7. VAST 的 virtio-fs/GDS 与 Memos KV API 是并存、迁移还是分层？
8. CMX 写入是 canonical payload 还是 engine page；谁承担 layout conversion？
9. 一柜/一 POD 的 sustained read/write、p99 latency、failure-domain 和 usable/raw 是多少？
10. 5× 的模型、context、hit rate、batch、对照存储和功耗边界是什么？

Agentic trace、provider cache 留存和 File Library quota 的证据已拆到 [`agentic-cache-workload.md`](agentic-cache-workload.md)，不再与 CMX 产品架构混写。

## 11. 参考实现与代码回溯

### 11.1 本次直接参考的实现

| 机制 | 代码锚点 | 采用内容 | 限制 |
|---|---|---|---|
| Dynamo tier/routing | `3rdparty/dynamo/lib/kv-router/src/protocols.rs::StorageTier`；`scheduling/overlap.rs::cache_hit_weight_for_tier` | 当前层枚举与成本函数的真实形态 | 没有 G3.5；不能把目标架构写成现状 |
| Dynamo shared cache | `scheduling/config.rs::SharedCacheType` | 可核对当前 Router 支持类型 | 只有 `None/Hicache` |
| KVBM offload/admission | `lib/llm/src/block_manager/offload.rs::OffloadManager`；`offload/filter.rs::FrequencyFilter` | 生命周期与写入过滤扩展点 | 当前主状态机不是 CMX 专用 |
| KVBM Event Plane | `lib/llm/src/block_manager/events.rs::{EventManager,DynamoEventManager}`；`kv_consolidator/publisher.rs::KvEventConsolidatorPublisher` | store/remove 事件与聚合发布已有可运行骨架 | 外部 storage advisor/provider schema 仍在演进 |
| NIXL Memos | PR #1717 `nixlDocaMemosEngine::{parseInitParams,registerMem,queryMem,prepXfer}`、`resolveMemosKey`、`doca_memos_progress_engine.cpp::{taskErrorCallback,collectQueryResults}` | key、segment、progress、QUERY/READ miss 的候选 API | open PR，且 READ 无 per-key result，不能作为稳定 partial-prefix 接口 |
| vLLM canonical refs | `distributed/kv_transfer/kv_connector/v1/offloading/worker.py::register_kv_caches`；`CanonicalKVCaches` / `CanonicalKVCacheRef` | tensor、group 和 ref 显式注册，可作为 adapter descriptor 参考 | vLLM 进程内 canonical，不是稳定跨引擎 wire ABI |
| SGLang component/hole | `hicache_storage.py::{PoolTransfer,PoolHitPolicy,batch_exists_v2}` | main/index/SWA/Mamba component 与 `ALL_PAGES/TRAILING_PAGES` 命中语义 | HiCache L1/L2 实例私有，不能照搬为全局权威 |
| LMCache 128-bit key | `lmcache/v1/storage_backend/nixl_storage_backend.py::_format_object_key_b128` | Memos 16-byte/32-hex key adapter 形态 | key 仍从 LMCache 自身 identity hash，未标准化 layout/component |
| Prefetch stop policy | `3rdparty/sglang/python/sglang/srt/mem_cache/hiradix_cache.py::can_terminate_prefetch` | best-effort/wait/timeout 的控制语义 | 只作为软件设计参考，不是 CMX 已实现能力 |

submodule revisions：

- Dynamo：`f5b1c1cceaee8374e3e6134f43f8aa1a0a225f9c`
- vLLM：`f3e9497e921a16741401c5e93af0c2c29ea74907`
- SGLang：`37f94cb7a0abd2577006c196444786ddfbe9d1e0`

### 11.2 使用边界

- 参考引擎给出的是 HBM page/状态布局；CMX 需要一个跨进程、跨 P/D 的序列化合同，不能直接照搬某个引擎的 allocator。
- Dynamo 当前 generic `External` 能复用控制面骨架，但 CMX 的共享性、延迟、可丢和 hole 语义需要独立建模。
- SGLang 的 prefetch policy 可借鉴终止语义，不能拿来证明 DOCA/NIXL 已实现同样行为。

## 12. 来源

### NVIDIA 一手材料

- [NVIDIA CMX 产品页](https://www.nvidia.com/en-us/data-center/ai-storage/cmx/)
- [GTC 2026 S81773 — Accelerate AI Inference Using DOCA for Storage](https://www.nvidia.com/en-us/on-demand/session/gtc26-s81773/)
- [GTC 2026 S82255 — The Physics of Long-Context Inference](https://www.nvidia.com/en-us/on-demand/session/gtc26-s82255/)
- [BlueField-4-powered CMX](https://developer.nvidia.com/blog/introducing-nvidia-bluefield-4-powered-inference-context-memory-storage-platform-for-the-next-frontier-of-ai/)
- [Scaling Agentic AI Factories with BlueField](https://developer.nvidia.com/blog/scaling-agentic-ai-factories-through-extreme-co-design-with-nvidia-bluefield/)
- [Vera Rubin POD](https://developer.nvidia.com/blog/nvidia-vera-rubin-pod-seven-chips-five-rack-scale-systems-one-ai-supercomputer/)
- [Inside Vera Rubin](https://developer.nvidia.com/blog/inside-the-nvidia-rubin-platform-six-new-chips-one-ai-supercomputer/)
- [BlueField-4 STX announcement](https://nvidianews.nvidia.com/news/nvidia-launches-bluefield-4-stx-storage-architecture-with-broad-industry-adoption)

### 伙伴一手材料

- [VAST：How NVIDIA Dynamo and VAST Unlock Context Reuse at Scale](https://www.vastdata.com/blog/how-nvidia-dynamo-vast-unlock-context-reuse-at-scale)
- [VAST Dynamo benchmark guide：Llama-3.1-405B / NFS-TCP / 62 s vs 3–3.5 s](https://github.com/vast-data/dynamo/blob/h100-0.7.0-demo/docs/guides/benchmark_on_vast.md)
- [VAST：More Inference, Less Infrastructure](https://www.vastdata.com/blog/more-inference-less-infrastructure-vast-nvidia)
- [VAST：Right-Sizing KV Cache](https://www.vastdata.com/blog/stop-wasting-gpu-cycles-a-practical-guide-to-right-sizing-kv-cache)
- [VAST：2024 BlueField-3 AI Factory architecture（70% claim 的实际 scope）](https://www.vastdata.com/press-releases/vast-nvidia-bluefield-architecture-for-ai-factory)
- [VAST Forward：CNode-X / CMX cluster configuration](https://www.vastdata.com/press-releases/vast-data-introduces-end-to-end-fully-accelerated-ai-data-stack-with-nvidia)

### 代码与规范

- [NIXL PR #1717 — DOCA MEMOS backend](https://github.com/ai-dynamo/nixl/pull/1717)
- [LMCache NIXL / DOCA_MEMOS](https://docs.lmcache.ai/kv_cache/storage_backends/nixl.html)
- [NVMe Key Value Command Set](https://nvmexpress.org/specification/key-value-command-set-specification/)
- [vLLM DeepSeek V4 blog](https://vllm.ai/blog/2026-04-24-deepseek-v4)

### 二手材料（只用于会话记录/交叉核对）

- [HPCwire：VAST sizing table](https://www.hpcwire.com/2026/03/02/blasting-through-the-gpu-memory-wall-with-nvidias-new-cmx-platform/)
- [Blocks & Files：partner paths and VAST virtio-fs](https://www.blocksandfiles.com/ai-ml/2026/03/30/nvidia-and-its-partners-kv-cache-extenders/5209284)
- [Glenn Lockwood：CMX / ICMS notes](https://glennklockwood.com/garden/icms)
