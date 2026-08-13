# NVIDIA CMX（Inference Context Memory Storage）

> 公开材料 + GTC 2026 S81773（DOCA for Storage）+ GTC 2026 S82255 / VAST Forward「Breaking Through the GPU Memory Wall」（VAST × Dynamo 架构师）+ NIXL `DOCA_MEMOS` 插件 + Dynamo KVBM / Router 源码。  
> DOCA Memos 的正式 SDK 文档尚未随 DOCA 4.0 公开（LMCache 维护者 2026-06 称要等秋季 SDK）。下面能钉到会话/插件/规范/开源树的钉，其余标成定性口径。  
> 不对照其它推理系统。VAST 是 NVIDIA 点名的 CMX 软件落法，单独成节。

可调公式与图在 [`../../tools/cmx-sim/`](../../tools/cmx-sim/)。

## 一句话

CMX **不是**一块盘，也不是「以太网闪存比企业盘快」。它是一套 **KV cache 存储解决方案**：GPU 算、Dynamo 编排、NIXL 搬、DOCA Memos 在双端 BlueField-4 上把 KV I/O 做到闪存、STX/CMX 盘框提供 G3.5 介质、Spectrum-X 提供可预期的 RDMA。

把 KV 当成**独立数据类**之后，从主机到闪存几乎全程走 `store(key, value)` / `retrieve(key)`，**最后一跳**才变成对 SSD 的 block 布局。POSIX、分布式文件系统、对象元数据、多副本、100% 耐久——按 KV 的性质被故意拿掉。

没有 Dynamo，盘框里的 KV 不会被选路、预取、跨 PD 复用。没有 DOCA，主机仍在走文件/块协议，STX 机柜只是稍近一点的 G4。两截都在，G3.5 才成立。

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

## 3. 整条栈：GPU + Dynamo + NIXL + DOCA + DPU + 盘框

NVIDIA 产品页把 CMX 写成 BlueField-4 + DOCA + Spectrum-X 加上 Dynamo。拆开之后，每一截只答一类问题：

```
请求
  │
  ▼
Dynamo Frontend / Router / Grove
  │  这个请求去哪张 GPU？P/D 怎么拆？worker 放哪柜？
  ▼
GPU 上的引擎 (vLLM / SGLang / TRT-LLM)
  │  张量布局、page、attention；G1 HBM 里正在算的 KV
  ▼
Dynamo KVBM
  │  这块 KV 现在该在哪一层？何时 offload / onboard / prestage？
  ▼
NIXL
  │  异构传输：GPU↔GPU（PD 东西向）和 GPU↔存储（CMX 南北向）同一套 API
  ▼
DOCA Memos（DOCA 4.0，跑在双端 BlueField-4 上）
  │  主机看见模拟 KV 设备；计算侧 BF4 映射/加密/完整性；
  │  存储侧 BF4 把最后一跳变成对 SSD 的 block
  ▼
Spectrum-X Ethernet（RoCE / RDMA）
  ▼
STX / CMX 盘框里的 NVMe
```

| 问题 | 谁答 | 谁不答 |
|------|------|--------|
| 这个请求去哪张 GPU？overlap 多少？ | Dynamo **Router** | DOCA / 盘框 |
| Prefill / Decode 怎么拆、worker 放哪柜？ | Dynamo + **Grove**（拓扑感知） | DOCA |
| 这块 KV 现在该在 G1 / G2 / G3 / G3.5 / G4？何时预取进 HBM？ | Dynamo **KVBM** | DOCA 不决定冷热策略 |
| 字节怎么从这张 GPU 搬到那张 GPU / 搬到存储？ | **NIXL** | Dynamo 下指令，自己不发 RDMA |
| `store(key)` / `retrieve(key)` 怎么变成对闪存的 I/O？ | **DOCA Memos** | Dynamo 不讲 NVMe-KV |
| 这个 key 去哪台 CMX 柜？线速加密 / CRC？ | 计算侧 **BF4** 上的 DOCA | 主机 CPU |
| NAND packing、对 SSD 发 block？ | 存储侧 **BF4** 上的 DOCA | Dynamo / 引擎 |
| 算 attention、KV 张量长什么样？ | **GPU + 引擎** | CMX 存不透明字节 |
| 介质、容量、功耗底盘 | **CMX 盘框** + Spectrum-X | 软件栈 |

要点：**应用保持无状态**。主机不要维护「每个 KV 在哪、还在不在」的全量闪存目录；介质布局在 DPU / CMX 一侧。Dynamo 仍然要知道 **block 的逻辑身份和层级**（否则没法选路），但那是编排索引，不是给 18 PB 里每一块 KV 在主机 DRAM 里建 inode。

### 3.1 一次命中请求怎么走完

以「前缀已在 CMX、Prefill 要 attend」为例（GTC + NVIDIA 技术博客的 prestaging 叙事）：

1. **Router** 用链式 block hash（`ExternalSequenceBlockHash`，u64）算 overlap，选 Prefill worker。G3.5 命中的语义是 POD 内可调度，不再把请求钉在「本地 SSD 有这份前缀」的那张 GPU 上。
2. **KVBM** 认定这些 block 在共享层，发 **onboard / prestage**：在 attend 之前把它们搬进 G2 或 G1。博客原话：KV managers prestage KV blocks from CMX into G2 or G1 ahead of decode（Prefill 同理，只是消费点是 attend）。
3. **NIXL** 走 `DOCA_MEMOS` 插件：本机对 KV 设备做 retrieve，bounce buffer **必须在 CPU**（`nixl_buffer_device=cpu`），再进 GPU。
4. **DOCA Memos** 在计算侧 BF4 上把 128-bit key 映射到正确的 CMX 柜，RDMA 过去；存储侧 BF4 按约定 layout 从 SSD 读 block，原样返回字节。
5. **引擎** 把字节解释成自己的 KV 布局，在 GPU 上算。CMX 全程不看 MLA / SWA / dtype。
6. 新算出来的 unique KV：**KVBM** 决定是否写回 CMX；写回再走 3→4 的反方向 `store`。
7. **KV events**（NATS / ZMQ）广播 Store/Remove，给 Router 下一次 overlap 用。这是编排可见性，不是闪存 inode。

Retrieve miss 或中间缺一块（context hole）：DOCA 立刻返回没有；**Dynamo / 引擎**负责只重算缺的，不能把「缺一块」当成「后缀全灭」。这是 CMX 语义的一部分，GTC 说 vLLM 和 Dynamo 都在做 partial KV retrieval。

### 3.2 Dynamo 在这里干什么

产品页原话：Dynamo 让 CMX 和底下那些 context 层在 POD 里**看起来是一层**，把请求送到 KV 已经在的地方；在 serving 层做 KV-aware placement 和 reuse。

