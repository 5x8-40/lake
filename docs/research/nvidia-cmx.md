# NVIDIA CMX（Inference Context Memory Storage）

> 公开材料 + GTC 2026 S81773（DOCA for Storage）+ NIXL `DOCA_MEMOS` 插件。  
> DOCA Memos 的正式 SDK 文档尚未随 DOCA 4.0 公开（LMCache 维护者 2026-06 称要等秋季 SDK）。下面能钉到会话/插件/规范的钉，其余标成定性口径。  
> 不对照其它推理系统或存储方案。

可调公式与图在 [`../../tools/cmx-sim/`](../../tools/cmx-sim/)。

## 一句话

CMX 的核心**不是**「以太网闪存比企业盘快」。核心是：把 KV cache 当成**独立数据类**，用**两颗 BlueField-4 把 I/O 管道切开**，从主机到闪存几乎全程走 **KV 语义**（`store(key, value)` / `retrieve(key)`），**最后一跳**才变成对 SSD 的 block 布局。POSIX、分布式文件系统、对象元数据、多副本、100% 耐久——按 KV 的性质被故意拿掉。

没有这条栈，STX 机柜只是稍近一点的 G4。有这条栈，G3.5 才成立。

## 1. 要解决的是数据路径，不只是容量

长上下文 / 多轮 / agent 把 KV 做成一种**既要快、又要大、又被所有 GPU 同时打满**的东西。企业存储的假设对不上：

1. **拷贝链**。典型路径是 SSD → 存储控制器 → 文件/对象服务 → 主机 CPU 内存 → GPU。每跳都吃延迟，直接打 TTFT。
2. **并发模型**。通用存储按「客户端不会同时打满」做统计复用。推理集群里 GPU 主机是持续、并发、多对多地读写 KV，存储服务器立刻变成集群吞吐上限。
3. **位置绑定**。G3 本地 SSD 能缓冲，但 KV 在哪块盘上，请求就得路由到哪张 GPU。别的节点空着也接不住——因为缓存不在那里。

G1 HBM 装不下；G4 共享文件/对象是为**不可重建**数据准备的（副本、scrub、目录/对象索引）。拿 G4 扛热 KV，等于用耐久服务的功耗和尾延迟，去养一份可以重算的缓存。

CMX 插在中间，产品名也叫 ICMS。层号 **G3.5**：以太网闪存、POD 级共享、专为 ephemeral KV。公开定性口径「每个 GPU POD 数 PB」；GTC 上 DOCA 架构师随口给过一个工作量级 **约 18 PB**（用来说明「别在主机 DRAM 里给每个 KV 留元数据」），不是已公布的 SKU。

## 2. 设计原点：KV 作为数据类的五条性质

GTC S81773 里，DOCA Memos 的设计是从 KV **作为存储对象**的性质往下推的，不是先买盘再找协议。这五条决定了后面所有机制：

| 性质 | 含义 | 对存储栈的后果 |
|------|------|----------------|
| **固定块大小** | 每个模型的 KV block 大小几乎固定（会话里举例：某 DeepSeek 约 800 KB，Llama-70B 约 10 MB） | 按模型建 **KV volume**，预先配 `max KV block size`；不必像文件系统那样同时伺候 1 B 和数 GB 的文件 |
| **写后不可变** | 一块 KV 写完不会改中间几个字节；要换就删了再写 | 没有 in-place update、没有空洞写、没有 POSIX 那种「文件可变」的元数据机 |
| **可重算** | 丢一块 KV 可以用 token 再算 | **故意不做 100% 耐久**。会话原话：100% 保证 vs 比如 99.9%，对存储人来说差别巨大。丢失不该太频繁（否则缓存没意义），但也不做企业盘那套副本/scrub |
| **大 IO、顺序写** | 按 block 粒度整块写 | 写放大压到接近 1，对 SSD 友好，拉寿命、降功耗 |
| **必须共享** | 前缀 KV 要能被 POD 内任意计算节点 retrieve | 路由不再绑「KV 在哪块本地盘」；否则有 GPU 空转、有 GPU 排队，TTFT 被位置绑死 |

