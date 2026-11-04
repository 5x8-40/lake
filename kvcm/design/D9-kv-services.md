# D9: 控制面：执行与运营能力

- 状态: **设计草案**（能力边界与优先级；收益为模型估算，待真机校准）
- 版本基线: **Mooncake v0.3.13.post1**（master / client 同版本）
- 上游: [summary.md](../summary.md) D9；[D2-kv-operations.md](D2-kv-operations.md) 的操作集；管理面（调度）：[D8](D8-joint-scheduling.md)
- 适用部署: **vLLM-Ascend + Mooncake Store + kvcm** 独立部署
- 关系声明: **kvcm 是独立项目，本文不依赖 lake 运行时**；lake 文档仅作参考
- 一句话: **D8 管“决定做什么”，D9 管“怎么执行、怎么核验、怎么记账、怎么运营”**。会话/预热/选路/扩缩是 D8 的管理面动作，本文不重复；本文只列控制面自己的独立能力。

---

## 0. 结论

1. kvcm 控制面做四件事：**执行、核验、记账、发布**；不做选路、不做扩缩、不持有前缀索引。
2. **会话 KV、预热不是“调度之外的另一套系统”**：它们是 D8 管理面的 L1 动作（会话保留/预取），由 D8 决策、控制面执行。本文只在服务地图里画一条“执行支撑”的边，不再重复列举和收益。
3. 真正“除了调度中心还能做什么”的是控制面的**独立运营能力**：KV 对账、用量账本、容量规划建议、下线搬迁、保留/删除规则、历史回放、会话分支/跨地域/模型灰度。
4. 优先级：**P0 KV 对账 + 用量账本**；P1 容量规划建议 + 下线搬迁 + 保留/删除规则；P2 历史回放 + 会话分支/跨地域/模型灰度。会话/预热的优先级归 D8（P0 命中/保留）。
5. 设计依赖的能力前提见 §6；具体事实（每操作有没有 RPC、什么边界）在 [D2](D2-kv-operations.md)，本文不重复。

### 0.1 直接回答：除了调度中心，控制面还能做什么

| 优先级 | 能力 | 一句话 | 关键技术/机制 | 主要收益 |
|---|---|---|---|---|
| **P0** | **KV 对账（反熵/核验）** | 索引说有的，池里真的还有 | 热/老条目抽样 + `batch_query_keys` 零副作用查询 | 降 ghost、保命中率、让 ROI 可信 |
| **P0** | **用量账本（账本/归因）** | 谁花了多少、省了多少、值不值 | 任务/会话/字节/ROI 记录 + 命中归因 | 成本可见、预算可调、可计费 |
| **P1** | **容量规划建议** | 池该多大、HBM/DRAM/SSD 怎么配 | working set + Little 定律 + admin 容量 | 少扩/少浪费、容量预测 |
| **P1** | **下线搬迁（Drain/迁移）** | 缩容/维护不丢热 KV | `CreateDrainJob` / move / verify / 宽限期 | 缩容不砸命中率、不丢请求 |
| **P1** | **保留/删除规则（治理）** | pin/TTL/模型版本/删除可控 | retention class + 预算 + 审计 + 命名空间 | 防滥用、防误删、合规 |
| **P2** | **历史回放/策略试算** | 调参不拿生产试错 | 决策日志 + 账本 + 反事实重放 | 策略迭代、事故复盘 |
| **P2** | **会话分支 / 跨地域 / 模型灰度** | 分支、跨区、模型发布 | copy/DFS/命名空间/保留 | 远期能力 |

> **不在本表**：会话 KV、预热、选路、扩缩。它们是 D8 管理面的动作；控制面只为它们提供 copy/keepalive/pin/verify 等执行接口，不再当成第二套能力。

### 0.2 人话解释：这些词到底在说什么

