# 数据流与请求生命周期

本文把 [`execution-modes.md`](execution-modes.md) 的两条时序、[`scheduling.md`](scheduling.md) 的路由决策、[`compute-layer.md`](compute-layer.md) 的入图与 KV 管理、[`kv-cache-pool.md`](kv-cache-pool.md) 的跨实例传输，落成一条**完整的请求生命周期**。目标（P1 完成判据）：任一特性的"数据从哪来、写到哪、谁来调度、失败怎么办"都可在此找到答案。

本文以 **KV 为中心**展开，沿用 execution-modes 的"执行节点 A/B"中性称呼（P/D 是调度层角色分配，不进数据流）。

## 1. 请求生命周期主轴（happy path）

```
① Bifrost(Gateway) 入口准入(限流/鉴权/过载 shedding) ── 拒绝则不计推理系统失败率
        ↓ (已准入请求)
② Router 读本地命中视图镜像(零 RPC): (model_id, prompt 前缀)
        ← 可复用 block 列表 + 各自位置(含"是否已在某 HBM"= 本地命中判定) + 集群负载视图
        ↓ (5ms 模式选择预算内,本地内存查 + 本地纯函数决策)
③ 模式选择 f(请求, 集群状态) → (模式, 节点)        [纯函数,见 §2]
        ↓
④ 执行(按模式分支,见 §3;统一 ready/done 双 fence 一步契约)
        ↓
⑤ 产出写回池:满块→写回 L2 durable(NVMe,F4 恢复点)→注册 radix;尾块→请求结束写一次(见 §4)
        ↓
⑥ decode 延伸前缀? → 触发时序二反向回传(radix 生长,服务未来请求) + D→P(§3.4,延伸 KV 喂下一轮 prefill)
        ↓
⑦ 完成 → 响应
```

### 请求生命周期总图

```mermaid
flowchart TD
    gateway["Bifrost Gateway\n鉴权 / 限流 / 准入"] -->|"已准入"| router["Go Router\n读 view mirror + 选模式"]
    gateway -->|"过载拒绝"| rejected["拒绝\n不计推理系统失败率"]

    router -->|"D-direct"| direct["目标计算节点\n残差 prefill + decode"]
    router -->|"混部"| colocated["单计算节点\n完整或增量 prefill + decode"]
    router -->|"PD 分离"| prefill["Prefill 节点\n产出 KV"]

    prefill -->|"池发起 L0 到 L0 或经 L1 / L2"| decode["Decode 节点\nready / done"]
    direct --> writeback["PutEnd 写回\nL2 durable + radix register"]
    colocated --> writeback
    decode --> writeback

    writeback --> refs["ReportRef / RequestBarrier\n释放请求引用"]
    refs --> response["响应完成"]
    writeback --> futureHit["radix 生长\n服务后续前缀命中"]

    direct -.故障 / 超时.-> recovery["F4 恢复\nRouter 重跑模式选择"]
    colocated -.故障 / 超时.-> recovery
    decode -.故障 / 超时.-> recovery
    recovery --> router
```

**职责切分**：① 归 gateway（过载/限流/丢弃，见 [`../features/slo.md`](../features/slo.md)）；②③ 归 Router/控制面（路由 + 模式选择 + 优先级排队，不丢请求）；④ 归计算层（引擎零分层逻辑，只消费 ready→算→发 done）+ 存储池（放置/传输/写回，池 agent 发起）；⑤⑥ 归存储池。

## 2. 模式选择决策树

模式选择是 Router 的纯函数 `f(请求, 集群状态) → (模式, 节点)`，逐请求选路。**不设 mode-to-mode 降级阶梯**——失败即重跑此函数（见 §4）。结构如下，**具体阈值留 P7**：

