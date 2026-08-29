# 07 — 一致性与故障模型

本文把散见于 [`storage-layer.md`](storage-layer.md) "一致性模型"、[`kv-cache-pool.md`](kv-cache-pool.md) "引用计数与驱逐"/"写回与生命周期"/"故障恢复"、[`scheduling.md`](scheduling.md) "调度器一致性"、[`data-flow.md`](data-flow.md) §5 的内容，收敛成一篇**自洽的一致性与故障模型**。目标：任一 KV block 的"谁写、谁读、何时一致、崩了丢什么、怎么恢复"在此有唯一答案。

## 1. 一致性分层：控制面强一致 / 数据面最终一致

| 面 | 内容 | 一致性 | 机制 |
|----|------|--------|------|
| 控制面 | KV block 位置元数据、radix 前缀树、节点拓扑、配额、引用汇总 | **强一致** | 控制面进程内存（权威）+ etcd（降频 checkpoint） |
| 数据面 | KV 字节、权重字节、跨节点传输、本地命中视图镜像 | **最终一致** | RDMA/TCP 直传 + 推送刷新 |

**为何分两面**：hot step loop 每步都要定位/组装 block table，不可能每步 RPC 强一致查控制面（撑不住 5ms 预算）。故**正确性地基**（block 在哪、是否可驱逐、radix 结构）归控制面强一致；**性能路径**（字节搬运、本地决策）归数据面最终一致，读镜像 + miss 兜底。

- **控制面是权威**：位置视图、radix、引用汇总的唯一真相在**存储控制面进程内存**（单写者线性一致）。**etcd 不承载高频位置写**——满块注册（每次 prefill 新块都触发）是高频写，进 etcd 会被 Raft 复制 + watch 放大拖垮；etcd 只存**降频 checkpoint**（节点/模型/配额/revision + 位置快照），供控制面崩溃重建、Router 冷启动建镜像、stream 断时回退。强一致权威在内存而非 etcd 的依据见 §8。
- **数据面读镜像**：agent / Router 各持本地镜像（零 RPC 决策），由控制面**推送**刷新（gRPC stream；同机可用共享内存直读），触发 = 位置视图权威变更（放置 / 驱逐覆写 / 迁移 / 满块注册）。详见 [`scheduling.md`](scheduling.md) §1 前缀解析、[`control-plane.md`](control-plane.md) Router 镜像节。
- **陈旧只损性能不损正确性**：镜像滞后导致误判本地命中 → agent pull 向控制面确认 → miss 则从池（L1/L2，未命中退 L3）回填（多一跳）。热点前缀变动少、基本不陈旧；陈旧风险集中在冷门 block，miss 代价也小。
- **规模演进预留**：全量镜像撞线后（见 [`control-plane.md`](control-plane.md)「镜像的规模上限与演进」），冷前缀选路查询回源控制面权威——一致性语义不变（权威查询本就线性一致），仅选路查询的分布从"全本地"变为"热本地 + 冷回源"。

> **参考对照**：LMCache 一致性节明示"无全局强一致"，靠 controller ZMQ 消息 + 心跳 + 序列号 + `RWLockWithTimeout`、full-sync best-effort（`sharing-and-backends.md`）；Mooncake 元数据全在 leader 内存、etcd 仅存 OpLog（`mooncake/kv-store.md` MasterService）。lake 的位置视图权威在控制面进程内存强一致（单写者线性一致），etcd 降频做持久后盾——既不靠 best-effort 复制（强于 LMCache），又不在高频写上压 etcd（学 Mooncake 把热元数据留内存的思路，但 lake 有全局权威而非 per-instance）。

## 2. 写一次读多次（KV immutability）

KV cache 是**写一次读多次**的数据：

