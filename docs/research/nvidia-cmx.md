# NVIDIA CMX（Context Memory Storage；原 ICMS）

> 状态快照：**2026-08-14**。本文把“已经公开实现”“厂商宣布的目标架构”“伙伴自报结果”“本文推导”严格分开。
>
> 核心结论：CMX 是 NVIDIA 公布的一套完整 KV cache 存储**目标架构**，不是一块盘；但公开软件尚未组成可复现的端到端实现。Dynamo 已有通用 KV-aware routing / tiering，NIXL 的 `DOCA_MEMOS` 仍是 open PR，Dynamo 也没有一等 G3.5/CMX 类型。VAST 给出了目前公开材料中较具体的伙伴落法和容量方法，但它的 20× 实验是现有 G3 路径，不是 Rubin + CMX benchmark。

可调公式与校验在 [`../../tools/cmx-sim/`](../../tools/cmx-sim/)；项目成员可通过 private GitLab Pages 直接查看 [`index`](https://lake-13d9f7.gitlab.io/)、[`capacity`](https://lake-13d9f7.gitlab.io/capacity.html) 和 [`economics`](https://lake-13d9f7.gitlab.io/economics.html)。

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

本文不做同类系统横向比较。vLLM、SGLang 和 Dynamo 只用于核对布局、接口和当前代码状态。

## 1. 先看成熟度：完整目标栈，不是完整已发布栈

| 组成 | 公开状态 | 能确认什么 | 不能确认什么 |
|---|---|---|---|
| Rubin GPU、BlueField-4、STX、Spectrum-X | **[NVIDIA 宣布]** | 硬件角色、参考 POD、CMX/G3.5 定位 | 量产 CMX 的持续带宽、延迟、可用容量 |
| Dynamo Router / KVBM / Grove | **[代码]** | 通用 KV-aware routing、G1–G4、offload、静态拓扑部署 | 一等 CMX tier、实时 CMX 柜亲和、完整 prestage 路径 |
| DOCA Memos | **[NVIDIA 宣布] + open PR** | NVIDIA 公布 KV API/双端 BlueField 目标；NIXL PR 实现最长 16-byte key | 稳定公开 SDK、最终错误/恢复/放置契约 |
| NIXL `DOCA_MEMOS` | **open PR** | 插件原型如何调用 `doca_kv`、对象 key、内存注册和 miss 开关 | 已发布/受支持的生产路径 |
| VAST 当前 Dynamo 集成 | **[伙伴声明]** | G3/NFS-oRDMA/GDS 路径及自报测试 | Rubin CMX 性能 |
| VAST G3.5/CMX 集成 | **[伙伴声明：upcoming]** | CNode/DNode/DASE 方向和容量方法 | virtio-fs 与 Memos KV API 最终如何收口 |

最重要的边界是：

- “GPU + Dynamo + NIXL + DOCA Memos + 双端 BlueField-4 + STX”是**目标架构图**。
- “今天可以从公开仓库构建并验证这条完整路径”不成立。
- 任何性能数字必须说明来自哪一段路径，不能把 VAST G3 测试、STX 声明和 Rubin CMX 目标混成一个 benchmark。

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

这是**目标关系**，不是当前开源调用栈的时序图。特别是：

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

CMX 优化的是 KV fast path。它意味着通用 POSIX、对象元数据和企业级耐久服务**不是必需条件**，不意味着所有伙伴实现都必须彻底删除文件、对象、NVMe-oF 或可选数据服务。

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

在已审阅的公开材料里，VAST 给出了较完整的伙伴实现叙事：

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

这说明“读回 KV 可比重算 prompt 快”，不等于 CMX 系统在 Rubin POD 级并发下达到 20×。

**未来 G3.5 [伙伴声明：upcoming]**

- 计算节点 BlueField-4 上运行 CNode；DNode/CMX controller 路径访问共享 NVMe namespace。DNode 与 storage-side BlueField-4 在最终产品中的封装和职责边界尚未公开。
- VAST 公开图出现 virtio-fs；NVIDIA Memos 叙事是 KV API。
- 二者可能并存，也可能分别服务旧路径和新路径；公开材料没有给出最终统一接口。
- 没有 Rubin + CMX 的独立持续带宽、TTFT 或故障恢复 benchmark。

### 5.3 大容量长上下文 sizing

VAST 的价值之一是把“会话保留数”写成容量公式。公开会话/报道使用：

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

**[伙伴声明]** VAST 还讨论了去重/缩减、可选纠删码、加密、多租户、审计和跨热/冷层的数据服务。它们说明伙伴可以在 CMX fast path 外增加企业能力；不能反推“CMX 标准本身已经定义了这些功能”。

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
| CMX 持续 read/write、p99 latency、queue depth | **未知** | 公开材料没有可用于仿真的数据表 |

因此计算器不再默认“200 GB/s/GPU”，也不再把需求超过 NIC 后仍显示的 offered request rate叫作可达吞吐。

## 7. 四个模型的会话状态体积

### 7.1 必须区分三种字节口径

1. **逻辑公式**：模型数学结构需要多少元素。
2. **引擎分配**：page、scale、alignment 后实际分配多少。
3. **CMX 序列化**：最终写入 Memos 的 canonical bytes。

第三种尚未公开。计算器允许选择前两种可核对 profile，并把 payload 估算明确标成 estimate。

### 7.2 1 Mi-token 校验点

以下 `1 Mi-token = 1,048,576 token`。十进制 `1,000,000` 与它相差 4.86%，不能都写成模糊的“1M”。

| 模型/profile | growing KV | paged/window state | 额外续算 state | 可移交合计 |
|---|---:|---:|---:|---:|
| V4-Pro BF16 公式布局 | 9.6172 GiB | 7.6250 MiB SWA | 17.8438 MiB compressor | **9.6421 GiB** |
| V4-Pro vLLM `fp8_ds_mla` + FP8 index | 5.3823 GiB | 4.3486 MiB SWA | 17.8438 MiB compressor | 5.4039 GiB |
| V4-Pro FP8 main + FP4 index payload estimate | 4.9135 GiB | 4.3486 MiB SWA | 17.8438 MiB compressor | 4.9352 GiB |
| V4-Flash BF16 公式布局 | 6.7188 GiB | 5.3750 MiB SWA | 11.6406 MiB compressor | **6.7354 GiB** |
| GLM-5.2 base FP8 logical（78 + 21 indexer） | **46.5820 GiB** | 0 | 0 | 46.5820 GiB |
| GLM-5.2 + MTP FP8 logical（79 + 22） | **47.2734 GiB** | 0 | 0 | 47.2734 GiB |
| GLM-5.2 base vLLM `fp8_ds_mla` | **52.6758 GiB** | 0 | 0 | 52.6758 GiB |
| Kimi K3 BF16 MLA + vLLM KDA state | 27.0000 GiB | 0 | 428.5547 MiB KDA | **27.4185 GiB** |
| Kimi K3 FP8 MLA + vLLM KDA state | 13.5000 GiB | 0 | 428.5547 MiB KDA | **13.9185 GiB** |

对 V4，`paged KV = growing KV + SWA`：V4-Pro BF16 的锚点仍是 **9.6246 GiB**，V4-Flash 是 **6.7240 GiB**。上表“可移交合计”另外加入继续压缩下一批 token 所需的 FP32 compressor residual；把 paged KV 锚点直接当成完整 P→D/CMX 会话状态会少算。

关键修正：

- DeepSeek V4-Pro BF16 的 9.62 GiB 保留为 **paged KV** 硬锚点；完整续算状态是 9.6421 GiB。
- V4 的 FP8 不能简单把 1024 B 除二；vLLM `fp8_ds_mla` main entry 是 584 B。
- vLLM `CompressorStateCache` 用 FP32 保存 C4/C128 residual。SGLang 的状态导出也显式枚举 compressor/indexer-compressor buffers，说明它们不能从会话移交中省略。
- GLM base 是 78 层 + 21 个 indexer；MTP 是可选的第 79 层 + 第 22 个 indexer，不能默认混入。
- Kimi K3 的 vLLM recurrent state 是 FP32，conv state 跟 cache/model dtype；默认 BF16 conv 时固定 state 是 428.5547 MiB，不是 221.55 MiB。
- K3 state 每个前缀 snapshot 只读一次；若写回最终状态，也只写一次，不能把它重复算进 growing KV。

### 7.3 公式锚点

V4-Pro BF16：

```text
swa = 61 layers × 128 entries × 1024 B
growing/token = 30 × (1024 + 256)/4 + 31 × 1024/128
              = 9,848 B/token
paged_KV(1,048,576) = growing + swa
                      = 9.624633789 GiB

C4 compressor/layer
  = [8 × (2 × 2 × 512) + 8 × (2 × 2 × 128)] × 4 B
  = 80 KiB                    # main + indexer, FP32
C128 compressor/layer
  = 128 × (2 × 1 × 512) × 4 B
  = 512 KiB
compressor = 30 × 80 KiB + 31 × 512 KiB
           = 17.84375 MiB
transferable_total = paged_KV + compressor
                   = 9.642059326 GiB
```

同理，V4-Flash 的 compressor residual 是 `21 × 80 KiB + 20 × 512 KiB = 11.640625 MiB`，所以 BF16 可移交总量为 `6.735366821 GiB`。

Kimi K3 的 vLLM KDA state（跨 TP ranks 汇总）：

```text
recurrent/layer = 96 × 128 × 128 × 4 B          # FP32
conv/layer      = (3 × 96 × 128) × (4 − 1) × 2 B
state           = 69 × (recurrent + conv)
                = 428.5546875 MiB
```

## 8. “GPU 跑满”下应该怎么算 CMX 流量

旧版用不完整的模型 FLOPs 公式推 unique token/s，尤其漏掉 Kimi K3 的 query heads 和 QK/AV 两部分，结果不能用于跨模型性能比较。新版要求用户输入实测或明确假设的 `effective_unique_tok/s/GPU`。

定义：

- `C`：请求到来前的既有上下文；
- `E`：本轮新增 token；
- `h`：可复用前缀比例；
- `block`：匹配粒度；
- `M = floor(C × h / block) × block`：真正命中的 token；
- `U = C − M + E`：Prefill 要计算的 unique token；
- `q`：命中 bytes 中确实要从 CMX 远程拉取的比例；
- `a`：新状态写入 CMX 的比例。

```text
read/request
  = q × [growing_KV(M) + prefix_state_snapshot]

write/request
  = a × [growing_KV(C+E) − growing_KV(M) + final_state_snapshot]

compute-offered req/s
  = effective_unique_tok/s/GPU × GPUs / U

required_read_B/s
  = offered_req/s × read/request

required_write_B/s
  = offered_req/s × write/request
```

边界条件不能写成 `Infinity`：当 `U=0`（100% 块对齐命中且 `E=0`）时，`effective_unique_tok/s` 不再约束请求率，因而无法从这个输入推出 `offered_req/s`。此时必须另给外部请求到达率 `λ`；若某方向的 bytes/request 为 0，该方向需求严格为 0，否则带宽需求是未知而非无限。

这套公式刻意拆开：

- prefix hit 与 CMX-resident fraction；
- cache admission 与请求准入；
- growing KV 与固定状态；
- compute-offered load 与 storage-achieved throughput。

只有用户提供可用 transfer budget 时，才计算：

```text
shared-link ceiling = B / (read/request + write/request)
full-duplex ceiling = min(B_rx/read/request, B_tx/write/request)
```

这个 ceiling 仍只是用户链路假设；没有 DPU、fabric、flash、queue 和 tail-latency 数据，就不是完整 CMX 性能模型。

## 9. 同一模型 P/D KV 布局不同怎么办

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

## 10. 真正的创新点与难点

### 10.1 能站住的创新点

1. **把 KV 当成独立数据类。** 允许为 immutable、recomputable、large-I/O cache 裁掉不必要的 durable fast-path 服务。
2. **POD 级共享。** KV 不再因为本地 SSD 位置把请求永久绑到某张 GPU。
3. **双端 DPU 数据路径。** initiator 与 storage target 都靠近网络/介质，主机不承担完整存储数据面。
4. **编排与 I/O 分层。** Dynamo 决定计算/生命周期，NIXL/DOCA 执行搬运和存储。
5. **非 100% 耐久成为上层契约。** miss/hole 是正常控制流，而不是异常兜底。

### 10.2 仍未解决的难点

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

## 11. 软件切口与 DPU/盘侧算法

必须把“公开能力”和“可研究算法”分开。

### 11.1 已有或已宣布

| 能力 | 状态 |
|---|---|
| RDMA/NVMe-oF 终止、inline crypto/integrity | BlueField 已有通用硬件能力 |
| KV API、靠近介质的 metadata/placement/queueing | NVIDIA 宣布的 Memos/STX 目标 |
| DASE 全局 namespace、CNode/DNode 分工 | VAST 伙伴架构声明 |
| Dynamo overlap、offload filter、generic external tier | 当前代码 |

### 11.2 可直接落在软件扩展点的工作

- 给 CMX 独立 tier、命中权重、带宽/延迟预算和失效事件。
- 用 `OffloadFilter` 类扩展点做“值得写入共享层”的 admission。
- 为 prefetch 加 `best_effort / wait_complete / timeout` 语义和 deadline accounting。
- 定义 per-block batch retrieve result，而不是靠 `ignore_read_not_found` 猜测。
- 定义 layout descriptor、canonical serialization 和 P/D handshake。
- 把 storage-side placement、queueing 和 tenant policy 暴露成可观测指标。

### 11.3 不应冒充已公开实现的算法

以下问题合理、也适合在 DPU/storage software 上研究，但 NVIDIA/VAST 没有公开具体算法：

- key 到 enclosure/drive 的一致性映射和故障改向；
- placement/eviction/GC 的数据结构；
- prefetch queue 的 deadline/QoS 调度；
- raw flash 的数据保护策略；
- 物理 NAND packing、FTL、erase-block 共置和 SSD GC。

尤其不能从“按 KV format/layout 写盘”推导出“已实现 prefix-aware NAND packing 或专用 FTL”。公开材料只支持 KV-aware I/O 和靠近介质的服务，未暴露 NAND 算法。

## 12. 尚待厂商回答的问题

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

## 13. Agentic trace、cache 留存与经济边界

### 13.1 匿名 Cursor usage trace

**[用户提供数据 + 推导]** 数据窗口为 `2026-07-20 16:03` 至 `2026-08-14 10:46`（UTC+8），来自单用户 Cursor team usage CSV：

- 553 个 usage events、8 个模型、19 个活跃日、96 个活跃小时；
- `731.0422M` total token，其中 prompt `726.4252M`、output `4.6170M`；
- prompt 中 `670.4458M` 是 Cache Read，`6.4455M` 是 Input with Cache Write，`49.5338M` 是 Input without Cache Write；
- 每行 `Total Tokens = Cache Write + uncached input + Cache Read + Output`，553 行全部一致；
- 296 行有数值 Cost，合计 `$502.65`；243 行为 `-`、14 行为 `Free`，所以该金额不是全部事件的经济价值；
- Cloud Agent ID / Automation ID 均为空，原始用户标识和 CSV 不入仓。

**关键限制：这里的 event 不是单次模型 API 请求。** 单个 event 最高包含 `22.0526M` prompt token，超过相关模型的单请求 context window，证明 Cursor 会把多次底层调用聚合为一个 usage event。因此下面的 input/output 分位数只能描述“每个 Cursor usage event 的聚合量”，不能用来反推单请求 KV 容量、并发数、TTFT 或 provider TTL。

整体 token-weighted cache hit 定义为：

```text
h = Cache Read / (Cache Write + uncached input + Cache Read)
  = 670.4458M / 726.4252M
  = 92.2939%
```

活跃小时 total token 的 `p50/p95/max` 分别为 `4.1143M / 20.9452M / 60.3019M token/h`。最大小时发生在 `2026-08-13 17:00`（UTC+8）：5 个 events、`60.0477M` prompt、`254.2K` output、`56.6650M` Cache Read、数值 Cost `$66.58`。

完整匿名分模型汇总：

| 模型 | events | total token | cache hit | input/event p50 | input/event p95 | output/event p50 | output/event p95 | peak token/h | 数值 Cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Composer 2.5 Fast | 4 | 9.223M | 93.91% | 1.335M | 5.256M | 11.4K | 16.2K | 5.949M | — |
| Cursor Grok 4.5 High Fast | 253 | 209.077M | 95.02% | 445.9K | 2.624M | 1.9K | 18.6K | 18.877M | $73.65 |
| Cursor Grok 4.6 XHigh | 18 | 25.177M | 93.69% | 1.393M | 2.903M | 11.4K | 38.5K | 13.756M | — |
| GPT-5.5 Medium | 85 | 93.311M | 87.34% | 585.5K | 3.688M | 2.6K | 22.7K | 26.474M | $67.28 |
| GPT-5.6 Sol Max | 14 | 104.705M | 92.71% | 6.194M | 19.034M | 38.2K | 67.2K | 60.302M | $124.20 |
| GPT-5.6 Sol Medium | 11 | 6.205M | 80.94% | 84.9K | 1.829M | 1.9K | 7.6K | 2.307M | $10.76 |
| GPT-5.6 Terra Medium | 4 | 3.752M | 86.41% | 803.8K | 1.390M | 6.8K | 7.7K | 1.620M | — |
| Kimi K3 Max | 164 | 279.593M | 91.90% | 584.8K | 7.818M | 5.6K | 50.2K | 21.332M | $226.76 |

这份 trace 能支撑 CMX workload 的三个输入：**单用户 token/h 峰值、token-weighted cache hit、模型 mix**。它不能直接给出 request arrival rate、单请求 context、KV bytes、GPU time 或存储成本；这些仍需底层 request trace 和模型布局。

### 13.2 Provider cache 留存：合同与实测不可混写

Mempko / arXiv:2607.19214 使用 100K prefix 测量 Anthropic Sonnet 4.5、OpenAI GPT-5.1、DeepSeek V3.2、Gemini 2.5 Pro 的空闲存活与 keepalive。其结果适合做 agent pause workload 的**测量样本**，不等于 provider 的统一 TTL 合同：

| Provider | 官方口径（2026-08-14 快照） | 该实验观测 | 本文采用方式 |
|---|---|---|---|
| Anthropic | 默认 5 分钟；可选 1 小时 | 5–6 分钟断崖 | 5 分钟可作为合同；4 分钟 keepalive 是实验策略 |
| OpenAI | 旧模型 in-memory 通常 5–10 分钟、最长 1 小时；另有 extended 24h / 新模型策略 | GPT-5.1 在 20 分钟约一半存活，30 分钟全冷 | 只记为 GPT-5.1/该时点实测，不能概括当前全部 GPT |
| DeepSeek | best-effort；官方称闲置后通常数小时至数天清理 | V3.2 在 10 分钟全冷 | 与官方范围冲突，只能保留为路径实测，不能写成“TTL=10 分钟” |
| Google Gemini | implicit cache 无 TTL 保证；explicit cache 默认 1 小时 | implicit 命中率在 33%–83% 波动 | 说明隐式路由/驻留不可预测，不能外推 explicit cache |

文章给出的 client-side keepalive break-even：

```text
I_max ≈ τ × (w / r − 1)
```

其中 `τ` 是 ping 间隔，`w` 是冷 re-prefill/cache-write 相对成本，`r` 是一次 cache-read ping 相对成本。这个公式用于 API 客户端的“是否续租”，不是 CMX 物理 block TTL；它对 CMX 的价值是生成 pause/resume workload 和 retention-policy 输入。

同样不能把 Dynamo `router_ttl_secs` 当成真实 KV TTL：该字段只在不消费 KV events 的 approximate indexer 中清理 Router 的预测条目，物理 KV 是否仍在 worker/storage 是另一件事。

### 13.3 “每用户 100 GB”已核实，但不是 KV quota

OpenAI Help Center 当前明确写明：ChatGPT Pro 用户拥有 **100 GB File Library storage**。这个额度承载上传文件和 ChatGPT 生成文件；它不是 prompt cache、KV cache、GPU-local cache、API context retention，也不是 OpenAI 给每个用户预留 100 GB KV。

因此本文只把 `100 GB/user` 放在**终端产品存储配额参照**中，不把它代入 CMX sizing。若要把 100 GB 设为 CMX 业务 quota，必须另给：

- 每用户要保留多少会话/前缀；
- 每份 session 的模型、layout、token 长度和共享比例；
- TTL/热度、去重、增量快照、冗余与 usable/raw；
- quota 是 soft、hard 还是可借用。

参考实现上，LMCache `QuotaManager` 提供按 `cache_salt` 动态设置 byte limit 的形态，但没有默认 100 GB，也不证明任何产品级 quota 应该取该值。

### 13.4 “花 5% 换 20% 算力”的可审计公式

先把 token 命中与整机算力分开：

```text
compute_saved
  = prefill_compute_share × cache_hit × avoid_efficiency

net_saved
  = compute_saved − cache_total_cost_share
```

- `prefill_compute_share`：无 cache 基线中 Prefill 占总 GPU 算力/成本的比例；
- `cache_hit`：真正避免重算的 token 或 block 比例；
- `avoid_efficiency`：命中后被 transfer、layout conversion、context hole、重复读等损耗折减后的有效避免率；
- `cache_total_cost_share`：容量、传输、DPU/控制面、keepalive 和运维成本占同一基线的比例。

Cursor trace 的 `h=92.2939%` 只能提供 `cache_hit`。若 `avoid_efficiency=100%`，要达到 `20%` 总算力节省：

```text
required_prefill_compute_share
  = 20% / 92.2939%
  = 21.67%
```

若 Cache 总成本确为同一基线的 `5%`，且总算力成本按比例转化为费用，则：

```text
gross compute saving = 20%
net saving           = 20% − 5% = 15%
gross benefit/cost   = 20% / 5% = 4×
net ROI              = (20% − 5%) / 5% = 3×
```

这回答了“能不能算”：**能算阈值和反事实，不能仅凭 cache hit 宣布真实省了 20% 算力。** 当前 CSV 没有 GPU time、Prefill/Decode 分解或 CMX 成本，257 行 Cost 也不是数值；实际结论必须补模型/硬件实测。可调计算见 [`../../tools/cmx-sim/economics.html`](../../tools/cmx-sim/economics.html)。

## 14. 参考实现与代码回溯

### 14.1 本次直接参考的实现

| 机制 | 代码锚点 | 值得参考的点 | 对 CMX 分析的限制 |
|---|---|---|---|
| Dynamo tier/routing | `3rdparty/dynamo/lib/kv-router/src/protocols.rs::StorageTier`；`scheduling/overlap.rs::cache_hit_weight_for_tier` | 当前层枚举与成本函数的真实形态 | 没有 G3.5；不能把目标架构写成现状 |
| Dynamo shared cache | `scheduling/config.rs::SharedCacheType` | 可核对当前 Router 支持类型 | 只有 `None/Hicache` |
| KVBM offload/admission | `lib/llm/src/block_manager/offload.rs::OffloadManager`；`offload/filter.rs::FrequencyFilter` | 生命周期与写入过滤扩展点 | 当前主状态机不是 CMX 专用 |
| KVBM Event Plane | `lib/llm/src/block_manager/events.rs::{EventManager,DynamoEventManager}`；`kv_consolidator/publisher.rs::KvEventConsolidatorPublisher` | store/remove 事件与聚合发布已有可运行骨架 | 外部 storage advisor/provider schema 仍在演进 |
| NIXL Memos | PR #1717 `nixlDocaMemosEngine::{parseInitParams,registerMem,queryMem,prepXfer}`、`resolveMemosKey`、`doca_memos_progress_engine.cpp::{taskErrorCallback,collectQueryResults}` | key、segment、progress、QUERY/READ miss 的候选 API | open PR，且 READ 无 per-key result，不能作为稳定 partial-prefix 接口 |
| MLA physical layout | `3rdparty/vllm/vllm/v1/kv_cache_interface.py::MLAAttentionSpec.real_page_size_bytes` | DeepSeek V4 `fp8_ds_mla` 为 584 B/entry；非 V4/V3.2 分支为 656 B（本文用于 GLM profile）及 alignment | engine allocation 不等于 CMX serialization |
| V4 continuation state | `3rdparty/vllm/vllm/models/deepseek_v4/compressor.py::CompressorStateCache.get_kv_cache_spec`；SGLang `deepseek_v4_memory_pool.py::{get_state_buf_infos,get_c128_state_buf_infos}` | C4/C128 FP32 residual 的 shape、window 与移交 buffer | 引擎状态合同仍需映射到 CMX object/layout |
| V4 FP8/FP4 payload | `3rdparty/sglang/python/sglang/srt/mem_cache/deepseek_v4_memory_pool.py::{DeepSeekV4SingleKVPool,DeepSeekV4IndexerPool}.get_bytes_per_token` | 584 B main、132/68 B index payload | SGLang page format不是标准交换格式 |
| Kimi K3 state | `3rdparty/vllm/vllm/model_executor/layers/mamba/mamba_utils.py::{MambaStateDtypeCalculator.kda_state_dtype,MambaStateShapeCalculator.kda_state_shape}` | recurrent FP32、conv/state shape | TP 汇总和 checkpoint policy仍要部署定义 |
| Prefetch stop policy | `3rdparty/sglang/python/sglang/srt/mem_cache/hiradix_cache.py::can_terminate_prefetch` | best-effort/wait/timeout 的控制语义 | 只作为软件设计参考，不是 CMX 已实现能力 |
| Dynamo approximate TTL | `3rdparty/dynamo/lib/llm/src/kv_router/indexer/mod.rs::KvIndexer::new`（`PruneConfig.ttl`） | 无事件时用 TTL 清理预测位置的最小形态 | Router metadata TTL，不是物理 KV/provider cache TTL |
| LMCache per-user quota | `3rdparty/lmcache/lmcache/v1/distributed/quota_manager.py::QuotaManager` | `cache_salt → byte limit`、动态 CRUD、下一 eviction cycle 生效 | 没有默认 100 GB；L2 adapter quota 也不是终端产品 File Library |

submodule revisions：

- Dynamo：`f5b1c1cceaee8374e3e6134f43f8aa1a0a225f9c`
- vLLM：`f3e9497e921a16741401c5e93af0c2c29ea74907`
- SGLang：`37f94cb7a0abd2577006c196444786ddfbe9d1e0`

### 14.2 关键差异

- 参考引擎给出的是 HBM page/状态布局；CMX 需要一个跨进程、跨 P/D 的序列化合同，不能直接照搬某个引擎的 allocator。
- Dynamo 当前 generic `External` 能复用控制面骨架，但 CMX 的共享性、延迟、可丢和 hole 语义需要独立建模。
- SGLang 的 prefetch policy 可借鉴终止语义，不能拿来证明 DOCA/NIXL 已实现同样行为。
- API provider 的 cache survival 和 Dynamo Router 的预测 TTL 都不能直接当作 CMX block lifecycle；三者分别属于外部服务观测、控制面 metadata、物理存储状态。
- LMCache 的 byte quota API 形态可借鉴，但 quota 数值必须从 workload/sizing 推导，不能从 ChatGPT File Library 的 100 GB 照搬。

## 15. 来源

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

### 代码、规范与模型

- [NIXL PR #1717 — DOCA MEMOS backend](https://github.com/ai-dynamo/nixl/pull/1717)
- [LMCache NIXL / DOCA_MEMOS](https://docs.lmcache.ai/kv_cache/storage_backends/nixl.html)
- [NVMe Key Value Command Set](https://nvmexpress.org/specification/key-value-command-set-specification/)
- [vLLM DeepSeek V4 appendix](https://vllm-project.github.io/2026/04/24/deepseek-v4.html)
- [DeepSeek-V4 paper](https://arxiv.org/abs/2606.19348)
- [DeepSeek-V4-Pro model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro)
- [DeepSeek-V4-Flash model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash)
- [GLM-5.2 official model article](https://huggingface.co/blog/zai-org/glm-52-blog)
- [Kimi K3 official model card](https://huggingface.co/moonshotai/Kimi-K3)

### Workload、留存与经济

- 用户提供的 Cursor team usage CSV（2026-08-14 导出；本文只入仓匿名聚合，不入仓原始文件和用户标识）
- [Mempko：Your Agentic Workflow's Cache Keepalive Costs 8x Too Much（v2）](https://blog.mempko.com/your-agentic-workflows-cache-keepalive-costs-8x-too-much-v2-the-interval-frontier/)
- [Keeping the Cache Warm Pays: Keepalive Economics for Agentic Workloads（arXiv:2607.19214）](https://arxiv.org/abs/2607.19214)
- [Anthropic Prompt Caching：默认 5m / 可选 1h](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [OpenAI Prompt Caching：in-memory / extended retention](https://developers.openai.com/api/docs/guides/prompt-caching)
- [DeepSeek Context Caching：best-effort、通常数小时至数天清理](https://api-docs.deepseek.com/guides/kv_cache)
- [Google Gemini Context Caching：implicit 与 explicit TTL](https://ai.google.dev/gemini-api/docs/generate-content/caching)
- [OpenAI Help：ChatGPT File Library storage limits](https://help.openai.com/en/articles/20001052-file-storage-and-library-in-chatgpt/)

### 二手材料（只用于会话记录/交叉核对）

- [HPCwire：VAST sizing table](https://www.hpcwire.com/2026/03/02/blasting-through-the-gpu-memory-wall-with-nvidias-new-cmx-platform/)
- [Blocks & Files：partner paths and VAST virtio-fs](https://www.blocksandfiles.com/ai-ml/2026/03/30/nvidia-and-its-partners-kv-cache-extenders/5209284)
- [Glenn Lockwood：CMX / ICMS notes](https://glennklockwood.com/garden/icms)