这五条合在一起，就是「为什么不在闪存上挂分布式文件系统」。block 协议最省，但 **block 不好共享**；文件系统能共享，但 feature 太重。Memos 走中间：对上是 KV，对下是 block。

## 3. 四层各管一件事

```
推理引擎 (vLLM / SGLang / TRT-LLM)
    │  KV block 的张量布局、page、attention
    ▼
Dynamo
    │  选哪张 GPU、KV 在哪一层、PD 怎么拆（编排，不是闪存）
    ▼
NIXL
    │  异构传输：GPU↔GPU（PD 东西向）和 GPU↔存储（CMX 南北向）同一套 API
    ▼
DOCA Memos（DOCA 4.0 SDK，双端）
    │  主机：模拟出来的 KV 设备（store/retrieve）
    │  计算节点 BF4：隔离、加密、完整性、映射、把请求送到正确的 CMX 柜
    │  CMX 柜 BF4：按约定 layout 落到 SSD（这里才是 block API）
    ▼
Spectrum-X Ethernet（RoCE / RDMA）
    ▼
STX 机柜里的 NVMe 闪存
```

要点：**应用保持无状态**。主机不要维护「每个 KV 在哪、还在不在」的全量索引；位置、路由、介质布局在 DPU / CMX 一侧。Dynamo 仍然要知道 **block 的逻辑身份和层级**（否则没法选路），但那是编排索引，不是给 18 PB 里每一块 KV 在主机 DRAM 里建 inode。

## 4. 核心机制：双端 BlueField 切开 I/O 管道

计算节点和存储柜 **两边都要有 BlueField-4**。会话原话：pipeline 拆到两端，因为各有各该靠近的东西。

```
┌─ 计算柜 ─────────────────────────────────────────┐
│  GPU / 主机 DRAM                                  │
│       │ store/retrieve(key)                       │
│  主机 DOCA Memos 驱动（模拟 KV 设备）              │
│       │                                           │
│  计算节点 BF4 · DOCA Memos                         │
│    · 隔离（会话明确类比 DOCA SNAP：主机看到的     │
│      不是真盘，数据编排在 DPU 上）                 │
│    · 线速加密 / CRC，不吃主机 CPU                  │
│    · KV 映射：这个 key 该去哪台 CMX 柜             │
└─────────────── Spectrum-X RDMA ───────────────────┘
┌─ STX / CMX 柜 ───────────────────────────────────┐
│  存储侧 BF4 · DOCA Memos                           │
│    · 靠近介质的 layout / 放置                      │
│    · 对 SSD 发 block 读写                          │
│  NVMe SSD 组 → 一个 KV volume                      │
└───────────────────────────────────────────────────┘
```

两端 BF4 **不是同一张规格表**：

| 落点 | 公开表述 | 在这条路径上干什么 |
|------|----------|-------------------|
| 计算节点 BF4 | Inside Rubin：64 核 Grace + CX-9，最高 800 Gb/s | 发起端：模拟 KV 设备、隔离、完整性/加密、选 CMX 柜 |
| STX 柜 BF4 | STX 新闻稿：Vera CPU + ConnectX-9 | 终止端：协议落地、闪存控制、block 布局 |

分析时当成 **同一代 DPU 家族的两个角色**（initiator / target），不要合成一颗芯片。

BlueField 上的加速引擎（加密、完整性、RDMA、NVMe-oF）是计算侧 DPU 和存储处理器 **共用的那类能力**。线速加密/CRC 的公开口径是 **800 Gb/s 不吃 Grace 周期**——这是「KV 流可以不做企业副本、但仍要在线上做完整性」的前提：保护在 DPU 硅上，不在主机软件栈里。

DOCA SNAP 是前代：给主机 **模拟一块盘**，租户切换时 DPU 改数据编排、主机仍只看见盘。Memos 把同一思路用在 **模拟 KV 设备** 上：主机看见的是 KV API，不是 POSIX 盘符。

## 5. KV volume：按模型的最大块大小切卷

用法（GTC）：把一组盘做成一个 **KV volume**，带一个 **max KV block size**。