```mermaid
flowchart TD
    Q[Router 读本地命中视图镜像:前缀复用 block + 位置 + 负载视图]
    Q --> L{前缀 KV 已被池预放置到\n某执行节点 HBM?\n本地命中}
    L -- 是 --> DD[D-direct:路由到该节点\n残差 prefill + decode,零传输]
    L -- 否 --> S{单节点完成划算?\nprompt 短 / 传输成本 > 分离收益}
    S -- 是 --> CO[混部:路由到一节点\n完整/增量 prefill + decode,本机完成]
    S -- 否 --> PD[PD 分离:时序二正向\nA prefill 产出 → 池搬到 B → B decode]
    DD --> R[decode 延伸前缀?\n多轮 agent]
    CO --> R
    PD --> R
    R -- 是 --> RB[触发时序二反向回传\n延伸 KV 回池,radix 生长]
    R -- 否 --> DONE[完成]
    RB --> DONE
```

**判定依据**（阈值 P7 校准）：
- **本地命中**：位置视图返回"前缀 block 已在某节点 L0"。→ D-direct，省传输。关键区分：Pool 命中（前缀 KV 在分布式池，省重算但需传输）≠ 本地命中（已在执行节点 HBM，零/极小传输直跳）。
- **单节点完成划算**：传输成本 > prefill 分离收益（短 prompt / 带宽紧张 / 各节点有空闲）。
- **跨节点传输划算**：prefill 大、各节点有空闲、带宽充裕，P/D 物理分离收益 > 传输成本。

决策树是结构定型的；阈值（本地命中判定边界、传输成本 vs 分离收益的拐点）留 P7 量化。

## 3. 三模式执行段（KV 流转详图）

三模式统一遵守 **ready/done 双 fence 一步契约**（见 [`compute-layer.md`](compute-layer.md)）：

```
池侧(步前): 定 read set/write set → 保证 read set 在 L0(缺则补拉) 
            → 给 write set 分配空闲 slot → 冻结被引用 slot(ref>0)
            → 组装完整 block table → 发 ready(fence)
引擎(步中): 拷 block table 进固定地址 tensor → replay graph → 新 token KV 写进 write slots
池侧(步后): 引擎发 done(compute fence) → 池解冻 → 满块写回 L2(NVMe)+注册 radix → 驱逐冷块 → 回收 slot
```

引擎的全部分层职责：**消费 ready → 算 → 发 done**。block table 由本地 agent 组装（in-process，持本地视图镜像），引擎不知地址、不组装 block table。

### 3.1 D-direct / 混部（时序一本地完成）

请求在单节点完成 prefill + decode，KV 全程本机 L0，无跨节点传输。

```
Router → 读本地命中视图镜像(本地命中判定) → 路由到目标节点
       → 本机 prefill(残差[D-direct] 或 完整/增量[混部]) → 本机 L0
       → 本机 decode(每步 ready/done 契约) → 增量 KV 异步写回池
```

- **D-direct**：前缀 KV 已被池**后台**预放置到本机 HBM（请求到达前完成），入口只做残差 prefill。零传输。
- **混部**：前缀 KV 仅在 Pool/未命中，本机完整或增量 prefill + decode，本机完成。省的是跨节点传输，非绕过存储池（本机 L0 仍归池管）。

两者共享同一条单节点时序。本地命中与否只决定 prefill 工作量，非数据流分支。

### 3.2 PD 分离（时序二正向：产出→消费，服务本次）

请求生命周期内某段 KV 要从 A 搬到 B。**engine-to-engine 控制链切断**——两个引擎从不知对方存在，池的本地 agent 发起传输，数据线仍直连 RDMA。

```
① A prefill 逐层产出 KV → 落 A 的 L0 slot(slot 由 A 的 agent 分配)
② A 每层产出 → A 的 agent publish 该层 page 切片 → 传输引擎在独立 stream 搬到 B
   (分块流水线:page_first_direct 子块,层算完即传该层切片,与 A 计算重叠,支撑 TTFT)
③ B 的 agent 查位置视图拿源地址 → 在 B 分配空闲 slot + 冻结 → RDMA 写入 → 返回 handle
④ B 的 agent 组装 block table(拉来的 slot + 已在 B 本地的 slot) → ready(fence) → B replay
⑤ B step done → publish 新 decode KV → 回到③(连续 batching)
```