| 名词 | 人话 | 一个例子 | 不做会怎样 |
|---|---|---|---|
| **KV 对账（反熵）** | 定期抽查“索引说有的 KV，池里是不是真的还有” | Router 索引说 B 有前缀 P；B 的副本其实已被驱逐。对账发现后把 B 的条目标成失效 | 请求被发到空节点，跨机读/重算；命中率统计虚高 |
| **用量账本（归因）** | 给每次预热/保留/迁移记账：花了多少、省了多少 | 预热 100 次，只有 20 次被用上；账本算出 ROI，决定下次只预取高价值前缀 | 不知道钱花在哪，预算拍脑袋，无法判断预热值不值 |
| **容量规划建议** | 根据历史用量算“池子该多大、HBM/DRAM/SSD 各多少” | 热前缀 2TB、温 10TB、冷 50TB → 建议 HBM/DRAM/SSD 配比和节点数 | 扩容靠猜：不是不够就是过度采购 |
| **下线搬迁（Drain）** | 节点要停之前，把上面的热 KV 搬到别的节点/落盘 | 缩容节点 C：先摘流，再把 C 的会话前缀 move 到 D，确认搬完再停 C | 直接停机会丢热 KV，缩完命中率掉、再扩回来 |
| **保留/删除规则（治理）** | 规定谁能保留多久、最多占多少、删之前要验什么 | 某会话 pin 了 50GB，超过配额就降级；删之前校验 model/revision，防误删别的模型 | pin 滥用占满池子；跨模型误删；没有审计 |
| **历史回放/策略试算** | 用历史请求重跑新策略，先看效果再上线 | 把 TTL 从 10 分钟改 30 分钟，先回放一周流量，看命中率/成本变化 | 只能线上试错，出事难复盘 |
| **会话分支 / 跨地域 / 模型灰度** | 会话分叉、异地副本、新模型小流量先上 | 分支共享前缀、各自增量；新 revision 先 5% 流量，旧 revision 逐步下线 | 分支/跨区/发布只能手工做，容易出错 |

> “反熵”是分布式系统术语（熵=混乱，反熵=对抗系统越来越乱）。日常沟通建议直接叫 **“KV 对账”**，本文两种写法都保留。

### 0.3 现成组件能做什么，我们补什么

这些能力不是“从零自研”，大部分有现成组件。关键区别是：**组件给的是“零件”，不是“这个拓扑下的成品”**。

| 能力 | 现成组件 | 已覆盖 | 还缺的“胶水” |
|---|---|---|---|
| **KV 对账** | Mooncake Conductor（`prefix_indexer.cpp::PrefixCacheTable::Query/Register`）建前缀索引；Dynamo standalone indexer（`listener.rs::handle_gap`）做事件/gap; Mooncake admin `GET /batch_query_keys` 给副本真相 | 索引、查询、gap replay、副本查询 | 抽样策略（热/老/可疑）、索引 vs master 的比对循环、`LOST/REMOVED` 失效事实回写、限速/HA |
| **用量账本** | LMCache `lmcache/observability.py::LMCStatsMonitor`（本地/远端/存储字节、请求统计）；Prometheus/OTel 存指标；AIBrix 有 usage/TPM 类指标 | 字节/请求级指标、监控存储 | 把 intent、copy bytes、READY、命中、引擎 APC 命中串成一条链；算预热 ROI/归因；这不是监控系统自带的语义 |
| **容量建议** | Dynamo Planner、llm-d WVA、AIBrix PodAutoscaler；Mooncake 容量/水位指标 | 扩缩算法、指标源、目标副本数 | 池侧 KV footprint/tier mix/shard 建议；和 D8 的决策接口对齐（只给事实/区间） |
| **下线搬迁** | Mooncake `CreateDrainJob`、`CancelDrainJob`；K8s drain | seg 级搬迁、job 级取消 | 与 D8 摘流/选 victim 联动；session-aware；搬完 verify 再停实例 |
| **保留/删除规则** | Mooncake pin/lease/tenant quota；K8s ResourceQuota | 单 key/tenant 的容量与租约 | 跨模型的保留策略、pin 预算、删除审计、ROI 约束 |
| **历史回放** | 没有 turnkey；可用 trace + 模拟器自建 | 事件/指标存储 | 决策日志 schema、反事实重放、策略版本对比 |