拆开是三个平面（Dynamo 自己的 architecture.md）：

| 平面 | 组件 | 在 CMX 路径上的具体工作 |
|------|------|------------------------|
| 请求面 | Frontend、**Router**、Prefill/Decode worker | KV-aware 选 worker；PD 分离后 Decode 经 NIXL 拿 KV；G3.5 命中后路由函数必须改（任意节点可 retrieve） |
| 控制面 | Planner、Operator、**Grove** | Grove 按 KV 局部性把 workload 放到已有 context 的机柜/节点，减少再搬 |
| 状态面 | **KVBM**、KV events、NIXL | block 生命周期（allocate → register → match）；G1↔G2↔G3↔G4 的 offload / onboard；向 NIXL 下 get/put |

KVBM 今天在开源树里的层模型（`lib/kvbm-engine/docs/architecture.md`、`docs/design-docs/kvbm-design.md`）：

| KVBM 层 | 介质 | 传输 | 和 CMX 的关系 |
|---------|------|------|----------------|
| G1 Device | GPU HBM | CUDA | 引擎正在用的 KV |
| G2 Host | pinned DRAM | CUDA / RDMA | 预取缓冲；NIXL `DOCA_MEMOS` 的 bounce 也在 CPU 侧 |
| G3 Disk | 本地 NVMe | POSIX / GDS | 单机暖 KV。VAST 今天接在这一档（Anat：currently integrated at G3 as local storage） |
| G4 Remote | S3 / NFS / 对象 | NIXL OBJ / POSIX | KVBM 把它当 **不透明 blob**（`ObjectStorage` 的 key 还是 **u64**） |
| **G3.5 CMX** | STX 以太网闪存 | NIXL `DOCA_MEMOS` | **开源枚举还没有这一档**。VAST / Dynamo 博客写的是 upcoming releases |

Router 的 `StorageTier`（`lib/kv-router/src/protocols.rs`）只有 `Device | HostPinned | Disk | External`。`from_kv_medium` 把 `SHARED` / `NETWORK` / `REMOTE` 都映射成 `External`。`cache_hit_weight_for_tier` 里 **Disk 和 External 用同一档 `disk_cache_hit_weight`**（`scheduling/overlap.rs`）。硬件上多出来的 G3.5，控制面先当「非本机、非 HBM 的共享层」打分。这是能钉到代码的衔接缝，不是已经一对一映射。

KVBM 对存储后端的契约（kvbm-design，API 仍标 finalized 中）：

- 对 NIXL Storage Agent：`registerVolume` / `get()` / `put()`，不挂主机文件系统。
- 对存储伙伴：Event Plane 发 `StoreEvent` / `RemoveEvent`（`sequence_hash`、`prefix_hash`、`storage_location`），伙伴自己建树、做冷热。**放置策略可以在存储侧**，Dynamo 不指挥盘怎么排。

Dynamo **不做**的事（边界）：

- 不解释张量，不转换 P/D 布局。
- 不在主机给 18 PB 闪存建 per-object inode（那是 DOCA / DPU 的 Exist）。
- 不跑 NVMe-KV 命令、不给 NAND packing。
- 不保证块还在：CMX 不是 100% 耐久，miss / hole 是正常控制流。
- 今天的 OffloadManager 主路径仍是 G1→G2→G3（可 `device_to_disk` 旁路 G2）。G3.5 作为一等目标还没长进这个状态机。

### 3.3 DOCA 在这里干什么

DOCA 是 **BlueField 上的系统软件**。CMX 用到的那一块叫 **DOCA Memos**（DOCA 4.0 SDK）：给推理框架一个 KV 通信/存储层，把以太网闪存变成 POD 级 cache。产品页：暴露简单 KV API；硬件加速完整性和加密；**应用保持无状态**，由 CMX 做 KV 的 routing 和 reuse。

这里的「routing」和 Dynamo Router **不是同一件事**：

| | Dynamo Router | DOCA Memos「KV routing」 |
|--|---------------|-------------------------|
| 输入 | 请求、block hash overlap、worker 负载 | 一个 128-bit key |
| 输出 | 哪张 GPU / 哪个 Prefill 或 Decode worker | 这个对象在哪台 CMX 柜、哪组盘 |
| 状态 | 调度用的 overlap 索引 + KV events | DPU 上的 KV 设备映射，主机不持全量目录 |
| 失败 | 换 worker、重算 | retrieve miss / hole → 立刻告诉上层 |

Memos 在双端 BF4 上切开 I/O 管道（S81773；下一节展开硬件）：

- **主机**：看见模拟出来的 KV 设备（会话类比 DOCA SNAP：主机看到的不是真盘，编排在 DPU 上）。API 是 Store / Retrieve / Delete / Exist / List，对上 NVMe Key Value Command Set（in-command key ≤ 16 B）。
- **计算侧 BF4**：隔离、线速加密/CRC、把 key 映射到正确的 CMX 柜、RDMA 发出去。
- **存储侧 BF4**：按约定 format/layout 把 value 写到 SSD——**这里才出现 block API**。盘上 packing 是 NAND 布局，不是张量布局。
- **存储伙伴**：Memos 提供开放接口，让 VAST 这类软件把 G3.5 做成自己的命名空间（CNode 跑在计算侧 BF4），而不是只能当 G4。

主机侧三条硬约束是 **DOCA 强加给 Dynamo / 引擎的契约**，不是 Dynamo 自己发明的：禁止在主机攒全量 KV 元数据、禁止 exist-then-retrieve、必须处理 context hole。NIXL 插件里的 `ignore_read_not_found`、`queryMem` 的 `assume_success` 快路径，就是在落实「别把 query 当租约」。

DOCA **不做**的事：

- 不选 GPU、不拆 PD、不算 overlap。
- 不决定这块 KV 该升到 HBM 还是降到 G4（那是 KVBM）。
- 不解释 value 里的张量。key 默认是 prefix hash，不含 dtype / TP / page 布局。
- SDK 文档未随 DOCA 4.0 公开前，placement / GC / 跨柜映射函数都还没有可引用的算法。

### 3.4 NIXL 是胶水

NIXL 不是第三套策略。Dynamo / 引擎说「搬这些 block」；NIXL 选后端。CMX 对应的后端是 `DOCA_MEMOS`（`ai-dynamo/nixl` #1717）：

- **local-only**：不是 NIXL agent 之间的 P2P，是本机对 KV 设备。
- key：32 个小写 hex → 设备侧 decode 成 16 字节 `docaMemosKey`。和 Dynamo Router 的 u64 链式 hash、KVBM G4 `ObjectStorage` 的 u64 key **不是同一个名字空间**。谁把调度身份压进 16 字节，公开材料没钉。
- bounce：**必须 CPU**。GDS 可以 cuda 的那条路，这条不走。

