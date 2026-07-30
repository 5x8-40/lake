# 03 — 存储层

存储层是彻底存算分离的根基,托管两类数据:**权重**与 **KV cache**,并提供分层缓存。

存储池是长期存续、模型无关的独立基础设施:同一池同时承载多个 `(model_id, revision)` 的权重与 KV,模型上下线与池生命周期解耦。池提供按模型的空间分配/扩缩容、GC、碎片整理(详见 [`kv-cache-pool.md`](kv-cache-pool.md))。

## 数据模型

### 权重
- 不可变,按 `(model_id, revision)` 寻址。
- 粒度:按 layer / tensor 分片,支持并行加载与按需加载。
- 格式:原始 fp16/bf16,可选量化副本(int8/int4/fp8)作为独立 artifact,不在线反量化。

### KV cache
- 可变、有生命周期,按 `(model_id, block_hash, pool_kind)` **内容寻址**(见 [`kv-cache-pool.md`](kv-cache-pool.md)):相同前缀 → 相同 block hash → 命中同一 KV,前缀复用天然成立。block = page(128 token × 全部层,page-first),身份按 token 段不含 layer_idx(layer_idx 只在传输切片 LayerSlice 出现)。
- 粒度:**per-block + 前缀树(radix)**索引(复用友好)。**block 粒度 = 128 token**(缓存命中/复用/传输/写回的最小单位,初版默认,待 P7 校准;与 SGLang `--page-size`、vLLM `block_size` 同量级,常见 16/128/256)。
- 持久化:热数据在 HBM/DRAM(L0/L1 缓存),F4 恢复点落 NVMe(L2),冷数据落对象存储(L3 SSOT)。

### KV 类型:t-type / r-type

两类的**复用条件一致**:都需**命中全部前缀**才能复用——从序列起点的连续前缀 KV/state 必须在场,attention/state 才能从该点续算。区别**仅在 HBM(L0)的存储形态**:r-type 用紧凑表示替代逐 token 的完整 KV,以降低 HBM 占用。

| 类型 | HBM(L0)存储形态 | 占用 | 典型算子 |
|------|-----------------|------|----------|
| **t-type** | 逐 token 完整 KV,paged block table | 随序列长度线性增长 | full attention、MLA |
| **r-type** | 紧凑表示:滑动窗口最近 W token / 定长 recurrent state | 亚线性或常量 | sliding window attention、Mamba/state-space、卷积类 |

**各层组织**:

- **HBM(L0)**:两类并存,引擎按类型分 arena / 管理器(t-type block arena + r-type 状态 arena)。**区分的唯一目的是减少 r-type 的 HBM 占用**——r-type 不存逐 token KV 而存紧凑状态(参考 vLLM per-group `SingleTypeKVCacheManager` + `KVCacheSpec`(`FullAttentionSpec`/`MambaSpec`);SGLang multi-pool `PoolName`(Mamba/SWA/DSA/Draft))。Q1 的"固定 arena"需为 r-type 另设固定状态 arena(见 [`compute-layer.md`](compute-layer.md) "KV 类型"节)。
- **DRAM/SSD(L1–L3)**:缓存命中/存取最小单位是 block(128 token),下层**统一按 block、page-first 组织**。两类复用条件本就一致(全前缀命中),故下层**不区分类型**;r-type 落下层时在 block 边界 checkpoint 其紧凑状态:
  - sliding window:存 **trailing pages**(最近 W token,参考 SGLang `PoolHitPolicy.TRAILING_PAGES`)。
  - Mamba/state-space/卷积:每 128 token 存一份 recurrent state 快照。
  - 复用时 radix 沿前缀匹配到最长边界,取该边界处的完整 KV(t-type)或 state 快照(r-type),从该点续算。

