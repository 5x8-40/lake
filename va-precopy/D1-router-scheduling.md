# D1: Router 集成与请求调度（预复制的选路侧）

- 日期: 2026-10-13
- 状态: 提议（设计定型，待 D2/D4 配套落地）
- 上游问题: [README.md](README.md)「开放问题 #2 上层编排」的选路半——Router 如何感知预复制副本并据此选实例；「谁预取、何时放行」的进程形态归 D4。

## 参考实现

| 参考 | 锚点 | 借什么 | 关键差异 |
|------|------|--------|----------|
| lake 选路面 | `../go/router/modeselect.go::SelectExecMode` + `prefixHint`；[`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「权威、镜像与本地 overlay」 | Router 持位置视图**只读镜像**、选路零 RPC；miss 回退权威查询；反向陈旧不查（只损性能） | lake 镜像由存储控制面 gRPC stream 推送；本设计权威是 Mooncake master，镜像靠自己 reconcile |
| lake 命中定义 | [`../docs/features/features.md`](../docs/features/features.md)「Pool 命中 vs 本地命中」 | 术语：Pool 命中（省重算需传输）≠ 本地命中（省传输） | lake 本地命中指 L0/HBM 池放置；本设计 Router 可观测的是本机 DRAM 副本（vllm-ascend 的 HBM 侧 APC 是引擎私有，不进视图） |
| lake 单向边界 | [`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「池放置·调度读视图」 | 调度读视图、不反向指挥放置；hint 上行、视图下行 | lake 的池有自主放置能力；Mooncake 池没有——放置智能在外置预复制控制面，但单向边界不变 |
| llm-d Router | [`../docs/research/llm-d/overview.md`](../docs/research/llm-d/overview.md)「核心结论」；[`../docs/research/llm-d/architecture.md`](../docs/research/llm-d/architecture.md) §4 | 推测索引（TTL 2s）是**派生索引**系统补决策-确认窗口的手法；权威视图系统没有该窗口——「存储池就是真相，Router 读的视图不需要逼近谁」 | llm-d 索引是事件流派生的副本视图（引擎才是真相）；本设计 master 即真相 |
| Dynamo router | [`../docs/research/dynamo/overview.md`](../docs/research/dynamo/overview.md)「Router(KV-aware)」 | 成本模型（`adjusted_prefill` = 预填块 − 各层命中×权重；cost 叠加 decode 占用 + 在途请求）；分层命中计价 device 1.0 / host 0.75 / disk 0.25；在途负载无权威、router 本地记账；事件 gap→快照重放 | Dynamo 缓存状态权威在 worker 本地索引，router 靠事件逼近，故有丢事件/gap 问题；本设计直读 master，无此失效模式。外部重算 block hash 链 Dynamo 已为先例（keys.py 同款） |
| Mooncake 界限 | [`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「为何池必须自长 radix」 | Mooncake store 是 dumb blob 池（无内容寻址、无前缀匹配）——预复制控制面就是外置的「懂前缀 + 懂位置」层 | 不照搬 lake 的池内 radix：keys.py 按 key 逐把算，不建树；前缀匹配退化为「连续满块命中长度」 |

## D1 要回答的问题

1. 选路时 Router 手上有什么输入（选路输入）
2. 副本可见性怎么来（三路径选型 + 盲区）
3. 「本地命中」指什么（事件身份语义）
4. 预复制与请求的先后（时序）
5. 多个候选实例怎么打分（目标函数）
6. 副本释放归谁（release 边界）

## 1. 选路输入：三张表

| 输入 | 内容 | 来源 | 现状 |
|------|------|------|------|
| 前缀 → keys | block hash 链 × PoolKey 全 rank 展开 | `precopy/keys.py` 口径（import 上游权威 hash 链；`PYTHONHASHSEED` 对齐） | 已有 |
| key → 副本位置 | 每把 key 的副本落在哪些 seg | master（放置权威） | `batch_get_replica_desc` / admin API 现查 |
| seg → 实例 | seg 名（`IP:rpc_port`）↔ 实例 ↔ rank | 生产由编排层注册上报；测试床的 `resolve.py` 探测链是取证手段，不是生产路径 | 生产侧归 D4 |

要点：

- **本地命中判定**：前缀的**连续满块**集合中，目标实例**每个 rank 的 key 都在本机 seg 上有内存副本**。全 rank 齐备 = 零跨机；部分 rank 本地 = 按本地块比例计入打分（get session 每 rank 独立选副本，部分本地仍省对应 rank 的传输）。
- 连续口径与 lake 一致（`modeselect.go::contiguousLocalHitBlocks`——中段有空洞不构成零传输前缀）。
- 命中 token 数 = 连续满块数 × block_size，尾巴重算（801 token 前缀命中 768 的口径，见 [README.md](README.md)「三个对象」）。
- MLA（`put_step>1`）key 折叠到 rank 0 → 本地判定退化为单 rank；该映射未实测（README「边界与已知限制」）。

## 2. 可见性：直读权威为主，事件只作优化

三条路径：

- **a. 自产事件**：预复制控制面复制完成/失效时向 Router 发事件
- **b. 池侧 publisher**：master 推送副本变更事件流（Mooncake master 无事件流，需自研）
- **c. 直读权威**：Router 持 key→replicas 只读镜像，周期 reconcile master + 预复制动作完成后即时刷新对应条目

**选 c 为主路径**；a/b 只在查询规模成为瓶颈时作增量优化层，不作权威。

理由：

- master 已是放置权威（README 时序节：「哪个 key 在哪个 seg 无需通知任何人——master 是放置权威，get 时 `BatchQuery` 现查」）。Router 视图 = 权威视图的镜像，不需要「逼近」谁——推测索引是派生索引系统的补窗手法，权威视图系统没有决策-确认窗口（llm-d architecture.md §4 小结、overview.md「核心结论」）。
- 事件流自带丢失/乱序/gap 失效模式，Dynamo 为此配了序号 + 快照重放整套兜底（dynamo overview.md「可靠性」节）；直读没有该失效模式。
- b 要改 master（自研事件流），与「不改池」边界冲突。

陈旧两个方向都只损性能：

- **正向**（副本已就绪、镜像未刷新）→ 错过本地命中，退为普通池命中。
- **反向**（镜像有、实际已被驱逐/连坐移除）→ 请求打到实例后 get miss，引擎自动远端读/重算，不损正确性（同 `prefixHint` 反向陈旧不查的口径）。预复制 READY 核验（`batch_get_replica_desc`）保证写入镜像的条目在写入时刻为真。

## 3. 事件身份：池副本 ≠ 引擎前缀缓存

- Router 可见的「本地命中」 = **池元数据里目标实例本机 seg 有 DRAM 副本**。
- 引擎 APC（HBM）命中是引擎私有 LRU 状态，不进 Router 视图、不作选路输入。
- 打分上本地 DRAM 副本 ≈ Dynamo 分层命中的 host 层（device 1.0 / host 0.75 / disk 0.25）：省跨机 RDMA、不省 D2H 加载。
- READY 语义：READY = 本机 DRAM 副本核验通过，不含 HBM；HBM 由真实请求的 get session 读后驻留 APC，属引擎自治。

## 4. 时序：预测式为主，反应式兜底

主路径（预测式）：

1. 预热 intent 到达（来源见 §6）
2. 控制面算 key → `create_copy_task` → `query_task` → `batch_get_replica_desc` 核验 → READY
3. Router 镜像刷新——该前缀对目标实例本地可见
4. 后续同前缀请求：镜像查中 → 直接选该实例

兜底（反应式）：选了实例但副本未就绪（未预取 / 在途 / 镜像正向陈旧）：

- 请求照常下发，get session 建立时 local-first 自动选现存副本（远端则远端读）——读路径既有行为，**不阻塞 TTFT**
- 控制面观测后异步补复制，后续请求受益
- 依据：session 锁副本是 per-request 的，错过只影响当前请求（[D003-kv-prefetch.md](D003-kv-prefetch.md)「错过时序的缓解」）

**不设等待式选路**（hold 请求等副本就绪）——READY 栅栏归编排放行（D003「与请求调度联动」），不塞进请求热路径；时序错过由读路径兜底，不由阻塞兜底。

## 5. 目标函数

候选实例分级：

1. 全 rank 连续本地副本（零跨机 RDMA）→ 选倾最强
2. 池远端副本（省重算，需跨机读；layerwise 下逐层远端更贵）
3. 无命中（全量重算）

打分 = 命中收益 − 传输/加载成本 + 负载惩罚，参照 Dynamo 成本模型（dynamo overview.md「Router(KV-aware)」）：

- 命中项：连续本地满块数 × host 层权重（初值借 0.75 量级，真机校准）；池远端命中按池命中权重另计
- 负载项：实例队列长 / in-flight 由 Router 本地记账（在途负载无权威，router 自己加减，Dynamo slot manager 同款口径）
- 传输成本：跨机读 ≈ 前缀字节 / 有效带宽；与等效重算成本比较，不划算则换实例重算（lake 带宽闸同构，`SelectExecMode` 的 `bwGBps` 阈值）

与 lake 差异：不选执行模式（vllm-ascend 没有 D-direct），输出 = **选实例** + （可选）**是否先触发预复制**。

## 6. release 与放置的单向边界

- Router 不指挥放置与释放：副本生命周期归池策略（TTL / 驱逐；副本无 pin，README「复制与副本」）。
- Router 只出 hint：路由决策与命中观测产生「前缀热度 / 将要用」信号 → 控制面自主决定复制到哪、何时复制；结果经 master 视图回流 Router 镜像。
- 这是 lake「池放置·调度读视图」单向耦合在本系统的形态；差异在放置智能的位置（池内 vs 外置控制面），边界方向不变。
- 已知缺口（登记，不归 D1）：master 无显式 pin API（lease 机制 → D2）；属主下线副本连坐移除（README 开放问题 #4 → D3/D5）。

## 决策小结

| 问题 | 选择 | 依据 |
|------|------|------|
| 选路输入 | 前缀→keys（keys.py 口径）× key→replicas（master）× seg→实例（编排注册） | §1 |
| 可见性 | 直读 master 权威 + Router 只读镜像；事件仅规模化优化 | §2，llm-d / Dynamo 对照 |
| 事件身份 | 池 DRAM 副本 = Router 可见的本地命中；APC 不可见不输入 | §3 |
| 时序 | 预测式预热为主；反应式读路径兜底；不设等待式选路 | §4，D003 时序节 |
| 目标函数 | 本地副本 > 池远端 > 重算，叠加负载与传输成本 | §5，Dynamo 成本模型 |
| release | 归池策略；Router 只给 hint；单向边界 | §6，lake 池放置·调度读视图 |

## 留给后续

- **D2**：KV 调度操作集（pin/demote/prefetch/retain/release；master 无显式 pin → `SubmitReplicaActionProposal` lease 机制；释放有 `Remove/BatchRemove` 现成）
- **D3**：副本生命周期与 READY 语义（连坐移除、TTL、属主下线后的副本归属）
- **D4**：编排进程形态（预复制控制面独立进程或并入 Router；READY 栅栏接 gateway；seg→实例注册通道）
- **D5**：故障衔接（源属主掉线 copy 失败、与重路由的先后）
- **D6**：layerwise 选型（[layerwise-taxonomy.md](layerwise-taxonomy.md)）
- **D7**：方案 A（壳请求进 HBM）去留与叠加顺序