东西向（P→D GPU↔GPU）和南北向（GPU↔CMX）收成一套 API，所以 PD 分离和 CMX 预取能在同一个 transfer 层里编排。细节见 §8。

### 3.5 GPU、DPU、盘框、织物

| 件 | 在 CMX 方案里的角色 |
|----|---------------------|
| **GPU（Rubin）** | G1：正在算的 KV + attention。288 GB HBM4 @ 22 TB/s。引擎管 page-in/out 和张量布局。CMX 省的是「HBM 装不下 / 本地盘不能共享」那一段，不是代替 HBM 扫描。 |
| **计算节点 BF4** | DOCA Memos 的 initiator。64 核 Grace + CX-9，公开口径最高 800 Gb/s。主机 CPU 不进 KV 热路径。 |
| **存储侧 BF4** | STX 柜里的 target：Vera CPU + ConnectX-9。协议落地、闪存控制。 |
| **CMX / STX 盘框** | G3.5 介质。Jensen CES：4×BF4 / 柜、150 TB / BF4 → **600 TB/柜**；均摊 **16 TB/GPU** → 1152 GPU ≈ **18.4 PB/POD**。AIC F2032-G6（32×E3.S）对得上这个外形；Quanta/Supermicro 展过 24 盘 STX。 |
| **Spectrum-X** | 计算柜到 STX 的南北向 RDMA。自适应路由、拥塞控制、无损 RoCE。KV 会打满统计复用型网络；没有可预期织物，§3.1 的预取窗口盖不住 attend。 |

两端 BF4 **不是同一张规格表上的同一颗芯片**，是同一代 DPU 家族的 initiator / target。缺一端，Memos 管道不成立。硬件展开见下一节。

## 4. 核心机制：双端 BlueField 切开 I/O 管道

角色总表见 §3.5。这里展开 **DOCA Memos 为什么必须双端**。计算节点和存储柜 **两边都要有 BlueField-4**。会话原话：pipeline 拆到两端，因为各有各该靠近的东西。

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

胶水角色见 §3.4。NIXL 不是「CMX 的驱动」，也不是第二套编排。它是推理数据路径的 **统一传输库**：一次请求会同时碰到

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

NVIDIA **自己的编排**把层写成 Dynamo `StorageTier`（详见 §3.2）。这里只钉和 Rubin 机柜的对齐：

| `StorageTier` | `from_kv_medium` | 和 G 层怎么对齐（NVIDIA 栈内部） |
|---------------|------------------|----------------------------------|
| `Device` | GPU / DEVICE | G1 |
| `HostPinned` | CPU / CPU_PINNED / CPU_TIER1 | G2 |
| `Disk` | CPU_TIER2 / DISK / NVME | G3 本地盘 |
| `External` | EXTERNAL / NETWORK / REMOTE / **SHARED** | G3.5 CMX 和/或 G4——软件枚举 **没有单独的 G3.5** |

Router 对 Disk 和 External 目前用同一档 `disk_cache_hit_weight`（`scheduling/overlap.rs`）。也就是说：**硬件上 G3.5 是新层，控制面枚举还没长出第五档**；CMX 命中在 overlap 计分里先被当成「非本机、非 HBM」的共享层。这是 NVIDIA 公开代码里能看到的衔接缝，不是已完成的一对一映射。

G1/G2 的 page-in/out、抢占，GTC 明确说 **主要仍由推理框架管**。CMX 补的是「框架 HBM/DRAM 管不过来、本地 SSD 又不能共享」那一段。

Spectrum-X 在这条路径上不是普通以太网：自适应路由、拥塞控制、无损 RoCE，用来压 **全 POD 并发 RDMA KV** 的抖动和尾延迟。KV 访问模式会打满统计复用型存储网络；没有可预期的 RDMA 织物，预取窗口就盖不住 attend。

## VAST 在 GTC 上的 CMX 落法（大容量长上下文）

NVIDIA 的 CMX 是参考架构；公开材料里把「PB 级长上下文」画成可部署系统、并且给了 sizing 表的，是 **VAST**。只读 S81773（DOCA Memos）会漏这一半。

主会话：

- GTC 2026 **S82255**（3 月 19 日）：*The Physics of Long-Context Inference: Breaking the Memory Wall With NVIDIA Dynamo*（Presented by VAST；Alon Horev / Anat Heilper）
- VAST Forward 2026：同题深化 *Breaking Through the GPU Memory Wall*（Anat Heilper + Dynamo 架构师 Vikram Sharma Mailthody）

### 和参考栈怎么接

VAST 不是另做一套 G3.5 硬件品牌，是把 **自己的存储软件塞进 NVIDIA 已经画好的双端 BF4**：

| 角色 | VAST 名称 | 跑在哪 | 干什么 |
|------|-----------|--------|--------|
| 计算侧 BF4 | **CNode** | 每台 GPU 服务器的 BlueField-4 | 放置、准入、快路径元数据；取消一排 x86 存储头 |
| 存储侧 BF4 | **DNode** | CMX / STX JBOF（G3.5） | 管盘。VAST 从 BF-1/BF-3 就把 DNode 卸到 DPU，BF4 是同一条线加马力 |
| 架构 | **DASE**（Disaggregated Shared-Everything） | 每个 CNode 看见 **全部** SSD 命名空间 | 无东西向协调、每主机一份专用带宽；这是他们解决「KV 全并发打满客户端–服务器」的办法 |

数据面：GPUDirect Storage + RDMA/NVMe-oF，**远端 SSD → GPU 零拷贝**，不经过主机 CPU。Blocks & Files 按 VAST 图读：G3.5 JBOF → G2 DRAM，**不经过 G3 本地盘**。NVIDIA 技术博客的口径是「典型 G1↔G2↔G3.5」；G3 在 CMX 叙事里经常被旁路（热插拔要停 GPU 柜）。Q&A 里 Anat 说本地 SSD 仍然更快，只是容量不够才上 G3.5。

控制面有一条和 S81773 **对不上的缝**：VAST 公开图是 DPU 上 **virtio-fs** 把命名空间呈给主机（文件接口），NIXL 走这个文件面；NVIDIA 产品页 / DOCA Memos 说的是 **KV API**（Vikram 在 VAST Forward 上也讲「新的 key-value NVMe API」）。Glenn Lockwood 的公开笔记标了这条不一致。两种都可能并存（文件给现有 Dynamo/NFS-oRDMA，KV 给 DOCA 4.0），**不能当成已经统一**。

VAST 自己的时间线要分开读，不要把数字糊到 Rubin CMX 上：