> **关键差异(相对参考实现)**:SGLang v2 multi-pool 把 Mamba/SWA/DSA/Draft 各开独立 pool + 各自 `PoolHitPolicy`(`ALL_PAGES`/`TRAILING_PAGES`),按类型**物理分池**存(`hicache_storage.py::HiCacheStorage.batch_exists_v2` / `PoolTransfer`)。我们更彻底:类型区分**只存在于 HBM 存储形态**(降占用),L1–L3 统一按 block 存储池承载(池本就不解释张量布局,按不透明字节块存取),block 内装的是逐 token KV 还是紧凑 state 快照由布局元数据声明,而非物理分池。

r-type 状态 checkpoint 的形式/间距与 L1+ 持久化性价比(Mamba state 是否值得落 L1+)留开放,见 [`kv-cache-pool.md`](kv-cache-pool.md) "t-type / r-type"开放点。

## 分层缓存

**层 = 介质,不是位置。** L0–L3 四层全部由存储池统一管理(放置/驱逐/副本/冷热/生命周期)。计算节点不拥有内存——HBM/DRAM/NVMe 是存储池的物理载体,计算服务向存储池申请放置而非自行管理本地缓存。所有 KV 位置(含"哪段 KV 在哪个节点 HBM / DRAM / NVMe")均为存储池权威元数据。统一管理的全局原则见 [`overview.md`](overview.md),本节讲分层细节。

> **为何 DRAM 不分本机/远端两层**:DRAM 是一种介质、一个池。一个 block 物理放本机 RAM 还是远端 KV Node 的 RAM,是存储池的**放置决策**(按延迟/容量/前缀亲和),和"L0 放哪个节点 HBM"是同一套机制——不该用 tier 编号固化成两层。同理 NVMe 也是一层(L2),block 放哪个节点 NVMe 由池决定。旧设计的 L1(主机 RAM)/ L3(远端内存池)按位置拆分,现合并为 L1(DRAM 池);旧 L4(对象存储)顺移为 L3。

> **纵切不横切（按职责切，不按层切）**：池的职责切分是**纵切**——全局控制面管**全局提交态元数据**（block 全局可见位置、ref 汇总、放置/GC/defrag/shard 决策）,每个节点上的 agent 管**所有层的本地热路径状态和本地操作**（local overlay、注册 MR、分 slot、本地 ref、serve、搬字节）。不是横切（agent 管 L0、全局管 L1+，按层分主）。理由：
> 1. **block 身份跨层不变 → 全局 owner 不该换**。一个 block 从 L0 demote 到 L1，`(model_id, prefix_hash, block_id)` 不变。横切意味着 demotion 那刻 owner 从 agent 换成全局，要协调交接（谁管全局 ref、过渡期归谁）——一个不可变对象的全局 owner 变了，平添一致性问题。纵切下全局 owner 恒为存储池控制面；本机物理状态先在 agent local overlay 落地,再以事件/批量提交方式收敛到全局视图。
> 2. **多副本要求单一权威横跨所有层**。热前缀常 L0+L1+L2 各一份。横切每层一个 owner，同一 block 多个 owner，ref 放谁、谁权威？纵切下全局记一个条目多位置、ref 一份，天然支持多副本。
> 3. **放置/驱逐/迁移是跨层跨节点决策**。"热块从 L2 promote 到 L0"要看热度、目标容量、副本分布，是全局优化。横切把决策权按层切碎，跨层要协商；纵切下全局看全图决策，agent 只执行。
> 4. **本地命中省 RPC 不靠 owner 切分**。横切唯一吸引力是"agent 管 L0、引擎不问全局"。但 lake 已用 **agent 只读镜像**满足这点——agent 持全局推送的 radix 副本，查本地命中零 RPC（见 [`kv-cache-pool.md`](kv-cache-pool.md)「前缀树索引」）。镜像吃掉了横切想省的那一跳，又不引入 owner 交接。
> 5. **与已定原则一致**：CLAUDE.md 第4条"计算节点不拥有任何内存——HBM/DRAM/NVMe 是池的物理载体，不是 worker 私有状态"直接否定横切（横切即"agent 拥有 L0"）。

横切按层分主（L0 归 agent、L1+ 归全局，demotion 时 owner 换手）；纵切按职责（本机热路径归 agent，全局提交态归 CP，切线竖着穿过全部四层，全局 owner 恒为控制面）：