- **单个 token range 的 KV 不可变**：某 token 位置的 KV 一经 prefill/decode 产出写入 block，永不被原地改写。后续 token 的 KV 是**新 block**，不存在多写者并发改同一字节。
- **写者单一**：满块路（block 填满 → 池算哈希 → 写回 L2 durable → 注册 radix，NVMe F4 恢复点）与尾块路（请求结束写一次、整块覆盖）都由池单写者执行，只需**单写者屏障**，无需多写者并发控制。对齐 vLLM `ExternalBlockHash`（只对完整 block 算哈希）。**durable-first**：L2 写回完成后才注册 radix 发布视图（对齐 Mooncake `PutEnd` COMPLETE 前字节已落稳、Dynamo transfer settled 后才 register presence；SGLang 反向 register-先靠 `host_value`+`BlockRemoved` 补偿，代价更高）。radix 发布时后盾已落实，无需防"radix 有、durable 无"悬空；register 后到请求结束屏障之间靠 writeback ref 冻结驱逐（见 §3）。
- **读者多个**：同一 block 可被多节点同时读——本地命中复用、跨节点直传源（D→P / PD 正向）、回填。读不阻塞写回、不阻塞驱逐（驱逐前先确认无在途 ref 且 L2 已 durable，见 §3）。

> **参考对照**：Mooncake `PutStart`→`PutEnd` 两阶段写 + `Get` 只读 `COMPLETE` 状态副本（`master_service.cpp`），对象写后 immutable、`Get` "not necessarily the latest"。lake 的"写一次读多次"是其 immutable 语义的更彻底版：block 一旦注册 radix 即永久可读，无"latest"歧义（radix 命中的是确定的前缀链哈希）。

## 3. 引用计数分两级（B1 闭环）

ref 是"正确性地基"——决定 step 期间冻结、可驱逐性、GC 真删。分两级，**解耦"每 step 高频"与"低频全局"**，避免 per-step 强一致撑不住预算：

### 第一级：本地引用计数（池本地 agent，请求级，高频）

同 vLLM `block_pool.py::free_blocks`/`touch`、sglang `radix_cache.py::inc_lock_ref`/`dec_lock_ref` 的机制——归属从引擎进程改为**池的本地 agent**（存算分离、block 归池权威、多引擎共享，ref 不能放某引擎进程内）。引擎只通过 read set/write set 间接表达引用，不持计数。

- **请求引用**：block 进入某请求 read set 时 +1。减点只在**请求结束且无续推引用**时（attention 每步读全部 KV，前缀 block 全程 in-flight，不能中途早减）。F4 续推时 ref 不归零而是**转移到新请求**，避免被淘汰。
- **在途传输引用**：跨实例传输发起时源 block +1（源端冻结，防 RDMA 半传被覆写致损坏）；完成 -1。D→P 子情况 A 的 L0→L0 直传同样有在途 ref（不因"零存储读取"豁免，见 [`data-flow.md`](data-flow.md) §3.4）。
- **写回引用（writeback ref，请求进行中冻结驱逐）**：满块 L2 写回 durable 后注册 radix（§2 durable-first），register 后到请求结束屏障之间 block 持 writeback ref，**不可驱逐**。因 radix 发布时 L2 后盾已落实，writeback ref 防的不是"radix 有、durable 无"悬空（durable-first 已消除该窗口），而是**请求进行中、barrier 未完成前**的驱逐保护——保证 radix 已发布的 block 在 barrier 落定前不被驱逐，对齐 Mooncake COMPLETE 后 lease、SGLang `lock_ref`。**请求结束是写回屏障**：flush+ack 该请求所有已注册 block 的 L2 写回，writeback ref 随屏障完成归零（解冻晚于 barrier）。
  - **参考对照**：SGLang `write_back` 模式驱逐即回写（`hiradix_cache.py::_evict_write_back`），TreeNode 的 `host_value` 即"evicted 但 backuped"——节点驱逐时先备份到 L2 再摘 L1，恒带 durable 后盾。lake 因 HBM 归池、ref 跨请求/跨节点，驱逐不再是唯一回写点（写回频率 N 可提前批写），改用 **writeback ref + 请求结束屏障**实现等价不变量。
  - **与 F4 风险窗口的关系**：writeback ref 只阻**正常路径驱逐**；F4（worker 崩溃）是节点销毁不是驱逐，L0 随故障丢失，未 durable 的 block 仍按"丢最后一次写回 L2 之后的少量 token"重算（见 §4）。即未 durable 的 block 只能经 F4 路径丢失，正常路径不会让它在 radix 里裸奔。