结论：

- **KV 对账**：索引和查询可以复用 Conductor/Dynamo/Mooncake admin；我们补的是“抽样比对 + 失效回写”，不是重写索引。
- **用量账本**：监控和字节统计可以复用 LMCache/Prometheus/OTel；我们补的是“intent → 命中”的归因语义，不是重写 TSDB。
- **容量/下线/治理**：算法和池操作大多有现成实现；我们补的是“和 D8 管理面/会话视图的联动”。
- 所以 D9 的价值不是“发明新轮子”，而是**把现成轮子装到这条链上**。

---

## 1. 控制面服务地图

```mermaid
flowchart TB
    subgraph D8["D8 管理面（决策）"]
        L1["会话 / 预热 / 选路 / 扩缩"]
    end

    subgraph D9["D9 控制面（执行 + 运营）"]
        OPS["八项操作<br/>copy/move/keepalive/pin/demote/promote/retain/remove"]
        VER["KV 对账<br/>（反熵/核验）"]
        LED["用量账本<br/>（账本/归因）"]
        CAP["容量 / 放置顾问"]
        DRA["Drain / 迁移"]
        GOV["治理"]
        SIM["回放 / 仿真"]
        EXT["分支 / 跨区 / rollout"]
        ADM["master admin"]
    end

    L1 -->|"intent"| OPS
    VER --> ADM
    LED --> OPS
    LED --> ADM
    CAP --> LED
    CAP --> ADM
    DRA --> OPS
    GOV --> OPS
    SIM --> LED
    EXT --> OPS
    OPS --> M[("Mooncake Store")]
    ADM --> M
```

读法：

- **左侧 D8** 是决策：会话保不保、预取到哪、请求去哪、扩不扩。
- **右侧 D9** 是控制面：执行这些决定（八项操作），并提供 KV 对账、用量账本、容量建议、下线搬迁、保留/删除规则、回放、分支等运营能力。
- 会话/预热不画成 D9 的独立服务；它们只通过 `L1 → OPS` 这条执行边出现。

---

## 2. 边界：为什么这些能力放控制面

- **它离池最近**：copy/move/keepalive/pin/demote/promote 都是池操作，只有控制面能执行。
- **它有台账**：任务、保活、副本、会话四类状态已在 ledger（D2 L5）；换任何上层都能复用。
- **它能核验**：`GET /batch_query_keys` / `GetReplicaListForAdmin` 零副作用；这是 KV 对账和 READY 的基础。
- **它是独立进程**：不随引擎实例生死；会话/保留可以跨 Router 重启。
- **它不该做的**：选路、扩缩决策、前缀索引——这些归 D8 管理面；控制面越界会把后台服务拖成在线决策器。

---

## 3. 控制面独立能力

### 3.1 KV 对账（反熵/核验）

**问题**：引擎事件看不到池副本删除；KVT 只增不减；ghost 条目把请求导向无副本实例。0.3.13.post1 的 publisher 默认不开，删除面更依赖 KV 对账。

**终局形态**：

```mermaid
flowchart TB
    S["采样：热 + 老 + 高 ghost 风险"] --> Q["查询：batch_query_keys / ForAdmin"]
    Q --> C{"副本还在?"}
    C -->|"在"| U["更新 as_of / confidence"]
    C -->|"不在"| E["发 LOST/REMOVED"]
    E --> K["D8 的 KVT 降级/清条目"]
    U --> S
```

**技术/机制**：

- 按 `hotness × age × P_ghost` 抽样，不全量 diff；
- 用零副作用 admin 查询；查询失败与“副本丢失”分开；
- 输出去重后的 `LOST/REMOVED/MOVED` 事实，供 D8 KVT 消费。

**收益**：

```text
score(b) = hotness(b) × age(b) × P_ghost(b)
P_ghost(b) ≈ 1 − e^{−λ_delete × age(b)}
```