```mermaid
flowchart LR
    subgraph HZ["横切: 按层分主 (否定)"]
        direction TB
        H_L0["L0 HBM ── agent 拥有"]
        H_CUT["──── 切在这里 ────\ndemotion 时 owner 换手"]
        H_L1["L1 DRAM ── 全局拥有"]
        H_L2["L2 NVMe ── 全局拥有"]
        H_L3["L3 对象  ── 全局拥有"]
        H_L0 --- H_CUT --- H_L1
        H_L1 --- H_L2 --- H_L3
    end
    subgraph VT["纵切: 按职责 (采用)"]
        direction TB
        V_GLOBAL["全局控制面 ── 全局提交态\nradix / locations / ref summary / GC"]
        V_AGENT["每节点 agent ── local overlay\nslot / local ref / MR / serve / bytes"]
        V_GLOBAL -->|"view / plan / confirm"| V_AGENT
        V_AGENT -->|"async events / batched commit"| V_GLOBAL
        V_L0["L0 HBM ── agent 注册+分slot, 全局记位置"]
        V_L1["L1 DRAM ── agent 注册/serve, 全局记位置"]
        V_L2["L2 NVMe ── agent serve, 全局记位置"]
        V_L3["L3 对象 ── 全局直接管"]
        V_AGENT -.- V_L0
        V_L0 --- V_L1 --- V_L2 --- V_L3
    end
```

| 层级 | 介质 | 容量 | 延迟 | 角色 | 管理主体 |
|------|------|------|------|------|----------|
| L0 | GPU/NPU HBM | 极小 | ~ns | 池放置的计算载体(易失,缓存) | 存储池(元数据强一致,物理载体在计算节点) |
| L1 | DRAM(池化) | 中 | ~μs(本机) / ~10-100μs(远端 RDMA) | 缓存副本(易失) | 存储池(block 放本机/远端 RAM 由池放置决定) |
| L2 | NVMe(池化) | 大 | ~10μs | **F4 恢复点**(持久,断电不丢) | 存储池(block 放哪个节点 NVMe 由池决定) |
| L3 | 对象存储 | 无限 | ~ms | SSOT 永久权威 | 唯一权威 |

> **物理约束**:L0 的统一管理是元数据层面的——物理访问仍受 GPU 本地性约束,attention 读 KV 必须在本机 HBM,无法高效跨节点直读。因此同一 batch 各 sequence 的 KV 必须已在同一 GPU HBM(本地命中),否则先由存储池补拉。放置与 batch 的边界见下"方案 Z"。L1/L2 跨节点访问走 SNIC(见 [`topology.md`](topology.md))。

**读取路径**:L0 → L1 → L2 → L3,逐层回填。
**写入路径**(两条独立职责,不要混):
- **持久化写回** = 满/尾块落 L2 durable(服务 F4 恢复点 + radix 生长)。L2 是唯一持久层,写回即写 L2。L1 易失,不增持久性,不参与写回。
- **L1 副本来源 = L0 demotion**(L0 容量压力驱逐下沉 / 池主动 demotion 热块),**不是满块即写 L1**。即刚产出的 block 只在 L0;L1 副本在 L0 让位时产生,命中靠读路径 L0→L1 回填。仿 SGLang `write_back`(驱逐即 `_evict_write_back` D2H 到 host DRAM,节点带 `host_value`="evicted but backuped")。差异:SGLang L1→L2(GPU→host)驱逐同步触发、实例私有;lake HBM 归池、跨请求/跨节点,demotion 是池放置决策(可主动 demotion),但副本来源同样是 demotion。

按冷热决定是否落 L3(SSOT)。

## 冷热与生命周期管理

存储池对每个 KV block 全权决定冷热判定、层间放置、提升/下沉、驱逐与终结,贯穿 L0→L3。

### 层间副本 vs 移动