| 阶段 | 落点 | 数字从哪来 |
|------|------|------------|
| **今天** | Dynamo KVBM 把 VAST 当 **G3**（Anat 原话：currently integrated at G3 as local storage） | Llama-3 405B、128K ctx、8× Hopper、**2×100 Gb/s**：整段 prefill 重算 **65 s** vs 从 VAST 拉 **3 s** → **20× TTFT**；GPU 计算时间省约 **90%**；打满线速；混合 KV **1.4×** 数据缩减。这是 **现网 NFS-oRDMA / GDS**，不是 Rubin CMX |
| **下一步** | 同一套平台进 **G4**（S3 over RDMA），生命周期对引擎不透明 | 未给独立测试 |
| **CMX / G3.5** | 「upcoming Dynamo/VAST releases」把 VAST 做成 G3.5 本身，而不是只当 G4 底下那层慢存储 | 没有单独的 Rubin 实测。NVIDIA 官方 5× TPS 仍是 vs traditional storage |

所以：**20× / 90% 不是 CMX 的 SLA**，是 VAST×Dynamo 在 Hopper + 200G 上「拉缓存 vs 重算 128K prefill」的实验。CMX 要证明的是同一条路径在 POD 级以太网闪存、全并发下还能预取进 G1/G2。

BF4 相对 BF3 的口径也不要混：Anat 说 **2× 网络、4× ARM、4× RAM**；Vikram 说 **2× 网络、5× compute、3× 内存带宽**。Spectrum-X 在他们和 NVIDIA 的存储测试里大约 **+20% 吞吐**，外加拥塞控制 / 无损 RoCE。

### 大容量怎么算（会话里的 sizing 表）

这是 VAST 方案真正多出来的内容：把长上下文 × 高并发 × 多轮保留写成可部署容量，而不是停在「PB / GPU POD」。

假设 **1 万并发用户**、每条完整会话 KV **32 GB**。Anat 说这是保守数：他们自己的实验一条会话要卸 **65 GB**，表上留了约 50% 余量。32 GB 是 **容量规划旋钮**，不是某个模型在 1M token 上的 KV 公式。

| 档 | 留什么 | 容量 | 算法 |
|----|--------|------|------|
| Instant resume | 只留当前会话，暂停后免重算 | **320 TB** | 10k × 32 GB × 1 |
| Multi-turn / 一周 | 最近 5～15 个会话 | **1.6 PB ～ 4.8 PB** | ×5 / ×15 |
| Agentic memory | 最多约 150 个会话，「终身」上下文 | **48 PB** | ×150 |

VAST 2026-03 博客只写到 320 TB 和「多会话进 PB」；5 / 15 / 150 这一档来自 VAST Forward 幻灯 + HPCwire 对同一张 sizing 表的转述（台上把 1.6 PB 口误成 TB，算术和 HPCwire 都是 PB）。

这和 CES 上 Jensen 的 SuperPOD 口径是另一笔账，可以对上、不要当成 VAST SKU：

- 每柜 **4× BF4**，每 BF4 后 **150 TB** → **600 TB / 柜**
- **16 TB CMX / GPU** × 1152 GPU → **18,432 TB ≈ 18.4 PB / POD**
- S81773 随口说的「约 18 PB」（用来讲「别在主机上堆 per-object 元数据」）就是这个数

单柜硬件公开锚点：Jensen 的 4×BF4 + 600 TB 和 AIC F2032-G6（32× E3.S）对得上；Quanta / Supermicro 在 GTC 展的是 24 盘 STX 柜。Lockwood 的判断：量产软件落法里，把 CNode 跑在 **客户端** BF、DNode 跑在 BF 前端 JBOF 上的，公开讲清楚的主要是 VAST。

### VAST 多出来的、NVIDIA 参考栈故意拿掉的

- **可选数据服务**：纠删码可以关（KV 可重算），监管场景可以打开加密、多租户、审计、保留期。会话设想未来 Dynamo API 按数据集告诉存储「这份当缓存、这份要耐久」。
- **同一套软件跨 G3.5 和 G4**：热 KV 和冷历史 / 日志不必两套阵列。这和 Memos「G3.5 不做企业耐久」可以共存——耐久放 G4，VAST 两头都做。
- **1.4× 缩减**：共享 system prompt / 常见上下文的去重。Memos 的 128-bit 不透明对象默认没有这层。
- **功率**：VAST Forward 开场是 **70% lower storage power**。未经 Rubin 生产环境验证。

Q&A 里几条对难点有用：

- CMX **不必**做成 HA；要不要做，取决于 GPU 重算成本和 SSD 成本谁更贵。
- 加密走 BF4↔BF4 线速（计算侧和存储侧都有 inline crypto）。
- 拥塞靠 DASE（无东西向协调）+ Spectrum-X。
- GDS 经 **DOCA**；东西向还是南北向挂 CMX，台上说是部署方选择（NVIDIA 过去最佳实践是南北向存储）。
- 现有 BF3 集群再加 CMX / BF4，**没有**台上给出的混部方案。

不要把 **CNode-X** 和「CNode 跑在 GPU 服务器 BF4 上」混成一件事。CNode-X（VAST Forward 2026）是 VAST 自己的 **带 GPU 的 AI OS 服务器**（cuVS / Sirius / NIM），用来加速向量检索和 SQL；新闻稿里「支持 CMX」指集群配置能挂 BF4 + Spectrum-X，不是说 CNode-X 替代了 STX 盘框。

## 生态、软件切口、DPU/盘上的算法

DPU、DOCA、CMX 盘框是 **基板**。VAST 没再造一颗 DPU，也没再造一种 NAND。它做的是基板上的 **存储软件**。下面三问分开答：生态上 VAST 已经交了什么、软件还能做什么、硬件侧到底有没有算法可做。

### 生态里谁干什么

| 角色 | 谁 | 交什么 |
|------|----|--------|
| 参考架构 + 芯片 + 编排 | NVIDIA | Rubin GPU、BF4、Spectrum-X、STX 外形、DOCA Memos、Dynamo、NIXL |
| 盘框 OEM | AIC / Quanta / Supermicro | JBOF（32 盘 F2032-G6、24 盘 STX 展机） |
| **存储软件（公开讲清楚的）** | **VAST** | CNode 上计算侧 BF4 + DNode 上盘框 BF4 + DASE + Dynamo 对接 |
| 其它点名伙伴 | WEKA、DDN、Dell、IBM、Nutanix、Cloudian… | Lockwood 的读法：多数是「对齐 CMX」新闻稿，没有把控制面塞进客户端 BF 的公开设计 |