- 降低 ghost 率，避免无效路由；
- 让预取/保留 ROI 可计算；
- 是事件索引可运维的前提。

**限制**：全量 diff 太贵；只做抽样/热条目；查询 QPS 要预算。

**优先级：P0。**

### 3.2 用量账本（账本/归因）

**问题**：预取/保留/迁移花了带宽和内存，但答不出“哪个模型/会话/前缀在浪费、ROI 多少、TTFT 改善是谁贡献的”。

**终局形态**：

```mermaid
flowchart LR
    I["Intent"] --> L["Ledger"]
    T["Task/READY"] --> L
    H["命中/请求结果"] --> L
    L --> A["聚合/归因"]
    A --> D["Dashboard / 预算 / 计费"]
    A --> C["容量顾问"]
```

**技术/机制**：

- 记录 intent、copy/move、bytes、耗时、READY/FAILED、保留 TTL、命中结果；
- 归因：`hit = engine_apc + local_pool + remote_pool + prewarm + retain`；
- ROI：`saved / cost`；浪费：预取未用、保留未恢复、迁移失败；
- 成本：GPU 时间、带宽、驻留字节。

**收益**：

- 让“保留多久/预取多少”有数可依；
- 支持容量顾问、预算、外部计费；
- 解决“预热贡献 vs 引擎 cache”的归因问题。

**限制**：请求命中结果需要 D8/worker 回传；没有回传只能估；多模型/租户归因依赖命名空间。

**优先级：P0。**

### 3.3 容量规划建议（容量/放置顾问）

**问题**：扩缩决策在 D8，但 D8 只看请求侧；控制面看得到池内副本、容量、驱逐、迁移。

**终局形态**：

```mermaid
flowchart TB
    L["Ledger：字节/副本/迁移"] --> M["模型：working set / tier mix"]
    A["Admin：容量/水位"] --> M
    W["Workload：QPS/ISL/OSL/复用"] --> M
    M --> P["Plan：节点数/配比/预热排期"]
    P --> R["D8 / 外部 HPA"]
    R --> F["反馈：SLO/命中/水位"]
    F --> M
```

**技术/机制**：

- working set、hot/warm/cold、HBM/DRAM/SSD 配比；
- 池节点数：`N = ceil(working_set / usable_per_node)`；
- 预热排期按 hook/热度/预算；
- what-if：不同保留策略/配比下的命中率与成本。

**边界**：只给建议/区间，不直接扩缩；决策归 D8。

**优先级：P1。**

### 3.4 下线搬迁（Drain/迁移）

**问题**：缩容/维护/rollout 时，KV 要搬走或保留；搬不走的会被引擎下线带走。

**终局形态**：

```mermaid
sequenceDiagram
    participant R as D8
    participant K as kvcm
    participant M as Mooncake
    participant O as 编排

    R->>R: 选 victim + 摘流
    R->>K: DrainPlan(victim, targets, deadline)
    K->>M: CreateDrainJob / move
    loop 进度
        K->>K: 查询/重试/verify
    end
    alt 成功
        K-->>R: DRAINED
        R->>O: remove(victim)
    else 卡住
        K-->>R: 部分失败
        R->>R: 保留 draining，下周期重试
    end
```

**技术/机制**：

- seg 级 `CreateDrainJob`（job 级可取消）或逐 key move/copy；
- 宽限期卸载（`GracefulUnmountSegment`）作为迁移时间窗；
- 失败保留 draining，不硬删；
- 与 D8 摘流、编排停实例配合。

**收益**：缩容不丢热 KV；避免“缩完再扩、命中率掉”。`T_drain ≈ bytes/BW + tasks/concurrency + verify`。

**限制**：单任务无取消；源必须在线；retain 未实现；DFS 依赖部署。

**优先级：P1。**

### 3.5 保留/删除规则（治理）

**内容**：

- retention class/TTL；pin 预算；模型/revision 隔离；删除审计；配额/借用；安全删除。
- 默认软释放；Remove 前按台账校验 model/revision；hard pin 有上限；所有删除可审计。