- **ref 归 0 ≠ 删内存，而是"可驱逐候选"**：对齐 vLLM `free_block_queue` / sglang `evictable_size_`——归 0 后 block 还在 HBM，可被前缀命中复用、可作传输源。真正释放 slot 只在 L0 容量不足**驱逐覆写**时。
- **归 0 不摘位置视图**：未被驱逐覆写则位置视图仍记"X 在该节点 L0"→ 仍可命中、仍可直传（D-direct / D→P 直传的命中来源）。只有驱逐覆写才摘视图。
- **step 期间冻结是引用计数的自然结果**：请求在跑 → ref>0 → 副本不被驱逐。无需额外 fence。

### 第二级：全局引用汇总（控制面，最终一致，低频）

汇总各请求 / 在途引用，供 **tier up/down**（冷热下沉/提升前看是否还有请求在用）与 **GC 真删**（引用归 0 且冷）。**不进 hot step loop**。

### 为什么驱逐不是问题

L0/L1 是**副本**（见 [`storage-layer.md`](storage-layer.md) "层间副本 vs 移动"）：驱逐 L0 只丢自己这份缓存，别的节点读自己的副本不受影响，L2/L3 还在可回填。故不需要"谁还在用"来阻止驱逐——**全局 ref 只用于 tier/GC，不用于阻止 L0 副本驱逐**。这是与"每层强一致计数"的根本差异：副本可随便丢，权威不可丢。§3 的 **writeback ref**（durable-first 下）拦的是 **register→barrier 窗口**内的驱逐，不是"L2 未落稳"（落稳已在 register 前完成）；与"全局 ref 不阻 L0 副本驱逐"不冲突。

> **参考对照**：Mooncake per-object lease（TTL 5s，lease 期内免 Remove/Evict）是其"引用保护"机制，过期即失效（`ObjectMetadata::GrantLease`/`IsLeaseExpired`）。lake 用显式两级 ref 替代 lease：本地 ref 精确到请求级而非定时，无需续约、无过期误删风险；全局 ref 用于 GC 而非阻塞驱逐。

## 4. 持久语义分层与风险窗口

层=介质，持久性分级——不同层抗的故障不同：

| 层 | 介质 | 角色 | 抗的故障 | 丢失条件 |
|----|------|------|----------|----------|
| L0 | HBM | 缓存副本（易失） | 无（丢了回填） | 随节点销毁失效 |
| L1 | DRAM | 缓存副本（易失） | 无（丢了回填） | 断电失效 |
| L2 | NVMe | **F4 恢复点**（持久） | **NPU/进程级故障** | 整机级故障（连本机 NVMe 一起没）且未落 L3 |
| L3 | 对象存储 | **SSOT 永久权威** | **整机级/池级失败** | 几乎不丢（L3 缺失才视为 block 不存在） |

**关键点**：L2 价值 = NVMe 持久（断电不丢）+ NPU 故障不烧 NVMe。NPU/进程级故障只销毁 worker 的 L0/L1（易失副本），不波及 NVMe——无论该 block 被池放在本机还是远端 KV Node 的 NVMe，都存活。F4 续推从 L2 读：池放本机就本地 NVMe 读（零网络），放远端就网络读远端 NVMe。整机级故障（连本机 NVMe 一起没）才退 L3（SSOT）。即 L2 的恢复能力**与位置无关**，只取决于"是否已写回 L2 durable"。

### 风险窗口（分两级）

```
block 在 L0 产出 ── 每 N 步写回 L2(NVMe) ──> 落 L3(冷下沉,非每步)
   NPU 故障丢: 两次写回 L2 之间的增量(尾巴)
   整机故障丢: 自上次冷下沉 L3 之后的增量(大窗口,但罕见)
```