NVIDIA 给存储伙伴留的官方挂钩是两截：DOCA Memos 的开放 KV 接口（把你做成 G3.5），以及 Dynamo KVBM 的 **Event Plane**（你被动听 Store/Remove，自己建树、做冷热）。VAST 走的是更重的一条：把 **整套 CNode 控制面**搬进 GPU 服务器上的 BF4，而不只是做一个 G4 blob 后端。

### VAST 已经做了哪些软件

相对 NVIDIA 参考栈「故意拿掉企业功能」，VAST 加回去的都是 **可选软件**，不是新硬件 SKU。

| 软件 | 跑在哪 | 对 CMX 的作用 |
|------|--------|----------------|
| **CNode** | GPU 服务器 BF4（计算侧） | 放置、准入、快路径元数据；取消 x86 存储头。每个 CNode 看见全部 SSD 命名空间 |
| **DNode** | CMX/STX JBOF 的 BF4 | 管盘。BF-1/BF-3 就开始卸，BF4 加马力 |
| **DASE** | 上述两端 | 无东西向协调 → 全 GPU 并发时每主机一份专用带宽。这是他们的核心算法，不是盘规格 |
| **NFS-oRDMA / GDS**（今天） | 现网，当 Dynamo **G3** | 20× TTFT 实验走的是这条，不是 Rubin CMX |
| **S3 over RDMA**（下一步） | Dynamo **G4** | 冷历史、对引擎不透明的生命周期 |
| **DOCA / virtio-fs**（G3.5，upcoming） | BF4 → 主机 | 目标是成为 G3.5 本身。控制面仍可能是文件（virtio-fs），和 Memos KV API 还没收口 |
| **数据缩减** | 存储软件 | 实验 1.4×（共享 system prompt / 常见前缀去重）。Memos 默认不透明 128-bit 对象没有这层 |
| **可选数据服务** | 存储软件，可关 | 纠删码、加密、多租户、审计、保留期。KV 可重算所以 EC 可以关；监管场景再打开 |
| **跨 G3.5 和 G4 的同一命名空间** | VAST AI OS | 热 KV 和冷日志不必两套阵列 |
| **sizing 方法** | 咨询/规划 | 1 万用户 × 32 GB × 保留会话数 → 320 TB / 数 PB / 48 PB。这是产品化，不是芯片 |

PolicyEngine / TuningEngine（VAST 称 2026 年底）管的是 **agent 访问权限和流水线**，不是 CMX 热路径上的 KV I/O。不要算进 G3.5 数据面。

### 软件上还能做什么

按层排。NVIDIA 参考栈把策略留在 Dynamo/引擎，把 I/O 留给 DOCA；中间的空档就是软件切口。

**Dynamo / 引擎（主机侧，公开代码里已经有钩子）**

| 切口 | 现状 | 还能做 |
|------|------|--------|
| **G3.5 当成一等层** | `StorageTier` 无独立档；Disk 与 External 同一 `disk_cache_hit_weight` | 给 CMX 单独权重和 onboard 路径，别和 G4 对象混打分 |
| **预取预算** | 博客只说 prestage 进 G2/G1，没有 SLA | timeout / best-effort / 按 token 数的预算（SGLang HiCache 有同类三策略，NIXL 这边还没写成 CMX 专用） |
| **offload 过滤** | `OffloadFilter` + `FrequencyFilter`（按 sequence_hash 频次，加倍计数、定期衰减） | 只把「还会被别的请求复用」的块写 CMX；一次性 decode 后缀不必出柜 |
| **partial retrieve / hole** | GTC 说 Dynamo 和 vLLM 在做 | 缺一块只重算缺的；Router overlap 不能把 hole 当成后缀全灭 |
| **两套哈希对齐** | Router u64 链 vs Memos 128-bit vs G4 `ObjectStorage` u64 | 调度身份如何压进 16 字节、会不会碰撞——公开材料没钉，这是软件必须定的契约 |
| **P/D 布局身份** | CMX 不转换张量 | key 混入 dtype/TP/`page_size`，或 bounce buffer 里 convert。见后文「布局不一样」节 |
| **禁止 exist-then-get、huge page** | DOCA 强加的契约 | 引擎/NIXL 改控制流；`ignore_read_not_found` 已经是插件开关 |

**NIXL（传输胶水）**

- `DOCA_MEMOS` 插件：设备地址、`subnqn`/`ns_id`/`NGUID`、threaded 进度引擎、读 miss 开关。
- bounce 今天 **必须 CPU**。软件上能做的是减少 bounce 次数、和 GDS 路径怎么收口——不能假装已经是 GPU 当 NVMe initiator。
- 东西向 PD 和南北向 CMX 共用一套 transfer API：prestaging 和 P→D 可以排进同一个队列。

**存储伙伴（Dynamo 给的官方扩展模型）**

KVBM **不指挥盘怎么排**。Event Plane 批量发 StoreEvent / RemoveEvent（`sequence_hash`、`prefix_hash`、`storage_location`，约 100 B，可 ~10 s 一批）。伙伴侧 **Storage Advisor**（kvbm-design 里写明 optional）可以：

- 用 `prefix_hash` 建树 / LRU，做 **热块提升、冷块下降、按前缀压实**。
- 自己决定这份 KV 当缓存（可丢）还是要耐久（G4）。VAST 设想未来 Dynamo API 会按数据集把这个意图说清楚。
- 不必改 KVBM 热路径。

VAST 的 CNode 比这个 Advisor 更重：元数据解析直接在计算侧 BF4 上，不是主机上一个订阅进程。其它厂商可以只做轻量 Advisor + G4 blob。

### 硬件上有算法吗？

有。只是算法跑在 **BF4 的硅加速器 / ARM / 盘固件** 上，不跑在主机 Python 里。DPU 和盘框不是「没有软件的铁盒子」。NVIDIA 自己对 STX 存储处理器的表述是：KV I/O、**元数据、放置、队列、recall/pre-staging、租户策略、数据保护** 都靠近存储和网络路径，而不是把闪存呈现成一块普通盘。

分三层，免得和 Dynamo 的策略混在一起：

**1. 已经在硅里的，不必再发明**

| 能力 | 在哪 | 对 KV 的意义 |
|------|------|----------------|
| 线速加密 / CRC | BF4 加速器，公开口径 800 Gb/s 不吃 Grace | KV 不做副本仍要在线上做完整性 |
| NVMe-oF / RDMA 终止 | BF4 + CX-9 | 主机 CPU 不进热路径 |
| Spectrum-X 拥塞控制、自适应路由、无损 RoCE | 交换机 + NIC | 全 POD 同时打 KV 时压尾延迟 |

**2. 必须跑在 DPU 上的算法（这是「硬件侧软件」的主战场）**