**收益**：

- 防 pin 滥用、防跨模型误删、满足合规；
- 让“保留”成为可控成本，而不是无限占内存。

**优先级：P1。**

### 3.6 历史回放/策略试算（回放/仿真）

**能力**：

- 记录 hook、事实、决策、intent、READY、命中、延迟；
- 回放：同输入重跑策略；
- 反事实：换权重/TTL/warm gate，看命中率/成本/尾部延迟。

**收益**：

- 调参不拿生产试错；
- 事故复盘；容量 what-if；
- 策略版本可比较。

**限制**：依赖 D8 决策日志完整；仿真精度取决于负载/时延模型。

**优先级：P2。**

### 3.7 会话分支 / 跨地域 / 模型灰度

- **分支**：共享前缀 + 独立 delta；分支关闭只释放独有块；Mooncake 整对象 copy，不做块级 CoW。
- **跨集群**：DFS/对象存储副本（main 能力；0.3.13 需确认部署）；首版不做。
- **模型 rollout**：新 revision 独立命名空间、预热、灰度、旧 revision 失效/GC；防旧 KV 误命中。
- 优先级：**P1/P2**。

---

## 4. 收益模型

> 会话/预热的收益在 D8 §7 已经算过（命中/保留省秒级 prefill、本地放置省十毫秒级传输），本文不重复。这里只算控制面独立能力。

### 4.1 能力收益对照

| 能力 | 主要收益 | 量级 | 成本 |
|---|---|---|---|
| KV 对账 | 降 ghost、保命中率 | 间接（百分点） | 查询 QPS |
| 用量账本 | 让预算/ROI 可算 | 间接 | 存储/聚合 |
| 容量顾问 | 少扩/少浪费 | 容量百分点 | 计算/查询 |
| Drain | 缩容不丢热 KV | 避免命中率断崖 | 迁移带宽 |
| 治理 | 防滥用/误删 | 风险 | 策略/审计 |
| 回放 | 调参效率 | 间接 | 日志/仿真 |

### 4.2 公式

```text
# KV 对账采样
score(b) = hotness×age×P_ghost
P_ghost ≈ 1−e^{−λ_delete×age}

# 容量
N = ceil(working_set / usable_per_node)
c_min = ceil(λ/(μ×ρ_target))

# Drain
T_drain ≈ bytes/BW + tasks/concurrency + verify

# 归因
hit = engine_apc + local_pool + remote_pool + prewarm + retain
ROI = saved / cost
```

### 4.3 结论

- **KV 对账/用量账本不是“性能功能”**，但没有它们，D8 的命中/保留决策不可信、不可调。
- **容量/Drain** 是“保命中率”的防守型收益；不做会在扩缩时把前面赚的吐回去。
- **治理** 让保留/删除可控；**回放**让调参可回归。

---

## 5. 优先级与路线图

```mermaid
flowchart LR
    P0["P0<br/>KV 对账 / 用量账本"] --> P1["P1<br/>容量建议 / 下线搬迁 / 规则"]
    P1 --> P2["P2<br/>回放 / 分支 / 跨地域 / 灰度"]
```

| 优先级 | 能力 | 理由 |
|---|---|---|
| **P0** | KV 对账 | 事件索引可信的前提 |
| **P0** | 用量账本 | ROI/预算/归因 |
| **P1** | 容量规划建议 | 扩缩需要池侧事实 |
| **P1** | 下线搬迁 | 缩容/维护必须 |
| **P1** | 保留/删除规则 | 防 pin 滥用/误删 |
| **P2** | 历史回放/策略试算 | 调参需要 |
| **P2** | 会话分支/跨地域/模型灰度 | 远期 |

> 会话 KV、预热不在此表：它们归 D8 管理面（L1），控制面只提供执行接口。

---

## 6. 能力前提

D9 的能力都依赖具体机制。**事实写在各处，本文不重复**：