- **默认直传**（A→B L0）：PD 时序重叠（A 边 prefill B 边 decode）主场景，省一跳、最低延迟。代价：A 的源 slot 被在途传输 ref 钉住、占 A 容量直到拉完。
- **经池中转**（A→池中段 L1/L2→B）：A 先结束/Drain 时，A 已 publish 到池中段（L1 DRAM 副本 / L2 NVMe），B 从池拉，时序解耦。
- 传输细节（内存注册、pull/publish、布局转换、在途 ref、Drain 推 L2）见 [`kv-cache-pool.md`](kv-cache-pool.md) "跨实例 KV 传输"。

### 3.3 反向回传（时序二反向：消费→池，服务未来）

B 在 decode 中延伸了前缀（生成新 token 的 KV）→ 这段延伸 KV 回传池 → radix 生长 → 未来请求（如下一轮 agent）查池时命中更长前缀。

```
B decode 生成延伸 KV → 异步回传池(落 L2 + 更新 radix)
                              ↓
                    前缀树生长,下次命中边界前推
```

反向回传**不为本次请求服务**，是为未来请求的前缀增强攒数据。agent 多轮的核心：每轮增长的 KV 回流，下一轮自动命中更长。与正向（服务本次）不可混作一谈。

### 3.4 D→P（decode 侧 KV 喂回 prefill，服务下一轮）

agent 多轮场景：第 N 轮 decode 产出的延伸 KV 是**第 N+1 轮 prefill 的输入前缀**。这条 KV 不必先落池再被下一轮 prefill 拉（绕一跳存储），可直接由 decode 侧喂回 prefill 节点——这是与 §3.3 反向回传并行的**第三条方向**：§3.2 P→D 服务本次、§3.3 D→池服务未来（攒数据）、**§3.4 D→P 服务下一轮**（直接喂回去）。机制即 [DualPath](../research/dualpath.md) 的 storage-to-decode 路径原生支持。

按"下一轮 prefill 所需 KV 是否已在 D 的 L0"分两子情况：

```
子情况 A(零存储读取,DualPath 不强调,我们独有):
   下一轮 prefill 所需 KV 全在 D 的 L0(= D 上轮 decode 自己产出、未下沉)
   → 连存储读取都省 → D L0 ──compute network RDMA──→ P L0 直传

子情况 B(DualPath storage-to-decode + CNIC 回传):
   所需 KV 部分需从池加载 → D 侧从 L1/L2 经 storage network 加载进 D 的 L0
   → 再经 compute network RDMA 传 P L0
   (借 D 侧存储带宽加载 + 高带宽 compute network 回传,绕开 P 侧 SNIC 瓶颈)
```

```
① D 上轮 decode 产出延伸 KV(落 D 的 L0) + 池异步反向回传(§3.3,radix 生长,与本节并行)
② 下一轮 prefill 落到某 P 节点 → P 的 agent 查位置视图:所需延伸 KV 在哪?
   - 在 D 的 L0(子情况 A)        → 直接从 D L0 拉,经 compute network RDMA 写入 P L0 slot
   - 需从 L1/L2 加载(子情况 B)    → 池调度选 D 侧加载(借 D 闲置 storage 带宽)→ D L0 → compute network → P L0
                                  (也可 P 侧自拉 = 传统 storage-to-prefill,池按 NIC 带宽视图选路)
③ P 的 agent 组装 block table(拉来的 slot + 已在 P 本地的 slot) → ready → P 增量 prefill
```