- **NPU/进程级故障（常见）**：worker 崩溃，L0/L1 随销毁失效；NVMe（L2）不波及（block 无论在本机还是远端 NVMe 均存活）→ 从 L2 续推，丢失**最后一次写回 L2 之后的少量 token**（满块路 + 尾块路）。
- **整机级故障（罕见）**：连本机 NVMe 一起没 → 退 L3（SSOT），丢失**自上次冷下沉 L3 之后的增量**。冷下沉 L3 频率由冷热生命周期决定，非每步；热工作集常驻 L1/L2。
- **写回频率 N 的权衡**：N 小（满块即写 L2）→ NPU 故障风险窗口短、丢得少、写放大大；N 大 → 反之。另有前缀生长诉求（多轮 agent 要快）反向压 N 小。N 留 P7 校准。
- **风险窗口的本质**：F4 续推能恢复到"最后一次写回 L2 的点"，之后到崩溃的增量 token 丢失。这是用"NVMe 作恢复点"换弹性的固有代价，非 bug。整机级故障退 SSOT 是兜底，代价是更大的增量窗口、但概率远低于 NPU 级故障。

> **参考对照**：SGLang `--hicache-write-policy` `write_back`/`write_through`（`storage-backends.md`）正是同一权衡——write-back 省带宽但有丢窗口，write-through 无窗口但写放大。lake 选 write-back（L2 NVMe 恢复点）+ 尾块路请求结束兜底，NPU 故障风险窗口收窄到"两次写回之间的增量"。差异：SGLang L1/L2 实例私有、崩溃即丢；我们 L2 是池化的 NVMe 副本，NPU 崩溃只丢该 worker 的 L0/L1，NVMe 副本（无论本机远端）存活。

## 5. 故障恢复（F4）

执行失败（节点故障 / 超时）→ 触发 F4 → Router 依最新集群状态**重跑模式选择**（纯函数，不设降级阶梯）→ 池把该 sequence 已有 KV 放置到新节点 HBM → 续推。

### worker 崩溃续推

1. 存储池检测节点失败（心跳超时）。
2. 把该 sequence 路由到新节点（Router 重跑选路）。
3. 池把已有 KV（**L2 F4 恢复点**，从 L2 读——本机 NVMe 或远端 KV Node NVMe，按池放置位置）放置到新节点 HBM。
4. **ref 从原请求转移到新请求**（避免被冷热淘汰，见 §3）。
5. 原节点 HBM/DRAM 副本随销毁失效（本就是易失副本，非私有状态）；原节点 NVMe 若未被整机级故障波及则仍存活、可作回填源。
6. 续推从"最后一次写回 L2 的点"开始，丢失之后的少量 token（§4 NPU 级风险窗口）。

### 断点 KV 也丢失

若该 sequence 的断点 KV 也未写回 L2（请求刚起、尚未触发满块写回 / 尾块写回）→ 退化为**从 prompt 重算**（[`features/features.md`](../features/features.md) F4 失败语义）。仅损失"最后增量窗口"的 token，不丢请求。

### Drain / 主动下线（非故障）

节点进入 Drain 时，agent 先把"还被远端引用的 block"（在途传输 ref>0 或被其他节点 read set 引用）落一份到 L2（NVMe，F4 恢复点，抗 worker 销毁即可），再下线——避免销毁后远端拉取落空。这是"默认直传 + Drain 推 L2"在弹性侧的落点（[`kv-cache-pool.md`](kv-cache-pool.md) "故障恢复"）。

### 池级失败 / SSOT 恢复

- **L3 = SSOT**：池重启 / 控制面 etcd 重建后，从对象存储恢复持久副本，不丢数据（[`features/features.md`](../features/features.md) F11）。
- **元数据重建**：位置视图权威在存储控制面进程内存,池重启从 etcd 降频 checkpoint 重建位置视图 + 从 L3 回填字节(etcd 只存 checkpoint,非强一致位置表,见 §1)。
- **L3 缺失才视为 block 不存在**：逐级下查到 L3 仍无 → 该 block 不存在（功能退化，非数据损坏）。

## 6. GC 与孤儿块 reconcile

回收无效 / 不可达 block（[`kv-cache-pool.md`](kv-cache-pool.md) "GC"）：