- **L0/L1 做副本(易失缓存)**:HBM/DRAM 是缓存副本,丢了下层(L2 NVMe 恢复点)还在,回填快;费空间但 L0/L1 小,可接受。
- **L2/L3 间按移动**:NVMe ↔ 对象存储,同层冗余无意义,省空间。
- **L3 永久权威**:唯一不可丢的副本(SSOT),L3 缺失才视为 block 不存在。

数据模型:一个 block 在元数据中有**多层位置**(L0/L1 各一份缓存副本,L2/L3 二选一),而非单一位置。`locations` 是"层→物理位置"集合;L0/L1 缺失只是缓存未命中,L3 缺失才视为不存在。

> **持久性分层**:L0/L1 易失(断电丢,靠 L2 回填);L2 持久(F4 恢复点,NVMe 持久 + NPU 故障不烧 NVMe,恢复能力与位置无关,worker 崩溃后从 L2 续推);L3 永久(SSOT,整机级故障兜底)。详见 [`consistency.md`](consistency.md) §4。

### 冷热判据

| 维度 | 性质 | 作用 |
|------|------|------|
| 引用数(in-flight ref) | 硬门槛 | >0 → 冻结迁移/驱逐,保护在途请求 |
| 访问频次 | 连续信号 | 热度主信号,驱动 promotion |
| 最近访问(recency) | 连续信号 | 防"曾经热、现在凉"占上层,驱动 demotion |
| 前缀亲和(公共前缀加权) | 修饰项 | 驱逐 tie-breaker,公共前缀不易驱逐 |

聚合(非简单加权和):
- 引用数 >0 → 冻结,不进冷热排序(正确性约束)。
- 否则**热度分** = f(频次, recency),驱动 promotion/demotion。
- 驱逐/下沉排序时,公共前缀 block 给予加权保护。

频次用 **LFU-Aging(衰减)** 而非滑动窗口:block 数量级大,滑动窗口元数据吃不消;衰减状态小、够用。参数待 P7 校准。

### 迁移触发:主动为主 + 被动兜底

- **主动**:后台扫描,按热度分 promotion(热块上提、L0 预放置)/ demotion(冷块下沉),保持上层常驻新鲜热数据、减少首次 miss。
- **被动**:读 miss 回填、写满驱逐,即时响应兜底。

主动是主路径——否则冷块淤积上层、热块首次必 miss,本地命中率起不来。

### 带宽预算

迁移 / GC / 碎片整理共享一个后台任务带宽池,内部按优先级调度。总预算 < 10%,不额外放宽(见 [`../features/slo.md`](../features/slo.md))。

### 放置与 batch 的职责边界(方案 Z)

存储池的 HBM 放置与计算层 batch 组成**单向耦合**:

- **存储池主动放置**:按热度/前缀亲和把高频 KV 预放置到节点 HBM,发布"哪些 KV 在哪个 HBM"的位置视图。不感知任何 batch 意图,只做通用热度放置。
- **调度器读视图组 batch**:读位置视图,优先把已在同一 HBM 的 sequence 组进同一 batch(本地命中 → D-direct),缺失的再让存储池补拉。不反向指挥放置。

信息流单向(存储池发布视图 → 调度器消费)。代价:通用预判未必对齐每个具体 batch——冷门前缀被组进 batch 时无本地命中,需临时补拉。

## KV Pool 架构(详见 05)

KV Pool 的远端物理载体由一组 KV Node 组成:每个贡献 DRAM + NVMe(L1/L2 池的远端载体),通过 RDMA 暴露 block 读写,元数据由存储池控制面维护。L0–L2 的物理载体分布在计算节点(本机 HBM/DRAM/NVMe)与 KV Node(远端 DRAM/NVMe)上,具体哪个节点由池放置决定,但元数据全归存储池;四层共用一套元数据与 API。KV Node 上跑的 agent(与计算节点 agent 共用传输引擎,详见 [`kv-cache-pool.md`](kv-cache-pool.md)「KV Node 上的 agent」)是池伸到该机的本地手,管 DRAM 注册 + NVMe serve,不持镜像、无 FFI。

## 一致性模型