**关键点**：
- **engine-to-engine 控制链仍切断**（与 §3.2 同）：P 的 agent 查池位置视图定源、池发起传输，P 引擎不知 D 存在。子情况 A 的"零存储读取"不改变这点——只是源地址落在 D 的 L0 而非池中段 L1/L2，仍由池 agent 发起。
- **与 §3.3 反向回传的关系**：两者并行不冲突。§3.3 是 D→池（异步、为所有未来请求攒前缀、radix 生长）；§3.4 是 D→P（为紧邻的下一轮、直接喂）。子情况 A 的"KV 已在 D 的 L0"成立的前提，正是上轮 §3.3 已把这段 KV 注册进 radix（位置视图才知道它在 D 的 L0）。
- **选路归池**：子情况 A vs B vs 传统 P 侧自拉，由池按 NIC 负载/带宽视图决定（compute network / storage network 两类带宽是池的资源，见 [`kv-cache-pool.md`](kv-cache-pool.md) "双网络路径"）。这正是 [DualPath](../research/dualpath.md) "借 decode 闲置 SNIC + CNIC 回传"在我们架构里的等价——且因池统一管理而更彻底。
- **TP 场景**：per-rank 字段预留，单卡先行（见 [`compute-layer.md`](compute-layer.md) "TP"）。D→P 跨节点传输涉及多 rank 的 KV 切片归集，留 P7。
- **子情况 A 依赖 §3.3 的 radix 注册时效**：位置视图知道延伸 KV 在 D 的 L0，前提是上轮 §3.3 反向回传已把该段 KV 注册进 radix（满块路）。若上轮回传尚未完成（radix 生长有滞后，见开放问题），位置视图缺该位置 → 子情况 A 降级为 B 或 P 侧自拉（多一跳存储读取/加载）。**降级是安全的**（只是损失零存储读取收益），但 D→P 子情况 A 的成立与反向回传时效耦合。
- **D→P 的 L0→L0 直传也有在途 ref**：与 §3.2 PD 正向直传同——子情况 A 从 D 的 L0 读源、经 compute network 传 P 时，源 block（D 的 L0 slot）按跨实例传输的通用在途 ref 规则 +1 冻结、RDMA 完成 -1（源端冻结，防半传被覆写致损坏，见 [`kv-cache-pool.md`](kv-cache-pool.md) "引用计数与驱逐"/"PD 分离下的传输流程"）。D→P 不因"零存储读取"而豁免在途冻结。

## 4. 产出写回

一次请求的 KV 从产生到消亡（详见 [`kv-cache-pool.md`](kv-cache-pool.md) "写回与生命周期"）：

- **满块路**：block 填满 → 池算哈希 → 写回 L2 durable（NVMe，F4 恢复点）→ 注册 radix。请求进行中就可能触发（decode 跨 block 边界）。durable-first：落稳后才注册 radix；register 后到请求结束屏障之间持 writeback ref 不可驱逐（请求进行中冻结），请求结束是写回屏障（见 [`consistency.md`](consistency.md) §3）。满块写回频率 N 留 P7。
- **尾块路**：请求结束时未满的尾块 → 请求结束点写一次（写全部已填 token，重放整块覆盖），纯容错，不进 radix。

引擎不感知 block 满不满（block 对引擎纯寻址单位）——满块判断、哈希、radix 注册、写回全归池。

### PutEnd 写回与生命周期图

```mermaid
flowchart TD
    done["engine done fence\n新 KV 已写入 L0 slot"] --> full{"block 已满?"}
    full -- 否 --> tail["尾块\n请求结束点写一次"]
    full -- 是 --> hash["agent 计算内容哈希\n形成 KVBlockID"]

    hash --> admit["AdmitRegisterBlocks\nquota + namespace preflight"]
    admit -- reject --> discard["discard settled bytes\n不注册 radix"]
    admit -- admit --> durable["flush durable\n写入 L2 NVMe"]
    durable --> register["RegisterBlocks\nradix + location view"]
    register --> refs["writeback ref 保持冻结\n直到 RequestBarrier"]
    refs --> inactive["ref=0 后进入 inactive\n可被 GC / defrag / demotion 处理"]
    tail --> l2Recovery["L2 恢复点\n不进 radix"]
```

这张图补充 P4.3–P4.8 后的关键顺序：`AdmitRegisterBlocks` 必须先于 durable 写入,`RegisterBlocks` 必须晚于 L2 落稳；GC、defrag、shard 只操作已经在控制面 view 中可解释的位置。

## 5. F4 故障分支

执行失败（节点故障/超时）→ 触发 F4 → Router 依最新集群状态**重跑模式选择**（§2 纯函数）。**不设 mode-to-mode 降级阶梯**——模式选择是纯函数，失败即重跑该函数，由最新状态重新定模式与节点。