| 要知道什么 | 去哪看 |
|---|---|
| 每个操作的 RPC、前提、版本差异 | [D2](D2-kv-operations.md)（§2 soft pin / §3 demote-promote / §4 retain / F11b 核验通道） |
| 池的事件、放置权威、lease 语义 | [D1](D1-router-scheduling.md) |
| kvcm 实现状态、工程项（E13/E15 等） | [summary.md](../summary.md) |

影响 D9 的四条前提：

1. **soft pin 有条件**（Case B）。治理规则不能假设"随时能 pin"；不满足就退 keepalive。
2. **hard pin 不能事后加**。所以「保留等级」里 PIN 一档只能用 lease 延长模拟。
3. **retain 未实现**。跨引擎存亡要靠 move 到活 seg 或自挂 SSD，Drain 和 HA 的能力上限受此约束。
4. **删除面没有原生事件**（publisher 默认关、copy 路径零发布）。所以 KV 对账是 P0，不是可选项。

---

## 7. 参考实现

> kvcm 独立于 lake；下表是“可借鉴的机制”，不是依赖。

| 参考 | 锚点 | 借了什么 | 关键差异 |
|---|---|---|---|
| **kvcm D2** | `D2-kv-operations.md` §1/§4 | 八项操作、READY、状态四类、retain/release | 本文把控制面组合成运营能力 |
| **SGLang session** | `mem_cache/unified_cache/session_ref_tracker.py::UnifiedSessionRefTracker` | session 软保护、frontier 引用 | 会话是 D8 L1；控制面只执行保留 |
| **vLLM Retention** | `v1/kv_hints/protocol.py::KvHintsEnvelope`；Retention API（#37003） | pin/retain/prefetch/demote/release 动词表 | 管理面决策，控制面执行 |
| **KVCR Guard** | `src/kvcr/remote_fw_dram.py::fetch/query/_try_release_pin`；Guard 组件 | 传输窗口 pin、旁挂保活 | kvcm 集中式 + master 权威，不需要 fencing |
| **TensorCast** | `tensorcast/api/_config.py::StorePolicyProfile` | cache/durable/ha/cold/warm/pinned 策略预设 | 用作 retention class 命名，不照搬存储后端 |
| **Dynamo Planner** | `components/src/dynamo/planner/core/load_scaling.py::LoadScalingMixin`；`throughput_scaling.py` | 慢/快双环、SLA 驱动 | 容量顾问只出事实/建议，决策归 D8 |
| **llm-d WVA** | `llm-d-autoscaling`（项目级） | SLO 感知的目标副本数、变体成本 | 本文只做池侧容量事实，不替代 WVA |
| **AIBrix PodAutoscaler** | `pkg/controller/podautoscaler/context/context.go`（`gpu_cache_usage_perc`） | KV 使用率/排队长度扩缩指标 | 决策在 D8，执行在外部 |
| **Mooncake** | `master_service.cpp::CreateDrainJob`、`CopyStart`、`GetReplicaListForAdmin` | drain job、copy 幂等、零副作用核验 | kvcm 封装，D8 不直连 |
| **LMCache controller** | `lmcache/v1/cache_controller`（lookup/pin/move/clear） | 操作集对照 | LMCache 无全局强一致元数据；kvcm 有 master 权威 |

---

## 8. 开放问题

1. **KV 对账成本**：0.3.13.post1 的 admin 查询零副作用，但采样率/查询 QPS 预算怎么定？
2. **用量账本**：请求命中结果从 D8/worker 回传的字段与延迟；多模型/租户怎么切？
3. **容量顾问的边界**：建议与 D8 决策如何避免“两个大脑”？建议只出事实/区间。
4. **Drain**：单任务无取消；retain 未实现时，跨引擎存亡只能靠 move 到活 seg / 自挂 SSD。
5. **治理**：pin/TTL 预算、模型版本、删除审计的默认策略。
6. **回放**：D8 决策日志的字段与采样率；仿真精度。
7. **分支/跨区/rollout**：DFS 在 0.3.13 的部署确认；跨区事件/带宽。

---