- 权重:不可变,靠缓存失效(revision 变更)。
- KV cache:**写一次读多次**。Prefill 写入后不可变(针对该 token range),后续 token 的 KV 是新 block,只需单写者屏障,无需多写者并发控制。
- 故障恢复:KV block 写入 L2(NVMe,F4 恢复点)后才认为可续推;崩溃从最近的 L2 checkpoint 续推,L2 也丢则退 L3(SSOT)。

> 一致性与故障模型的**完整形式化**(控制面强一致 / 数据面最终一致、ref 两级、持久语义分层、风险窗口、F4 续推、GC reconcile)见 [`consistency.md`](consistency.md);本节是其中"写一次读多次 + 故障恢复点"的简述。

## 开放问题

- KV block 压缩:是否有跨 block 的低秩/量化压缩降低传输量?
- LFU-Aging 参数与前缀亲和加成取值,待 P7。
- 后台任务带宽池的内部优先级调度策略待定。
- 多租户隔离(远期预留,当前不实现):归外部控制面/部署切分;lake 侧未来若做,靠 `KVBlockID` 加 scope 维度(见 [`kv-cache-pool.md`](kv-cache-pool.md) "Block 寻址"预留、[`../features/features.md`](../features/features.md) F8)。
- **r-type 状态 checkpoint**:Mamba/卷积 recurrent state 落 L1+ 的 checkpoint 间距/形式、sliding window trailing pages 阈值,待实现/P7 校准。
- **block 粒度 128**:与传输/复用/写放大的权衡,待 P7 校准。
- **L1 副本来源:demotion 还是满块即写**(写放大 vs 命中率,P7 校准):
  - **现状(仿 SGLang)**:L1 副本只由 L0 demotion 产生(驱逐下沉 / 主动 demotion),满块写回只落 L2 durable,不写 L1。刚产出的 block 在 L0→L2 durable 之间,L1 是空的。
  - **SGLang 的选择**:`write_back` 模式驱逐即回写(`hiradix_cache.py::_evict_write_back`,D2H 到 host DRAM),节点 `host_value`="evicted but backuped";`write_through` 只写 L3(远端),不写 host DRAM。即 host DRAM(lake L1)副本始终靠驱逐,非产出即写。
  - **权衡**:demotion-only 写放大小,但满块到 demotion 之间若 L0 已让位、L1 还空,命中要从 L2 读(~10μs)。备选:满块写回时**顺便写一份 L1**,让热前缀立刻在 L1 命中(~μs,本机零网络),代价每满块写两层。agent 多轮热前缀复用频繁,从 L2 回填的延迟是否值得用写放大换,待 P7 测。
  - **生命周期图解**(block B = 首轮填满的第一个 128-token block):
    ```
    t0  prefill 产出 B(只在 L0)
        │ L0 (HBM) │ L1 (DRAM) │ L2 (NVMe) │ L3 │
        │   [B]    │           │           │    │   B 只在 L0;命中只能从 L0 读

    t1  B 填满 → 算哈希 → 写回 L2 (durable) → 注册 radix
        │ L0  │ L1 │  L2   │ L3 │
        │ [B] │    │ [B]✓  │    │   写回只落 L2,不写 L1;durable-first:落稳后才注册 radix,发布时后盾已落实

    t2  decode 继续,L0 满,B 被驱逐(demotion 到 L1)
        │ L0  │  L1   │  L2   │ L3 │
        │     │ [B]   │ [B]✓  │    │   L1 副本此刻才产生(demotion,非 t1 写回)
    ```
    - t1→t2 之间 L1 是空的:命中 B 时 L0 还活着 → 从 L0 读;若 L0 已让位而 demotion 未到 → 从 L2 读(~10μs)。
    - 两条路来源不同:写回→L2(持久化),demotion→L1(缓存)。故"写回 L2"与"L1 命中"不矛盾。
    - SGLang 对应:L1(HBM)=lake L0、L2(host DRAM)=lake L1、L3(远端)=lake L2/L3;host DRAM 副本同样靠驱逐产生。