这些公开材料 **点了名、没给公式**。VAST 的 CNode/DASE 是目前唯一讲清楚的实现。

| 算法 | 为什么必须在 DPU / 盘侧 | 已知约束 |
|------|------------------------|----------|
| **key → 柜 / 盘 的映射** | 主机不持 18 PB 的 per-object 目录；计算侧 BF4 仍要把 retrieve 送到正确的柜 | 一致性哈希 / 故障改向 **没有公开算法** |
| **KV volume 按 max block size 切** | 固定块大小（GTC：DeepSeek ~800 KB → 1 MB 卷；Llama-70B ~10 MB 另卷；当前 key 最长 128 bit） | 一种模型家族一个卷；P/D 块大小不同就要两卷 |
| **NAND packing（GTC：按约定 format and layout 写到盘上）** | 写放大要接近 1；KV 不可变、整块、顺序 | **NAND 布局，不是张量布局**。适合大顺序写的 FTL/GC，而不是 POSIX 4K 随机 |
| **放置与共置** | 同一前缀的连续 block 物理靠近，预取一次顺序读 | Event Plane 的 `prefix_hash` 就是给这件事用的；Memos 默认不保证 |
| **队列 / QoS / 多租户隔离** | 所有 GPU 同时 RDMA；要在 DPU 上做，不能回到 x86 头节点 | STX 博客点名 tenant policy；VAST 用 DASE 消东西向争用 |
| **recall / 辅助 prestaging** | NVIDIA 说存储处理器也参与 cache recall 和 pre-staging | **何时**预取仍归 KVBM；DPU 做的是 outstanding I/O 和靠近介质的调度 |
| **耐久策略** | KV 可重算 → 默认可关 EC/副本；监管再打开 | 要不要 HA = GPU 重算成本 vs SSD 成本（VAST Q&A） |
| **去重 / 压缩** | 共享前缀字节相同 | VAST 1.4× 是软件缩减；BF4 有压缩加速器，Q&A 说 VAST 会用。Memos 不透明对象默认不做 |

**3. 不要放到硬件里的**

选哪张 GPU、P/D 怎么拆、张量 dtype/TP 转换、Router overlap——这些是 Dynamo / 引擎。放到 DPU 上会把存储软件变成第二套调度器，和「应用无状态、DPU 只做 KV 设备」的切分打架。

一句话：**硬件侧的算法是「固定大小、不可变、可丢的 KV 对象，在 PB 级闪存上怎么映射、怎么排、怎么顺序写、怎么在全并发下隔离」；软件侧的算法是「这块对象该不该出 HBM、该不该预取、请求去哪张 GPU」。** VAST 把前一半做成了 DASE+CNode；后一半仍是 Dynamo，而且 G3.5 档还没长进开源枚举。

## 10. 公开数字 vs 没公布的

| 有 | 没有（不要当成数据表） |
|----|------------------------|
| NVL72 / HBM4 288 GB @ 22 TB/s / 50 PFLOPS NVFP4；POD ≈ 1152 GPU；CX-9 1.6 Tb/s 端点（部分表）；BF4 800 Gb/s | CMX 保证带宽/GPU、prestaging SLA、副本因子（设计上接近无副本）、故障后重算预算 |
| Jensen CES：4×BF4 / 柜、150 TB / BF4 → **600 TB/柜**；**16 TB/GPU** → 1152 GPU ≈ **18.4 PB/POD**（S81773 口头 ~18 PB 即此） | 5× TPS 的对照负载（模型、ctx、命中、batch） |
| VAST sizing：1 万用户 × 32 GB/会话 → 320 TB / 1.6–4.8 PB / **48 PB**（1 / 5–15 / 150 会话） | Rubin + CMX 的 20× 实测（20× 是 Hopper+VAST G3 实验） |
| VAST×Dynamo：128K Llama-3 405B，65 s 重算 vs 3 s 拉取；1.4× 缩减；200G 线速 | Memos 的 placement/GC；virtio-fs 与 DOCA KV API 如何收口 |
| KV volume + max block size；128-bit key；双端 Memos | 跨 CMX 柜的具体映射函数 |
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

## 创新点

相对「本地 SSD + 文件系统 / 企业共享盘扛 KV」，能站住的切口就这几条。RDMA、NVMe-oF、DPU 做加密、分层缓存——都不是新的。

1. **按 KV 数据类裁协议，而不是把 KV 塞进通用存储。** 固定块、不可变、可重算、大 IO、必须共享——五条一起决定：对上 KV、对下 block、中间不挂 DFS。这是产品定义，不是介质升级。
2. **双端 BlueField 切开 I/O 管道。** SNAP 那套「主机看见模拟设备、编排在 DPU」第一次用在 KV 设备上：计算侧 BF4 做隔离/映射/线速完整性，存储侧 BF4 做盘上 packing。缺一端这条路径不成立。
3. **把非 100% 耐久做成一等契约。** 不是「尽量别丢」，而是框架必须处理 miss 和 **context hole**（中间缺一块，只重算缺的）。这换来写放大≈1、不做副本/scrub。
4. **主机不做 per-object 元数据。** 对象规模按 PB 计时，inode 式索引会先把 DRAM 吃光。Exist/位置由 Memos 答；Dynamo 只留调度用的 block 身份，不是闪存目录。
5. **用 POD 级共享解开「KV 绑本地盘 → 请求绑 GPU」。** G3.5 命中的语义是任意计算节点可 retrieve，路由函数必须改。没有这一条，G3.5 只是更大的本地盘。
6. **编排平面和 I/O 平面切开。** Dynamo 答「哪张 GPU、哪一层、何时预取」；DOCA 答「这个 key 怎么落到哪组盘」。混在一起要么在主机堆闪存目录，要么让存储软件去选 GPU。

NIXL 把 PD 东西向和 CMX 南北向收成一套传输 API，是这条栈能落地的胶水，本身不是 CMX 闪存的创新。

## 难点

难的不是「把 SSD 插到 STX 柜里」。