- DeepSeek 类、block ≈ 800 KB → 建上限 1 MB 的 volume，挂给跑这个模型的计算节点。
- Llama-70B 类、block ≈ 10 MB → 另一个 volume、另一组盘。
- 一张计算节点可以同时挂多个 volume（节点上跑多个模型）。

这不是「一个大文件系统大家写」。是 **按 I/O 粒度把闪存切成给固定大小 KV 用的卷**。存储侧最后一跳的 API 是「从这些盘读/写这些 block」；之上三截 API 都是 KV。

NIXL 插件侧能看到对应的设备参数：NVMe-oF 地址、`subnqn`、`ns_id`、`NGUID`、kernel KV device。也就是说，对 NIXL 而言 CMX 呈现为 **NVMe KV 命名空间**，不是 POSIX 路径，也不是随便一个 S3 bucket（虽然 LMCache 把它 **归类** 成 object-style backend，那是因为 NIXL 的 OBJ 代码路径能复用，加上 128-bit 名字约束）。

## 6. 128-bit key：和 NVMe-KV 对上的那一刀

当前 Memos **key 最长 128 bit**。这不是随便选的：

- [NVMe Key Value Command Set](https://nvmexpress.org/specification/key-value-command-set-specification/) 把 **随命令带上的 key 限制在 16 字节**（更长的 key 要另走传 key 机制）。命令是 Store / Retrieve / Delete / Exist / List；value 架构上限到 4 GB；覆盖写要求 KV 级原子。
- NIXL `DOCA_MEMOS`：`docaMemosKey` 就是 **16 字节 buffer + keyLen**；对象名是 **正好 32 个小写 hex 字符**，插件当字符串传入、在设备侧 hex-decode。动态模式下 LMCache 用缓存 key 的 SHA-256 **截断到 128 bit**（名字不透明，128 bit 碰撞是概率事件）。
- GTC 对引擎侧的说法：key **通常是 tokens 的 prefix hash**，value 是要存的 KV 块。

三层不要混成一个哈希：

| 层 | 哈希宽度 | 干什么 |
|----|----------|--------|
| Dynamo `ExternalSequenceBlockHash` | **u64**，带 parent 的链式 block hash | Router 算 overlap、选 worker（`protocols.rs`） |
| DOCA Memos / NVMe-KV | **128 bit** | 闪存上的对象名字 |
| 引擎内部 page hash | 实现相关（如 SGLang L3 用 SHA256 链） | 引擎自己的层间 key |

CMX 不解释张量。它不看 MLA / indexer / SWA。卷的 `max block size` 必须盖住引擎实际写下去的那一块字节。

NIXL 还规定：`DOCA_MEMOS` 的 `nixl_buffer_device` **必须是 cpu**（和 POSIX/Azure 一样，不像 GDS 可以 cuda）。意思是这条存储路径的 bounce buffer 在主机/DPU 能 DMA 的 CPU 侧内存，不是 GPU 直接当 NVMe initiator。GTC 也要求引擎用 **huge page**——KV 块很大，4K 页来回 pin/map 会罚。

## 7. 主机侧三条硬约束（这才是「无状态」的内容）

GTC 点名现有推理框架缺的几块，不是产品口号：

### 7.1 不要在主机攒全量 KV 元数据

CMX 被说成「近乎无限」。若每存一块 KV 就在 DRAM 留一份元数据，容量会先把主机内存吃光。**Exist/位置由 Memos 答**，主机按 key 存取。

这和「Dynamo 完全没有 KV 索引」不是一回事。Router 要的是 **前缀 overlap、层、worker**，粒度是调度用的 block 身份，不是闪存 inode。

### 7.2 禁止 exist-then-retrieve

很多引擎 retrieve 前先 Exist。Memos 的要求是：**直接 retrieve**。

- Retrieve 本身就是 Exist：没有就立刻 miss。
- 更关键：这层 **不是 100% 耐久**。Exist=yes 到 Retrieve 发出去之间，块可能已经没了。多一次 RTT 还制造假阳性。
- NIXL 插件里有 `ignore_read_not_found`、`queryMem` 的 `assume_success` 快路径——和「别把 query 当租约」是同一件事。

### 7.3 必须处理 context hole

旧世界：存储 100% 保证「写下的都还在」，前缀要么连续命中到某点，要么从该点起全没。  
CMX：**中间某一块可能单独缺**。如果把「缺一块」当成「后面全没」，等于没用上这层共享缓存。

正确行为：只重算缺的那些 block，其余从 CMX 拉。GTC 说 vLLM 在做、Dynamo 也在做 **partial KV retrieval**。这是 CMX 语义的一部分，不是引擎边角。

Retrieve 失败时：重算，或去更冷的 G4 找。框架必须把 miss 当成正常控制流。

### 7.4 路由要改

KV 在 CMX volume 里，**任意计算节点都能 retrieve**。路由函数不能再按「本地 SSD 上有这份前缀」把请求钉死在某张 GPU 上。G3 本地命中仍然有价值（少一次出柜），但 G3.5 命中的语义是 **POD 内可调度**，不是「必须去那台机器」。

## 8. NIXL：异构传输 + prestaging

NIXL 不是「CMX 的驱动」。它是推理数据路径的 **统一传输库**：一次请求会同时碰到

- **东西向**：Prefill → Decode 的 GPU↔GPU KV（PD 分离）；
- **南北向**：GPU/主机 ↔ CMX / 本地盘 / 对象。

GTC 的提法：inference data path is **heterogeneous**，NIXL 把这两种搬法收成一套 API。`DOCA_MEMOS` 只是 NIXL 的一个 **storage backend 插件**（`ai-dynamo/nixl` #1717，链 `doca_kv`）：

- 后端标成 **local-only**（不是 NIXL agent 之间的 P2P，是本机对 KV 设备）；
- 内存注册只走 **OBJ_SEG**；
- key 从 metadata 推，hex 派生；
- 进度引擎可 threaded / 非 threaded；
- 有独立的 QUERY 操作，以及读 miss 怎么处理的开关。

**Prestaging** 是性能契约：在 Prefill attend / Decode 用到前，把命中块提前搬进 G2/G1。官方 5× TPS / 5× 能效相对的是 **traditional storage**（为不可重建数据准备的 x86 企业栈），工作负载没公布。没有预取，GPU 就在闪存边上空转，G3.5 退化为稍快的 G4。

5× 叙事的另外一半：KV 不做副本/后台 scrub → 每 token 功耗下降。前提是 **丢块就重算的成本 < 耐久服务的成本**。这不是对所有失败模式都自动成立。

STX 新闻稿另有一条「相对传统 CPU 存储架构 4× 能效、2× 摄入」——那是 STX **存储参考架构** 的声明，不要和 CMX 的 5× TPS 混成一个数。

## 9. 在 Rubin 分层里的位置

Vera Rubin POD ≈ 40 柜、1152 Rubin GPU，五类机柜：NVL72、Groq 3 LPX、Vera CPU、**BlueField-4 STX（CMX 住这）**、Spectrum-6 SPX。计算柜经 Spectrum-X 访问 STX，不是 NVL72 里的本地盘。

| NVIDIA 层 | 介质 | 延迟量级（公开/分析口径） | 这套 1M 负载下干什么 |
|-----------|------|---------------------------|----------------------|
| G1 | 288 GB HBM4 / GPU，22 TB/s | ns | 正在算的 KV |
| G2 | NVL72 柜内 54 TB LPDDR5X | 十到百 ns | 预取缓冲、HBM 溢出 |
| G3 | 节点本地 SSD | µs | 单机暖 KV；跨节点要再搬 |
| **G3.5 CMX** | STX 以太网闪存 | 介于本地 SSD 与企业共享存储之间（无公开 SLA） | POD 内共享的会话 / 前缀 |
| G4 | 对象/文件 | ms 级 | 要留下的冷历史，不在热路径 |

NVL72：72 Rubin + 36 Vera；单柜约 20.7 TB HBM；NVFP4 inf 50 PFLOPS/GPU。时间点：2026 H2 经制造/存储伙伴出货。

NVIDIA **自己的编排**把层写成 Dynamo `StorageTier`（`lib/kv-router/src/protocols.rs`）：

| `StorageTier` | `from_kv_medium` | 和 G 层怎么对齐（NVIDIA 栈内部） |
|---------------|------------------|----------------------------------|
| `Device` | GPU / DEVICE | G1 |
| `HostPinned` | CPU / CPU_PINNED / CPU_TIER1 | G2 |
| `Disk` | CPU_TIER2 / DISK / NVME | G3 本地盘 |
| `External` | EXTERNAL / NETWORK / REMOTE / **SHARED** | G3.5 CMX 和/或 G4——软件枚举 **没有单独的 G3.5** |

Router 对 Disk 和 External 目前用同一档 `disk_cache_hit_weight`（`scheduling/overlap.rs`）。也就是说：**硬件上 G3.5 是新层，控制面枚举还没长出第五档**；CMX 命中在 overlap 计分里先被当成「非本机、非 HBM」的共享层。这是 NVIDIA 公开代码里能看到的衔接缝，不是已完成的一对一映射。

G1/G2 的 page-in/out、抢占，GTC 明确说 **主要仍由推理框架管**。CMX 补的是「框架 HBM/DRAM 管不过来、本地 SSD 又不能共享」那一段。

Spectrum-X 在这条路径上不是普通以太网：自适应路由、拥塞控制、无损 RoCE，用来压 **全 POD 并发 RDMA KV** 的抖动和尾延迟。KV 访问模式会打满统计复用型存储网络；没有可预期的 RDMA 织物，预取窗口就盖不住 attend。

## 10. 公开数字 vs 没公布的

| 有 | 没有（不要当成数据表） |
|----|------------------------|
| NVL72 / HBM4 288 GB @ 22 TB/s / 50 PFLOPS NVFP4；POD ≈ 1152 GPU；CX-9 1.6 Tb/s 端点（部分表）；BF4 800 Gb/s | CMX 每柜闪存 TB、保证带宽/GPU、prestaging SLA、副本因子（设计上接近无副本）、故障后重算预算 |
| 「PB / GPU POD」；GTC 口头 ~18 PB（用来讲元数据） | 5× 的对照负载（模型、ctx、命中、batch） |
| KV volume + max block size；128-bit key；双端 Memos | Memos 的 placement/GC/hot 算法、跨 CMX 柜的具体映射函数 |
| NIXL `DOCA_MEMOS` 插件形态 | 正式 DOCA 4.0 API 手册（尚未公开） |

出柜带宽仿真默认按 CX-9 1.6 Tb/s 单向 ≈ **200 GB/s/GPU**。若采用「0.4 TB/s/GPU」那种机柜汇总，按双向或不同口径理解，不要和 200 GB/s 混用。

## 11. 1M 会话下，CMX 会被打到哪一面

负载：每人一条 ~1M token 会话，前缀命中 90/95/99%，PD 分离。

- **Prefill 跑满**：unique tok/s 由 GEMM+indexer 决定。高命中时新 KV 很小，但 Prefill GPU 仍要把命中前缀从 CMX 拉进来才能 attend → **读主导**。99% @ 1M 时读/写 ≈ 99。这正是 prestaging + 南北向 NIC 的工作点。
- **容量驻留**：活会话超过 G1（再超过 G2）的部分只能在 CMX。Agent 等工具时不占算力，占容量。
- Decode 若整段 KV 已在 HBM，CMX 不参与那一步。`22 TB/s ÷ KV` 是 G1 扫描上限，不是 CMX 带宽模型。
- 中间缺块（§7.3）在 1M、高命中时会被放大：1M 上下文是一长串 block，任意一块 miss 都不能当成后缀全灭。

```
unique = C × (1 − hit) + extra
FLOPs  = 2 × N_active + attn(C)     # V4 indexer 只打 C/4
tok/s  = (peak × util) / FLOPs
write  = tok/s × ΔB/token
read   = req/s × KV(hit × C)
```

## 12. 四模型 KV 口径（仿真里 dtype 可改）

| 模型 | 结构 | BF16 @ 1,048,576 | FP8 @ 1M（常见 serving） |
|------|------|------------------|--------------------------|
| V4-Pro | 30 c4a + 31 c128a，共享 KV | **9.62 GiB** | ~4.8 GiB（FP8+FP4 再小） |
| V4-Flash | 2 SWA + 21 c4a + 20 c128a | 6.72 GiB | ~3.4 GiB |
| GLM-5.2 | 79×MLA 576-d + 22 indexer | 94.4 GiB | 47.3 GiB（引擎 656 页则 53.5） |
| Kimi K3 | 24 MLA + 69 KDA 固定态 | 27.2 GiB | 13.7 GiB |

V4-Pro 9.62 GiB 钉在 vLLM 2026-04-24 附录。不要用「V3.2 的 10%」或 SGLang `DeepSeekV4SingleKVPool.get_bytes_per_token` 的 584 B 未压缩 FP8 页布局代替附录压缩公式。

这些数字决定 **KV volume 的 max block size 和预取字节数**。CMX 本身不读模型结构；引擎切多大块，卷就要按那个粒度建。K3 的 KDA 是每会话固定态，高命中预取必须带完整 KDA snapshot，不能只传 MLA 前缀。

## 代码索引

NIXL 不在本仓库 submodule。沿符号回溯：

| 机制 | 位置 |
|------|------|
| Dynamo 层枚举（无独立 G3.5） | `3rdparty/dynamo/lib/kv-router/src/protocols.rs`::`StorageTier` / `from_kv_medium` |
| 链式 block hash（Router overlap，64-bit） | 同文件 `ExternalSequenceBlockHash` / `LocalBlockHash` |
| Disk vs External 同一档命中权重 | `3rdparty/dynamo/lib/kv-router/src/scheduling/overlap.rs` |
| NIXL DOCA_MEMOS 后端 | `ai-dynamo/nixl` `src/plugins/doca_memos/doca_memos_backend.{h,cpp}`::`nixlDocaMemosEngine` / `docaMemosKey` |
| KV 设备进度与 `doca_kvdev_io_set_conf` | `src/plugins/doca_memos/doca_memos_progress_engine.cpp` |
| 128-bit hex 对象名、cpu bounce | LMCache docs：NIXL `DOCA_MEMOS` backend |
| NVMe-KV 16 B in-command key | NVM Express Key Value Command Set；SPDK `SPDK_NVME_KV_KEY_MAX_LEN = 16` |

## 来源

- [GTC 2026 S81773 — Accelerate AI Inference Using DOCA for Storage](https://www.nvidia.com/en-us/on-demand/session/gtc26-s81773/)（双端 Memos、KV volume、128-bit key、禁止 exist-then-get、context hole、SNAP 类比）
- [NVIDIA CMX 产品页](https://www.nvidia.com/en-us/data-center/ai-storage/cmx/)（DOCA Memos SDK 定义）
- [NVIDIA：BlueField-4-powered CMX](https://developer.nvidia.com/blog/introducing-nvidia-bluefield-4-powered-inference-context-memory-storage-platform-for-the-next-frontier-of-ai/)
- [NVIDIA：Vera Rubin POD](https://developer.nvidia.com/blog/nvidia-vera-rubin-pod-seven-chips-five-rack-scale-systems-one-ai-supercomputer/)
- [NVIDIA：Inside Vera Rubin](https://developer.nvidia.com/blog/inside-the-nvidia-rubin-platform-six-new-chips-one-ai-supercomputer/)
- [NVIDIA Newsroom：STX](https://nvidianews.nvidia.com/news/nvidia-launches-bluefield-4-stx-storage-architecture-with-broad-industry-adoption)
- [NIXL PR #1717 DOCA MEMOS backend](https://github.com/ai-dynamo/nixl/pull/1717)
- [LMCache NIXL / DOCA_MEMOS](https://docs.lmcache.ai/kv_cache/storage_backends/nixl.html)
- [NVM Express Key Value Command Set](https://nvmexpress.org/specification/key-value-command-set-specification/)
- [vLLM DeepSeek V4 附录](https://vllm-project.github.io/2026/04/24/deepseek-v4.html)
- DeepSeek-V4 论文 §4.2.1（arXiv:2606.19348）