- **冷块回收**：引用 0 + 冷（LRU 末尾）→ 淘汰。
- **孤儿块**：Prefill 崩溃残留的部分写入 block → 写入屏障标记未完成，TTL 后回收。对齐 Mooncake zombie 清理（`put_start_discard_timeout` 30s 无 `PutEnd` → 抢占释放）。
- **元数据一致性**：以控制面元数据为权威，**block 字节删除前确认元数据已无引用**；崩溃恢复扫描 reconcile 孤儿块。
- **节流**：后台运行，受带宽 / IO 预算限制（<10%），不阻塞数据面。

> **参考对照**：Mooncake `ClearInvalidHandles`（`client_live_ttl_sec` 10s 过期移除死 client 副本）+ zombie 清理是同类机制。lake 加一层"元数据权威先于字节删除"的顺序约束——先摘位置视图、确认全局 ref=0、再删字节，避免"字节先删、引用还在"的悬空访问。

## 7. 一致性速查

| 关注点 | 一致性 | 权威 | 见 |
|--------|--------|------|-----|
| block 位置 / radix / 引用汇总 | 强一致 | 控制面进程内存（权威）+ etcd（降频 checkpoint） | §1、§8 |
| 本地命中视图镜像 / agent 视图 | 最终一致 | 控制面推送（gRPC stream / 同机共享内存） | §1、[`scheduling.md`](scheduling.md) §1 |
| KV 字节（单 token range） | 写一次读多次（immutable） | 产出即定 | §2 |
| ref（本地） | 强一致（agent 内） | 池 agent | §3 |
| ref（全局） | 最终一致 | etcd 汇总 | §3 |
| L0/L1 副本 | 易失，可丢 | L2/L3 回填 | §3、§4 |
| L2 恢复点 | 抗 NPU/进程级故障 | NVMe（持久） | §4、§5 |
| L3 SSOT | 永久权威 | 对象存储 | §4、§5 |
| 字节删除 | 元数据先于字节 | 控制面 | §6 |

## 8. 位置一致性的理论定位

前面各节给出了 lake 一致性的**结论**（控制面强一致 / 数据面最终一致、写一次读多次、两级 ref、持久语义分层）。本节回答"这些结论的强度究竟是什么、为什么这么分档合理"——用多核 / DSM / disaggregated memory 的成熟理论把 lake 的位置一致性定位清楚。结论是一句话：**lake 的位置一致性不是单一强一致，是三个经典范式的组合**。

```
lake 位置一致性 = 目录一致性 (directory coherence)   ← MESI 的集群 scale 版
               + 释放一致   (release consistency)     ← DSM 经典（TreadMarks/Munin）
               + flat disaggregated memory           ← 当代内存池化
```

### 8.1 一致性谱系：lake 各路径要哪档

| 强度 | 术语 | 含义 | lake 对应 |
|---|---|---|---|
| 最强 | 线性一致（linearizability） | 写后立即可见，全局单序 | 搬 KV 那一刻同步查权威 |
| 释放一致（release） | 同步点刷一致，普通操作松 | **请求结束 = 写回屏障**（§3 writeback ref） |
| 最终一致（eventual） | 不再写后收敛 | Router 选路镜像 / GC 视图 |

关键：**全局 KV 管理要的是"不丢 + 最终收敛"，不是线性一致；自由流动要的是"搬时查准"（同步查询），不是全局实时推送。** 线性一致只在"搬 KV 时查权威"那一刻需要，而那是查询不是高频写——没有写瓶颈。

### 8.2 范式 1：目录一致性（位置管理的骨架）

MESI 解决多核 cache 一致性：多 cache 副本 + 主存，写一个要让其他副本失效。但 MESI 靠 **bus snooping（总线广播）**，scale 到几十核就吃力；集群级无总线，改用 **directory（目录）**——目录记录每个 block 在哪些 cache 有副本，写时查目录**定向失效**，不广播。

