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
| vLLM kv event（2026-10-13 代码核实） | 上游 `3rdparty/vllm` `vllm/distributed/kv_events.py:56` `BlockStored`（带 `medium` 字段）；vllm-ascend 0.26 `kv_transfer.py:1162` / `pool_worker.py:3111`（put 成功发 `medium="cpu"`）、`ascend_store_connector.py:213` `take_events` | 引擎 kv event **能**携带池写入信息——引擎事件驱动索引生态（llm-d / AIBrix / Dynamo `KvIndexer`）靠它建视图 | 两个结构性盲区：① 事件只在连接器 worker 代码路径生成，`create_copy_task` 绕引擎 → **预复制副本无事件**；② vllm-ascend 0.26 全仓零 `BlockRemoved` → **副本消失也无事件**。不能当视图来源 |

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

**为什么不能靠引擎 kv event**（2026-10-13 代码核实，锚点见「参考实现」表）：

- kv event **能**携带池信息：事件带 `medium` 字段；AscendStoreConnector 在引擎自己 put 成功后发 `BlockStored(medium="cpu")`，跨 rank 聚合后由 `take_events()` 发出。
- 但对预复制有两个结构性盲区：
  1. **绕引擎 = 无事件**：事件只在连接器 worker 代码路径里生成；`create_copy_task` 不经任何引擎代码，结构上不可能有引擎为该副本发事件。
  2. **没有 Removed**：vllm-ascend 0.26 只发 `BlockStored`、零 `BlockRemoved`——副本消失（驱逐 / TTL / 属主连坐，README 开放问题 #4）对事件消费者同样不可见。
- 结论：靠引擎 kv event 建索引的 router（Dynamo `KvIndexer` / llm-d EPP / AIBrix 路线）对预复制副本**必然盲、且必然陈旧**。直读 master 不是优选，是**必然**。
- 集成约束（归 D4）：若最终部署的 router 框架消费引擎 kv event（Dynamo 路线即如此），预复制视图必须带外注入该框架（自定义 producer/scorer），不能指望事件流。

陈旧两个方向都只损性能：

- **正向**（副本已就绪、镜像未刷新）→ 错过本地命中，退为普通池命中。
- **反向**（镜像有、实际已被驱逐/连坐移除）→ 请求打到实例后 get miss，引擎自动远端读/重算，不损正确性（同 `prefixHint` 反向陈旧不查的口径）。预复制 READY 核验（`batch_get_replica_desc`）保证写入镜像的条目在写入时刻为真。

## 3. 事件身份：三种「KV 在哪」的说法不能混

- **master 视图**（权威）：key → 副本 → seg。预复制副本唯一完备的信息源。
- **引擎 kv event**（引擎动作的自白）：只覆盖引擎自己的 put；无 Removed（§2）；引擎 APC（HBM）命中是引擎私有 LRU 状态，不进任何事件、不作选路输入。
- **控制面事件**（预复制动作的自白，路径 a）：控制面知道每次 copy 的结果，可主动发「就绪/失效」——身份与引擎事件不同，**不能混进引擎事件流**；只作镜像刷新触发，不作权威。

Router 可见的「本地命中」只按 master 视图定义：**目标实例本机 seg 有 DRAM 副本**。

- 打分上本地 DRAM 副本 ≈ Dynamo 分层命中的 host 层（device 1.0 / host 0.75 / disk 0.25）：省跨机 RDMA、不省 D2H 加载。
- READY 语义：READY = 本机 DRAM 副本核验通过，不含 HBM；HBM 由真实请求的 get session 读后驻留 APC，属引擎自治。

## 4. 时序：预测式为主，反应式兜底

最终形态是 Router 把请求发给实例——因此 intent 的**主来源就是路由决策本身**：「Router 决定这个请求（会话）去哪个实例，precopy 就预热哪个实例」。两个回路并存：

- **会话级回路（预测式，主）**：会话首次路由按负载/亲和选实例 → 决策上行 → 控制面预热该会话前缀到选定实例 → **后续轮次**本地命中。agent 多轮场景的主收益在这里；首轮赶不上预热是预期行为（走兜底）。
- **请求级回路（回报路径）**：镜像里已有本地副本 → Router 优先选该实例——预热的收益经视图回流兑现。

主路径（预测式）：

1. 预热 intent 到达（路由决策上行，或热度观测辅助，见 §6）
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
- Router 只出两类 hint，控制面自主执行：
  - **预热 intent**：路由决策（选定实例 → 预热到该实例，§4 会话级回路）+ 热度观测（辅助）→ 控制面决定复制到哪、何时复制
  - **释放 hint**：会话生命周期（结束 / 迁移 / 超时）→ 控制面评估后按 D2 操作集释放（`Remove`/`BatchRemove` 现成）——Router 知道「谁还会用」，池知道「谁还在」，释放决策需要两者，故 hint 上行、执行在控制面
- 结果经 master 视图回流 Router 镜像——这是 lake「池放置·调度读视图」单向耦合在本系统的形态；差异在放置智能的位置（池内 vs 外置控制面），边界方向不变。
- 已知缺口（登记，不归 D1）：master 无显式 pin API（lease 机制 → D2）；属主下线副本连坐移除（README 开放问题 #4 → D3/D5）。

## 决策小结

| 问题 | 选择 | 依据 |
|------|------|------|
| 选路输入 | 前缀→keys（keys.py 口径）× key→replicas（master）× seg→实例（编排注册） | §1 |
| 可见性 | 直读 master 权威 + Router 只读镜像——引擎 kv event 有结构性盲区（绕引擎无事件、无 Removed），直读是必然不是优选 | §2，llm-d / Dynamo / vLLM 核实 |
| 事件身份 | 三种身份不混：master 视图（权威）/ 引擎 kv event（引擎动作）/ 控制面事件（预复制动作）；本地命中只按 master 视图定义 | §3 |
| 时序 | 路由决策驱动预热（会话级回路为主）+ 请求级回报回路；反应式读路径兜底；不设等待式选路 | §4，D003 时序节 |
| 目标函数 | 本地副本 > 池远端 > 重算，叠加负载与传输成本 | §5，Dynamo 成本模型 |
| release | 归池策略；Router 给预热/释放两类 hint，控制面执行；单向边界 | §6，lake 池放置·调度读视图 |

## 留给后续

- **D2**：KV 调度操作集（pin/demote/prefetch/retain/release；master 无显式 pin → `SubmitReplicaActionProposal` lease 机制；释放有 `Remove/BatchRemove` 现成）
- **D3**：副本生命周期与 READY 语义（连坐移除、TTL、属主下线后的副本归属）
- **D4**：编排进程形态（预复制控制面独立进程或并入 Router；READY 栅栏接 gateway；seg→实例注册通道；**router 框架若消费引擎 kv event，预复制视图带外注入**，§2 集成约束）
- **D5**：故障衔接（源属主掉线 copy 失败、与重路由的先后）
- **D6**：layerwise 选型（[../docs/research/vllm-ascend/layerwise-taxonomy.md](../docs/research/vllm-ascend/layerwise-taxonomy.md)）
- **D7**：方案 A（壳请求进 HBM）去留与叠加顺序