| 难点 | 为什么难 |
|------|----------|
| **预取窗口** | 1M 前缀一次是数 GB 到几十 GB。attend 之前必须进 G1/G2。以太网闪存没有公开 prestaging SLA；窗口盖不住，GPU 空转，5× 叙事塌掉。 |
| **全 POD 并发** | KV 不是企业盘那种统计复用。所有 GPU 同时 RDMA 读写，拥塞和尾延迟是织物问题（Spectrum-X 要解决的），不是 SSD 带宽表问题。 |
| **key → 柜 的映射** | 主机不持全量元数据，计算侧 BF4 仍要把请求送到「正确的 CMX 柜」。映射/一致性哈希/故障改向 **没有公开算法**。 |
| **引擎契约** | 禁止 exist-then-retrieve、huge page、hole 重算、路由不再钉本地盘——现有引擎大量反模式。CMX 的语义要引擎改，不是插上就能用。 |
| **值的身份 / 布局** | Memos 存不透明字节。key 默认是 prefix hash，**不含** dtype、TP、page 布局。同一模型 P/D 布局不同会直接踩坑，见下一节。 |
| **两套哈希** | Router `ExternalSequenceBlockHash` 是 u64 链；CMX 对象名是 128 bit。谁负责把调度身份压进 16 字节、会不会碰撞，公开材料没钉。 |
| **CPU bounce** | NIXL `DOCA_MEMOS` 要求 `nixl_buffer_device=cpu`。热路径多一跳主机/DPU 内存，不是 GPU 直接当 NVMe initiator。 |
| **双 DPU 故障域** | 计算侧 BF4 挂了，主机看到的模拟 KV 设备就没了；存储侧 BF4 挂了，卷不可达。块可重算，但 prestaging 中断仍会打 TTFT。 |
| **控制面没有 G3.5 档** | Dynamo `StorageTier` 只有 Device/HostPinned/Disk/External，Disk 与 External 同一档命中权重。硬件多出来的层，软件还当「非本机共享」打分。 |
| **能效账** | 省下的是耐久服务功耗；花掉的是 miss 重算 + 双 BF4 基线功耗。没有公开负载，这本账算不完。 |

盘上 packing（GTC：「按我们定的 format and layout 写到盘上」）是 **NAND 布局**，不是张量布局。两件事不要混。

## 同一模型、Prefill / Decode KV 布局不一样

CMX **不转换**张量布局。它按 key 存一段字节，retrieve 原样还给你。GTC 按模型建 volume、配一个 `max KV block size`，隐含假设：**跑这个模型的计算节点，写出的块大小（因而布局家族）是一样的。**

「同一个模型」在引擎里仍然可以不是同一种 KV：

| 轴 | 例子 | 对 CMX 的后果 |
|----|------|----------------|
| token 分页 | `block_size` 16 vs 64 vs 256 | 每 key 的 value 长度变了；对不上 volume 的 max block，或 retrieve 长度错 |
| 内存排布 | SGLang `layer_first` / `page_first` / `page_first_direct` | 字节数可能相同，attention 读出来是垃圾 |
| 精度 | BF16 vs FP8 vs `fp8_ds_mla` 656 B/层 | 块大小和内容都变 |
| TP / 头切分 | P 侧 TP=8、D 侧 TP=4；MLA 每 rank 一份副本 vs 切头 | 每 rank 写入的切片不是同一对象 |
| MLA / 混合注意力 | per-layer 连续 view vs cross-layer 打包；indexer / SWA / KDA 是否打进同一块 | 一块里装的平面不同 |
| 引擎 | vLLM paged vs SGLang page；TileRT decode 还要 FP8 反量化、NSA 平面 | 必须在边界 convert，CMX 不会做 |

三种结局：

1. **P 和 D 用同一个 128-bit key（纯 prefix hash），value 布局不同。**  
   Retrieve「成功」。若长度仍落在 volume 上限内，Memos 不会报错——它不解释张量。Decode 用错误布局做 attention，属于静默损坏。这是最坏情况。
2. **块大小已经对不上。**  
   按 GTC 该开两个 KV volume（两个 max block size）。P 写入的对象 D 根本不在那个卷上，表现为 miss → 重算。共享前缀这条路断了，G3.5 对 PD 没用。
3. **长度碰巧一样、内容不一样。**  
   还是（1）。CMX 没有 checksum-of-layout。

正确做法都在 **CMX 之外**，NVIDIA 公开栈也是这么处理 PD 的，只是对象从「对端 GPU」换成了「CMX 卷」：

| 策略 | 谁做 | 效果 |
|------|------|------|
| **强制 P/D 同一份 KV spec** | 部署/握手 | TileRT：两端 `--kv-cache-dtype` 必须一致，握手失败即拒。这是 CMX volume「一个模型一个 max block」能成立的前提。 |
| **key 绑定布局身份** | 引擎在写 128-bit 名之前 | 把 dtype、TP、`page_size`、mem-layout、MLA pack、revision 混进 key（再 hash 到 16 字节）。不同布局变成不同对象，不会串味，但 **跨布局不复用**。vLLM 的 `generate_block_hash_extra_keys` 今天混的是 LoRA / 多模态 / `cache_salt` / prompt embeds，**不是** TP 和 mem-layout；`make_block_hash_with_group_id` 只隔离 KV-cache group（full vs MLA）。SGLang 用 `tp_lcm_size` + `should_split_heads` 把不同 TP 切成可拼接的多 key（`cache_controller.py::_generate_storage_config`），这是异构 TP 复用，不是任意布局转换。 |
| **边界转换** | 写 CMX 前或读 CMX 后，在引擎/NIXL bounce buffer | 收成规范布局再 store；retrieve 后再 convert 成 D 侧 HBM 布局。Memos / `DOCA_MEMOS` 插件不做这件事。代价是 CPU 上的 gather/scatter，和 vLLM connector 逐层 load、TileRT `extract`/`convert` 是同一类税。 |
| **两个 volume、两份拷贝** | 存储配置 | P 写 P 卷、D 读 D 卷，中间仍要一次布局转换或放弃共享。容量和预取带宽都 ×2。 |

所以：同一个权重、PD 分离、KV 布局不同时，**CMX 不会帮你对齐**。要么部署上把 P/D 的 `KVCacheSpec`（vLLM：`block_size` + `page_size_bytes` + MLA `cache_dtype_str`/`compress_ratio`）做成同一份，要么在 key 或 bounce buffer 上自己做身份/转换。否则不是 miss，就是静默读错。

vLLM 进程内 `KVCacheSpec.merge` 要求同一 cache group 的层 spec 完全相等——那是单引擎约束。Prefill 进程和 Decode 进程各有一份 spec，CMX 看不到这两份是否相等。

## 代码索引

NIXL 不在本仓库 submodule。沿符号回溯：