```
多核 MESI (snoop, 不 scale):           lake (directory, scale):

  Core0 cache ─┐                         Router 镜像 ─┐
  Core1 cache ─┼─ bus 广播失效            agent 镜像 ──┼─→ 控制面（目录）
  Core2 cache ─┘ (谁改了谁喊)            Router 镜像 ─┘   查目录→定向失效
                                                        (不广播)
```

**lake 的目录 = 存储控制面**（持"哪个 block 在哪些节点的 L0/L1/L2"）。block 被驱逐/迁移时，控制面**查目录知道谁持镜像 → 定向发失效**（摘视图），不是全集群广播。这正是 §3"归 0 不摘位置视图，只有驱逐覆写才摘视图并推送"的语义——**lake 已在用 write-invalidate**。

MESI 状态机可借分类（不照搬协议）映射到 lake block 位置状态：

| MESI | 含义 | lake block 状态 |
|---|---|---|
| M (Modified) | 独占且已改 | 独占放某节点 L0，刚写未写回 L2 |
| E (Exclusive) | 独占未改 | 独占放某节点 L0（单副本） |
| S (Shared) | 多副本共享 | 多节点 L0/L1 有缓存副本 |
| I (Invalid) | 无效 | 已驱逐覆写，镜像摘除 |

不照搬 MESI 的理由：MESI 是硬件级纳秒强一致 + snoop 总线；lake 是软件级、集群规模、只要 release 一致——借**状态分类 + write-invalidate 语义**，不借 snoop 总线与硬件强一致。

### 8.3 范式 2：释放一致（请求结束 = release 屏障）

**Release consistency（DSM 经典，TreadMarks/Munin）**：普通读写可松，**只在同步点（release/acquire）保证一致**——进入/离开临界区才 flush。

lake 直接对应：§3 的 **writeback ref + 请求结束屏障**就是 release 一致。满块 L2 durable 后注册 radix（请求执行中）可松——晚几毫秒被 Router 看见无所谓；**请求结束那刻 = release**：flush 该请求全部已注册 block 的 L2 写回 + ack + writeback ref 归零（解冻晚于 barrier），保证此后该请求的 KV 对全局可见、可被引用、GC 可管。

```
R 执行中（prefill 产出 block B1..Bn）:
  t1  B1 满块 → 注册 → 控制面内存（可松：Router 此刻没看到 B1，无所谓）
  t2  B2 满块 → 注册 → 控制面内存（同上）
  —— 一致性"松"：B1..Bn 在控制面权威，Router 镜像可能还没更新 ——

t_end  R 请求结束 → RELEASE 屏障（§3）:
       flush(B1..Bn 全部 durable 写回 L2) + ack
       —— 一致性"刷"：此后 B1..Bn 全局可见、可被引用、GC 可管 ——

t_end+  别的请求 R' 复用 B1..Bn 前缀:
        Router 镜像已收敛（或 miss 回填）→ 命中 → D-direct
```

为什么这合理：满块注册高频，但**不必即时全局可见**——同一请求的 block 在请求结束前不会被别人引用（因果上，复用发生在 R 结束后）。release 屏障卡在"可能被引用"的边界，既不阻塞高频注册，又保证正确性。这正是 §1"控制面权威在内存、etcd 只存降频 checkpoint"的理论依据：满块注册写内存（release 一致，可松），请求结束才 flush 到 durable，etcd 的 checkpoint 自然落在低频的 release 点而非每个满块。

### 8.4 范式 3：flat disaggregated memory（L0↔池 的分层）

当代内存池化两种路线：**coherent**（硬件一致，如 CXL.cache）vs **flat**（软件显式管，local cache + far authority，miss 显式 pull）。lake 选 flat——不依赖硬件 coherence，软件管 L0↔L1↔L2↔L3。

```
flat disaggregated memory:           lake 分层:

  local cache ──miss──→ far memory     L0（本机 HBM）──miss──→ L1/L2/L3（池）
     ↑ 拉远端，显式 pull                    ↑ 控制面查位置 → RDMA pull
     ↑ 软件管放置/驱逐                       ↑ 池管放置/驱逐
```