```
执行失败(故障/超时) → F4 触发 → Router 重跑 f(请求, 最新集群状态) → (新模式, 新节点)
                                          ↓
                        池把该 sequence 的已有 KV 放置到新节点 HBM → 续推
```

```mermaid
flowchart TD
    failure["worker / 节点故障或超时"] --> detect["存储池与 Router 接收故障信号"]
    detect --> freeze["控制面保留 sequence 引用\n避免恢复窗口被 GC"]
    freeze --> reroute["Router 重跑模式选择函数\n不走预设 fallback 链"]
    reroute --> locate["Authority 查询已有 KV\n优先 L2 恢复点 / L3 SSOT"]
    locate --> place["池将 KV 放置到新节点 L0\n必要时从 L1 / L2 / L3 拉取"]
    place --> resume["新 worker ready\n继续推理"]
    resume --> barrier["完成后 RequestBarrier\n释放旧请求引用"]
```

**worker 崩溃续推**：
- 存储池检测 → 把该 sequence 路由到新节点 → 池把已有 KV（L2 F4 恢复点）放置到新节点 HBM → 续推。
- ref 从原请求**转移**到新请求（避免被冷热淘汰，见 [`kv-cache-pool.md`](kv-cache-pool.md) "引用计数与驱逐"）。
- 原节点 HBM 副本随销毁失效（本就是易失副本，非私有状态）。
- 丢失的仅是最后一次写回 L2 之后的少量 token（NPU/进程级故障，NVMe 不波及、block 无论本机远端均存活）。

**持久语义与风险窗口**（见 [`kv-cache-pool.md`](kv-cache-pool.md) "故障恢复"）：
- L2(NVMe) = F4 恢复点（NVMe 持久 + NPU 故障不烧 NVMe，恢复能力与位置无关）；L3 = SSOT 永久权威（抗整机级/池级失败）。
- 风险窗口分两级：NPU/进程级故障（常见）丢"最后一次写回 L2 之后的少量 token"；整机级故障（罕见）退 L3 SSOT，丢"自上次冷下沉 L3 之后的增量"，冷下沉 L3 频率由冷热生命周期决定，非每步。

**过载不在此分支**：过载 shedding 归 gateway（①），不计推理系统失败率；推理系统只上报信号（队列长度/in-flight/剩余容量）供 gateway 决策。

## 6. 职责边界速查

| 关注点 | 归属 | 见 |
|--------|------|-----|
| 入口准入 / 限流 / 过载 shedding / 丢弃 | gateway | [`../features/slo.md`](../features/slo.md) |
| 路由 / 模式选择 / 优先级排队（不丢请求） | Router / 控制面 | §2、[`scheduling.md`](scheduling.md) |
| 节点角色分配（P/D/draft） | 调度层 | [`scheduling.md`](scheduling.md) |
| KV 放置 / 冷热 / 生命周期 / radix / 位置视图 | 存储池 | [`kv-cache-pool.md`](kv-cache-pool.md) |
| block table 组装 / L0 内存注册 / 传输发起 | 存储池本地 agent（in-process） | [`compute-layer.md`](compute-layer.md)、[`kv-cache-pool.md`](kv-cache-pool.md) |
| KV 跨节点传输执行 | 存储池数据面（池 agent 发起，非引擎） | [`kv-cache-pool.md`](kv-cache-pool.md) "跨实例 KV 传输" |
| 前向计算（graph replay）| 计算层引擎（零分层逻辑） | [`compute-layer.md`](compute-layer.md) |
| 产出写回触发 / 频率 N | 存储池（基于引擎 publish） | §4 |
| F4 故障恢复 / 续推 | 存储池 + Router 重路由 | §5 |

## 7. 开放问题

- 模式选择决策树阈值（本地命中判定、传输成本 vs 分离收益拐点）待 P7。
- 满块写回频率 N、分块流水线深度、反向回传 radix 生长时效待 P7。
- continuous batching 与 KV 跨节点迁移协同（迁移中的 sequence 如何处理）。
- D→P 选路（§3.4 子情况 A/B 与 P 侧自拉的 NIC 带宽视图决策）待 P7。