| 机制 | 位置 |
|------|------|
| Dynamo 层枚举（无独立 G3.5） | `3rdparty/dynamo/lib/kv-router/src/protocols.rs`::`StorageTier` / `from_kv_medium` |
| Disk vs External 同一档命中权重 | `3rdparty/dynamo/lib/kv-router/src/scheduling/overlap.rs`::`cache_hit_weight_for_tier` |
| 链式 block hash（Router overlap，64-bit） | `protocols.rs`::`ExternalSequenceBlockHash` / `LocalBlockHash` |
| KVBM 四层（尚无 G3.5） | `3rdparty/dynamo/lib/kvbm-engine/docs/architecture.md`；`docs/design-docs/kvbm-design.md` |
| Offload / onboard 状态机 | `3rdparty/dynamo/lib/llm/src/block_manager/offload.rs`::`OffloadManager` |
| 按频次过滤是否卸层 | 同目录 `filter.rs`::`OffloadFilter` / `FrequencyFilter` |
| G4 不透明 blob、u64 key | `3rdparty/dynamo/lib/llm/src/block_manager/storage/object.rs`::`ObjectStorage` |
| NIXL get/put + Event Plane / Storage Advisor | `3rdparty/dynamo/docs/design-docs/kvbm-design.md`（`registerVolume` / StoreEvent / prefix 树冷热） |
| 请求/控制/状态三平面 | `3rdparty/dynamo/docs/design-docs/architecture.md` |
| NIXL DOCA_MEMOS 后端 | `ai-dynamo/nixl` `src/plugins/doca_memos/doca_memos_backend.{h,cpp}`::`nixlDocaMemosEngine` / `docaMemosKey` |
| KV 设备进度与 `doca_kvdev_io_set_conf` | `src/plugins/doca_memos/doca_memos_progress_engine.cpp` |
| 128-bit hex 对象名、cpu bounce | LMCache docs：NIXL `DOCA_MEMOS` backend |
| NVMe-KV 16 B in-command key | NVM Express Key Value Command Set；SPDK `SPDK_NVME_KV_KEY_MAX_LEN = 16` |
| 单引擎层 spec 必须相同 | `3rdparty/vllm/vllm/v1/kv_cache_interface.py`::`KVCacheSpec.merge` / `MLAAttentionSpec` |
| block hash extra keys（不含 TP/layout） | `3rdparty/vllm/vllm/v1/core/kv_cache_utils.py`::`generate_block_hash_extra_keys` |
| 异构 TP 切 key | `3rdparty/sglang/python/sglang/srt/managers/cache_controller.py`::`_generate_storage_config`（`tp_lcm_size` / `should_split_heads`） |
| PD 握手拒 dtype 不一致 | `3rdparty/tilert/tilert/pd_vllm/`（`--kv-cache-dtype`；`profiles/*.py`::`extract`/`convert`） |
| VAST CNode / DASE / virtio-fs | 无 submodule。公开博客 + GTC S82255 / VAST Forward 会话；Lockwood `garden/icms` |

## 来源

- [GTC 2026 S81773 — Accelerate AI Inference Using DOCA for Storage](https://www.nvidia.com/en-us/on-demand/session/gtc26-s81773/)（双端 Memos、KV volume、128-bit key、禁止 exist-then-get、context hole、SNAP 类比）
- [GTC 2026 S82255 — The Physics of Long-Context Inference（VAST × Dynamo）](https://www.nvidia.com/en-us/on-demand/session/gtc26-s82255/)
- [VAST：How NVIDIA Dynamo and VAST Unlock Context Reuse at Scale](https://www.vastdata.com/blog/how-nvidia-dynamo-vast-unlock-context-reuse-at-scale)（20× 实验；10k×32 GB → 320 TB；CMX G3.5 未来发布）
- [VAST：More Inference, Less Infrastructure](https://www.vastdata.com/blog/more-inference-less-infrastructure-vast-nvidia)（CNode 上 GPU 服务器 BF4、DASE、零东西向）
- [VAST：Right-Sizing KV Cache](https://www.vastdata.com/blog/stop-wasting-gpu-cycles-a-practical-guide-to-right-sizing-kv-cache)（G3.5 落 Dynamo/VAST upcoming；同一平台跨 G3.5 和 G4）
- [HPCwire：VAST sizing 表 320 TB / 1.6–4.8 PB / 48 PB](https://www.hpcwire.com/2026/03/02/blasting-through-the-gpu-memory-wall-with-nvidias-new-cmx-platform/)
- [Blocks & Files：伙伴 KV 扩展 / VAST virtio-fs、G3.5→G2 旁路 G3](https://www.blocksandfiles.com/ai-ml/2026/03/30/nvidia-and-its-partners-kv-cache-extenders/5209284)
- [Glenn Lockwood：CMX / ICMS 笔记](https://glennklockwood.com/garden/icms)（Jensen 4×BF4 / 600 TB / 16 TB/GPU；virtio-fs vs DOCA KV）
- [NVIDIA CMX 产品页](https://www.nvidia.com/en-us/data-center/ai-storage/cmx/)（Dynamo：KV-aware placement；DOCA Memos：KV API + 无状态应用）
- [NVIDIA：BlueField-4-powered CMX](https://developer.nvidia.com/blog/introducing-nvidia-bluefield-4-powered-inference-context-memory-storage-platform-for-the-next-frontier-of-ai/)（KVBM+NIXL 编排层间移动；Grove 拓扑放置；Memos 开放给存储伙伴）
- [NVIDIA：Scaling Agentic AI Factories with BlueField](https://developer.nvidia.com/blog/scaling-agentic-ai-factories-through-extreme-co-design-with-nvidia-bluefield/)（STX 存储处理器：KV I/O、元数据、放置、队列、recall/pre-staging、租户策略）
- [VAST Forward：CNode-X / CMX 集群配置](https://www.vastdata.com/press-releases/vast-data-introduces-end-to-end-fully-accelerated-ai-data-stack-with-nvidia)（CNode-X ≠ CNode-on-BF4）
- Dynamo 源码：`docs/design-docs/{architecture,kvbm-design}.md`；`lib/kv-router/src/protocols.rs`；`lib/llm/src/block_manager/offload.rs`
- [NVIDIA：Vera Rubin POD](https://developer.nvidia.com/blog/nvidia-vera-rubin-pod-seven-chips-five-rack-scale-systems-one-ai-supercomputer/)
- [NVIDIA：Inside Vera Rubin](https://developer.nvidia.com/blog/inside-the-nvidia-rubin-platform-six-new-chips-one-ai-supercomputer/)
- [NVIDIA Newsroom：STX](https://nvidianews.nvidia.com/news/nvidia-launches-bluefield-4-stx-storage-architecture-with-broad-industry-adoption)
- [NIXL PR #1717 DOCA MEMOS backend](https://github.com/ai-dynamo/nixl/pull/1717)
- [LMCache NIXL / DOCA_MEMOS](https://docs.lmcache.ai/kv_cache/storage_backends/nixl.html)
- [NVM Express Key Value Command Set](https://nvmexpress.org/specification/key-value-command-set-specification/)
- [vLLM DeepSeek V4 附录](https://vllm-project.github.io/2026/04/24/deepseek-v4.html)
- DeepSeek-V4 论文 §4.2.1（arXiv:2606.19348）