搬 KV = flat memory 的显式 pull：agent 要远端 block → 同步查控制面（权威，准）→ 拿到位置 → RDMA 拉。这一查是**线性一致查询**（那一刻要准），但**是查询不是高频写**——无写瓶颈，只有 ms 级查询延迟（搬 KV 本就 ms 级，可接受）。

```
D→P 回传 KV 例子（P 要复用 D 产出的前缀 block B，已在池）:
  1. P 的 agent 同步查控制面:"B 在哪?"        ← 线性一致查询（强）
  2. 控制面查目录:"B 在 D 节点 L0"            ← 权威，准
  3. P 的 agent → RDMA pull B 从 D            ← flat memory 显式 pull
  4. B 入 P 的 L0，控制面更新目录（S 副本）     ← 定向失效给持镜像者
```

第 1 步强一致（查权威），但不写、不广播、不进 etcd 高频路径——这是"自由流动不需要全局强一致推送"的落点：流动时查准即可，平时不必全推。

### 8.5 三档一致性汇总

| 用途 | 一致性档 | 机制 | 瓶颈 |
|---|---|---|---|
| 搬 KV（查 block 在哪） | **线性一致查询** | 同步查控制面目录（非推送） | 无写瓶颈，ms 查询延迟 |
| 全局 GC/配额/迁移 | **不丢 + 最终收敛** | agent 上报带 ack+序号，可攒批 | 可控（攒批降频） |
| Router 选路镜像 | **最终一致** | gRPC stream 推送，增量+gap replay | 可松（错了 miss 回填） |
| 满块注册写权威 | **release 一致** | 写控制面内存，请求结束 flush（§3） | 不进 etcd，无 Raft 放大 |

由此澄清两个常见混淆：**强一致 ≠ 必须进 etcd**（权威可放控制面进程内存，单写者线性一致）；**不丢 ≠ 强一致**（全局管理靠 ack + 序号 + 攒批做到"不丢 + 最终收敛"，不是线性一致）。

### 8.6 dynamo 在此框架里的位置

dynamo 没有上述三档的完整组合，因为它**不需要全局管理**（KV 归 engine，无全局 GC/配额/迁移权威）：

| | dynamo | lake |
|---|---|---|
| 目录一致性 | 无全局目录（router radix 副本 + 每实例本地） | **有**（控制面 = 集群目录） |
| 释放一致 | 无（事件流 best-effort，无 release 屏障） | **有**（请求结束 = 写回屏障，§3） |
| flat memory | 部分（`find_matches` 搬时查本地权威） | **完整**（L0↔L1↔L2↔L3） |
| 一致性强度 | 最终一致（best-effort，可丢） | release 一致（不丢）+ 线性查询（搬时） |

dynamo 最接近的是 `find_matches`（`3rdparty/dynamo/lib/kvbm-engine/src/leader/mod.rs:31`）——搬 KV 时查本地强一致视图，即 flat memory 的 pull。但缺目录（无全局副本失效）和 release 屏障（无写回屏障），因为它的"全局管理"由 engine 各自负责、不做集群级权威。lake 因 HBM 归池，必须自补这两层。

> **reference 说明**：理论范式（directory coherence / release consistency / flat disaggregated memory）属经典 DSM / 多核理论，**本项无直接 3rdparty 参考实现**；dynamo `find_matches` 是 flat memory pull 的最近参照。反证依据：dynamo `distributed.rs` 把 KV 事件踢出 etcd 走 NATS/ZMQ（"approximate mode" 可丢），印证"高频位置写不该进 etcd"。

## 9. 开放问题

- 写回频率 N（风险窗口 vs 写放大 vs 前缀生长时效）待 P7。
- 满块写回频率（满一个写 vs 攒几个）待 P7。
- 反向回传 radix 生长时效上限（满块注册进 radix 的滞后，影响 D→P 子情况 A 成立，见 [`data-flow.md`](data-flow.md) §3.4）。
- 推送刷新的延迟与带宽（etcd watch vs gRPC stream 取舍）待 P7。
- 池级失败下 L3 回填的冷启动时延（见 [`../features/slo.md`](../features/slo.md) 冷启动）。
