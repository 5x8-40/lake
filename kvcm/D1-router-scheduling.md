# D1: Router 集成与请求调度（预复制的选路侧）

- 日期: 2026-10-13
- 状态: 提议（设计定型，待 D2/D4 配套落地）
- 上游问题: [README.md](README.md)「开放问题 #2 上层编排」的选路半——Router 如何感知预复制副本并据此选实例；「谁预取、何时放行」的进程形态归 D4。

## 参考实现

| 参考 | 锚点 | 借什么 | 关键差异 |
|------|------|--------|----------|
| lake 选路面 | `../go/router/modeselect.go::SelectExecMode` + `prefixHint`；[`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「权威、镜像与本地 overlay」 | Router 持位置视图**只读镜像**、选路零 RPC；miss 回退权威；反向陈旧不查（只损性能） | lake 镜像由存储控制面 gRPC stream 推送；本设计权威是 Mooncake master，镜像靠自己 reconcile |
| lake 命中定义 | [`../docs/features/features.md`](../docs/features/features.md)「Pool 命中 vs 本地命中」 | 术语：Pool 命中（省重算需传输）≠ 本地命中（省传输） | lake 本地命中指 L0/HBM 池放置；本设计 Router 可观测的是本机 DRAM 副本（F6） |
| lake 单向边界 | [`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「池放置·调度读视图」 | 调度读视图、不反向指挥放置；hint 上行、视图下行 | lake 的池有自主放置能力；Mooncake 池没有——放置智能在外置控制面，边界方向不变 |
| llm-d Router | [`../docs/research/llm-d/overview.md`](../docs/research/llm-d/overview.md)「核心结论」；[`../docs/research/llm-d/architecture.md`](../docs/research/llm-d/architecture.md) §4 | 推测索引（TTL 2s）是**派生索引**系统补决策-确认窗口的手法；权威视图系统没有该窗口 | llm-d 索引是事件流派生的副本视图（引擎才是真相）；本设计 master 即真相 |
| Dynamo router | [`../docs/research/dynamo/overview.md`](../docs/research/dynamo/overview.md)「Router(KV-aware)」 | 成本模型（分层命中×权重 + decode 占用 + 在途请求）；分层计价 device 1.0 / host 0.75 / disk 0.25；在途负载无权威、router 本地记账 | Dynamo 缓存状态权威在 worker 本地索引，router 靠事件逼近（丢事件需 gap→快照重放）；本设计直读 master，无此失效模式。外部重算 block hash 链 Dynamo 已为先例（keys.py 同款） |
| Mooncake 界限 | [`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「为何池必须自长 radix」 | Mooncake store 是 dumb blob 池（无内容寻址、无前缀匹配）——预复制控制面就是外置的「懂前缀 + 懂位置」层 | 不照搬 lake 的池内 radix：keys.py 按 key 逐把算，不建树；前缀匹配退化为「连续满块命中长度」 |

## 事实基线（2026-10-13 代码核实）

设计建立在以下已核实事实上。Mooncake 锚点 @ `1fc27b62`（submodule）；vllm-ascend 锚点 @ 0.26（rc1）。

**池与副本**

- **F1 master 是放置权威**：key → 副本 → seg 由 master 记录；读路径 get 时 `BatchQuery` 现查，没有其他位置通知通道（[README.md](README.md) 时序节）。
- **F2 key = 满块 × rank 切片**：每个满块 × 每个 rank 的切片 = 一把 key，尾带 `head_or_tp_rank`；801 token 前缀 × TP=2 → 12 把 key（README「三个对象」）。
- **F3 副本选择 local-first**：非 layerwise 读时 `SelectBestReplica` 本机内存副本优先（`replica_selection.h:122`）；layerwise get session 建立时 `SelectCompleteMemoryReplica` 选一次副本（`real_client.cpp:471`），且 session 要求恰好一个副本（`:6470` 附近，`replicas.size() != 1 → INVALID_REPLICA`）→ 复制必须先于 session 完成，错过只影响当前请求（[kv-prefetch.md](kv-prefetch.md) 时序节）。
- **F4 复制 API**：`create_copy_task(key, [seg])`（Python 绑定 `mooncake-integration/store/store_py.cpp:3362`）；master 只校验 key 存在 + 目标 seg 可分配（`master_service.cpp:13677 CreateCopyTask`）；整对象复制；源属主客户端必须在线；副本落位用 `batch_get_replica_desc` 核验（`store_py.cpp:3340`）。
- **F5 副本无 pin**：READY 后受池驱逐策略管理；属主实例下线，key 连同远端副本被 master 移除（README 开放问题 #4，2026-10-12 观察，待确认）。
- **F6 引擎 APC 私有**：HBM 侧前缀缓存是引擎私有 LRU——无 pin、无池元数据、外部不可见（kv-prefetch.md 方案 A 注意节）。

**引擎 kv event**

- **F7 事件 schema 带介质字段**：`MEDIUM_GPU` / `MEDIUM_CPU`（上游 `vllm/distributed/kv_events.py:51-52`）；`BlockStored{block_hashes, parent_block_hash, token_ids, block_size, medium}`（`:56-68`）。
- **F8 引擎 put 成功才发事件**：AscendStoreConnector 的 worker 侧在引擎自己 put 成功后生成 `BlockStored(medium="cpu")`（`kv_transfer.py:1162`、`pool_worker.py:3111`），跨 rank `KVEventAggregator` 聚合后经 `take_events()` 发出（`ascend_store_connector.py:213`）；门控 `enable_kv_cache_events`（`pool_scheduler.py:108`）。
- **F9 盲区一：预复制无事件**：事件只在连接器 worker 代码路径生成（F8）；`create_copy_task` 不经任何引擎代码 → 预复制副本**结构上无事件**，任何引擎事件消费者都看不到。
- **F10 盲区二：无 Removed**：vllm-ascend 0.26 全仓零 `BlockRemoved` → 副本消失（驱逐 / TTL / F5 连坐）对事件消费者同样不可见。

**复制幂等性（重复发 copy 的后果）**

- **F11 重发安全，三层防护都在 CopyStart（`master_service.cpp:6444`），不依赖任何事件**：
  1. **目标 seg 已有副本 → 跳过**：按 seg 粒度去重——`GetReplicaBySegmentName(tgt) == nullptr` 才计数（`:6498`）、才分配（`:6595`）；全部目标已有副本 → 返回空 targets → 客户端 `replica_copy_skipped / target_replicas_already_exist`，按**成功** no-op（`client_service.cpp:4363`）。同 seg 不会产生第二份同 key 副本。
  2. **同 key 有在途任务 → 拒绝**：`HasReplicationTask()` → 第二个 `CopyStart` 失败 `OBJECT_HAS_REPLICATION_TASK`（`:6488`）——并发重发不会建两个任务；但这是**任务失败**（`query_task` 可见）不是静默成功，控制面重发时要把该错误码按「已在途」处理，不当失败。
  3. **部分重叠按 seg 粒度**：targets 中部分 seg 已有副本 → 只给缺失的 seg 建副本，非全有或全无。
- **F12 提交层无去重**：`CreateCopyTask`（`master_service.cpp:13677`）只校验 key 存在 + 目标 seg 可分配，不查目标已有副本、不查在途任务——两个 `create_copy_task` 都会拿到任务 ID；去重发生在源属主执行 `Copy→CopyStart` 时（F11），时序：先执行者赢，后者按 F11-2 失败。
- **F13 同机不同 seg 的重复是设计内**：防护是 per-(key, seg)；同机多实例各有 seg，同 key 在同机不同 seg 各一份是异构同机的正常工作形态（README 异构 TP 节）。key 不同的内容重复（不同模型/hash 命名空间）不在防护范围。

## 设计决策

### 1. 选路输入：三张表

| 输入 | 内容 | 来源 | 依据 |
|------|------|------|------|
| 前缀 → keys | block hash 链 × PoolKey 全 rank 展开 | `precopy/keys.py` 口径（import 上游权威 hash 链；`PYTHONHASHSEED` 对齐） | F2 |
| key → 副本位置 | 每把 key 的副本落在哪些 seg | master | F1 |
| seg → 实例 | seg 名（`IP:rpc_port`）↔ 实例 ↔ rank | 生产由编排层注册上报（测试床 `resolve.py` 探测链是取证手段，不是生产路径） | 归 D4 |

要点：

- **本地命中判定**：前缀的**连续满块**集合中，目标实例**每个 rank 的 key 都在本机 seg 上有内存副本**。全 rank 齐备 = 零跨机；部分 rank 本地 = 按本地块比例计入打分（F3：get session 每 rank 独立选副本，部分本地仍省对应 rank 的传输）。
- 连续口径：中段有空洞不构成零传输前缀（同 `modeselect.go::contiguousLocalHitBlocks`）。
- 命中 token 数 = 连续满块数 × block_size，尾巴由引擎重算（801 token 命中 768 口径，README「三个对象」）。
- MLA（`put_step>1`）key 折叠到 rank 0 → 本地判定退化为单 rank；该映射未实测（README「边界与已知限制」）。

### 2. 可见性：直读 master 权威（必然，不是优选）

三条路径：

- **a. 自产事件**：预复制控制面复制完成/失效时向 Router 发事件
- **b. 池侧 publisher**：master 推送副本变更事件流（Mooncake master 无事件流，需自研）
- **c. 直读权威**：Router 持 key→replicas 只读镜像，周期 reconcile master + 预复制动作完成后即时刷新对应条目

**选 c 为主路径**；a/b 只在查询规模成为瓶颈时作增量优化层，不作权威。理由：

- master 已是放置权威（F1），Router 视图 = 权威视图的镜像，不需要「逼近」谁——推测索引是派生索引系统的补窗手法，权威视图系统没有决策-确认窗口（参考实现·llm-d）。
- 引擎 kv event **结构上**不能作来源：预复制副本无事件（F9）、副本消失无事件（F10）——事件驱动的 router（Dynamo `KvIndexer` / llm-d EPP / AIBrix 路线）对预复制必然盲且必然陈旧。
- 事件流自带丢失/乱序/gap 失效模式，Dynamo 为此配了序号 + 快照重放整套兜底（参考实现·Dynamo）；直读没有该失效模式。
- b 要改 master（自研事件流），与「不改池」边界冲突。

陈旧两个方向都只损性能：

- **正向**（副本已就绪、镜像未刷新）→ 错过本地命中，退为普通池命中。
- **反向**（镜像有、实际已被驱逐/连坐移除）→ 请求打到实例后 get miss，引擎自动远端读/重算，不损正确性（F3 local-first 是读路径既有行为；同 `prefixHint` 反向陈旧不查的口径）。READY 核验（F4 `batch_get_replica_desc`）保证写入镜像的条目在写入时刻为真。

集成约束（归 D4）：若最终部署的 router 框架消费引擎 kv event（Dynamo 路线即如此），预复制视图必须**带外注入**该框架（自定义 producer/scorer），不能指望事件流（F9/F10）。

### 3. 事件身份：三种「KV 在哪」的说法不能混

- **master 视图**（权威，F1）：key → 副本 → seg。预复制副本唯一完备的信息源。
- **引擎 kv event**（引擎动作的自白，F7/F8）：只覆盖引擎自己的 put；无 Removed（F10）；引擎 APC 命中不进任何事件（F6）。
- **控制面事件**（预复制动作的自白，路径 a）：控制面知道每次 copy 的结果，可主动发「就绪/失效」——身份与引擎事件不同，**不能混进引擎事件流**；只作镜像刷新触发，不作权威。

Router 可见的「本地命中」只按 master 视图定义：**目标实例本机 seg 有 DRAM 副本**。

- 打分上本地 DRAM 副本 ≈ Dynamo 分层命中的 host 层（device 1.0 / host 0.75 / disk 0.25，参考实现·Dynamo）：省跨机 RDMA、不省 D2H 加载。
- READY 语义：READY = 本机 DRAM 副本核验通过（F4），不含 HBM；HBM 由真实请求的 get session 读后驻留 APC，属引擎自治（F6）。

### 4. 时序：路由决策驱动，两个回路

最终形态是 Router 把请求发给实例——因此预热 intent 的**主来源就是路由决策本身**：「Router 决定这个请求（会话）去哪个实例，precopy 就预热哪个实例」。两个回路并存：

- **会话级回路（预测式，主）**：会话首次路由按负载/亲和选实例 → 决策上行 → 控制面预热该会话前缀到选定实例 → **后续轮次**本地命中。agent 多轮场景的主收益在这里；首轮赶不上预热是预期行为（走兜底）。
- **请求级回路（回报路径）**：镜像里已有本地副本 → Router 优先选该实例——预热收益经视图回流兑现。

预测式主路径：

1. 预热 intent 到达（路由决策上行，或热度观测辅助，见 §6）
2. 控制面算 key → `create_copy_task` → `query_task` → `batch_get_replica_desc` 核验 → READY（F4）
3. Router 镜像刷新——该前缀对目标实例本地可见
4. 后续同前缀请求：镜像查中 → 直接选该实例

intent 去重与重试安全：控制面以（前缀 × 目标实例）为幂等键，在途不重复发、完成不重复发；即便重发也由 F11 兜底（同 seg 跳过 / 在途报 `OBJECT_HAS_REPLICATION_TASK`）——重发安全，不产生重复副本。

反应式兜底：选了实例但副本未就绪（未预取 / 在途 / 镜像正向陈旧）：

- 请求照常下发，get session 建立时 local-first 自动选现存副本，远端则远端读——读路径既有行为（F3），**不阻塞 TTFT**
- 控制面观测后异步补复制，后续请求受益
- session 锁副本是 per-request 的，错过只影响当前请求（F3）

**不设等待式选路**（hold 请求等副本就绪）——READY 栅栏归编排放行（[kv-prefetch.md](kv-prefetch.md)「与请求调度联动」），不塞进请求热路径；时序错过由读路径兜底，不由阻塞兜底。

### 5. 目标函数

候选实例分级：

1. 全 rank 连续本地副本（零跨机 RDMA）→ 选倾最强
2. 池远端副本（省重算，需跨机读；layerwise 下逐层远端更贵）
3. 无命中（全量重算）

打分 = 命中收益 − 传输/加载成本 + 负载惩罚（成本模型参照 Dynamo，参考实现）：

- 命中项：连续本地满块数 × host 层权重（初值借 0.75 量级，真机校准）；池远端命中按池命中权重另计
- 负载项：实例队列长 / in-flight 由 Router 本地记账（在途负载无权威，router 自己加减，参考实现·Dynamo）
- 传输成本：跨机读 ≈ 前缀字节 / 有效带宽；与等效重算成本比较，不划算则换实例重算（lake 带宽闸同构，`SelectExecMode` 的 `bwGBps` 阈值）

与 lake 差异：不选执行模式（vllm-ascend 没有 D-direct），输出 = **选实例** + （可选）**是否先触发预复制**。

### 6. release 与放置的单向边界

- Router 不指挥放置与释放：副本生命周期归池策略（TTL / 驱逐；副本无 pin，F5）。
- Router 只出两类 hint，控制面自主执行：
  - **预热 intent**：路由决策（选定实例 → 预热到该实例，§4 会话级回路）+ 热度观测（辅助）→ 控制面决定复制到哪、何时复制
  - **释放 hint**：会话生命周期（结束 / 迁移 / 超时）→ 控制面评估后按 D2 操作集释放（`Remove` / `BatchRemove` 现成）——Router 知道「谁还会用」，池知道「谁还在」，释放决策需要两者，故 hint 上行、执行在控制面
- 结果经 master 视图回流 Router 镜像——「池放置·调度读视图」单向耦合在本系统的形态；差异在放置智能的位置（池内 vs 外置控制面），边界方向不变。
- 已知缺口（登记，不归 D1）：master 无显式 pin API（lease 机制 → D2）；属主下线副本连坐移除（F5 → D3/D5）。

## 决策小结

| 问题 | 选择 | 依据 |
|------|------|------|
| 选路输入 | 前缀→keys（keys.py 口径）× key→replicas（master）× seg→实例（编排注册） | §1；F1/F2 |
| 可见性 | 直读 master 权威 + Router 只读镜像——引擎 kv event 结构性盲区（F9/F10），事件路径不可行 | §2；F1/F9/F10 |
| 事件身份 | 三种身份不混：master 视图（权威）/ 引擎 kv event（引擎动作）/ 控制面事件（预复制动作）；本地命中只按 master 视图定义 | §3；F6-F10 |
| 时序 | 路由决策驱动预热（会话级回路为主）+ 请求级回报回路；反应式读路径兜底；不设等待式选路 | §4；F3/F4 |
| 目标函数 | 本地副本 > 池远端 > 重算，叠加负载与传输成本 | §5；参考实现·Dynamo |
| release | 归池策略；Router 给预热/释放两类 hint，控制面执行；单向边界 | §6；F5；参考实现·lake 单向边界 |

## 留给后续

编号登记处（含 P/E 系列、状态与依赖）：[backlog.md](backlog.md)。

- **D2**：KV 调度操作集（pin/demote/prefetch/retain/release；master 无显式 pin → `SubmitReplicaActionProposal` lease 机制；释放有 `Remove`/`BatchRemove` 现成）
- **D3**：副本生命周期与 READY 语义（F5 连坐移除、TTL、属主下线后的副本归属）
- **D4**：编排进程形态（预复制控制面独立进程或并入 Router；READY 栅栏接 gateway；seg→实例注册通道；**router 框架若消费引擎 kv event，预复制视图带外注入**，§2 集成约束）
- **D5**：故障衔接（源属主掉线 copy 失败（F4）、与重路由的先后）
- **D6**：layerwise 选型（[../docs/research/vllm-ascend/layerwise-taxonomy.md](../docs/research/vllm-ascend/layerwise-taxonomy.md)）
- **D7**：方案 A（壳请求进 HBM）去留与叠加顺序
