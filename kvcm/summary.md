# kvcm 总结（summary）

来源：[issue #54](https://github.com/5x8-40/lake/issues/54)（KV-CM 遗留问题总清单）。本文是 kvcm 预复制生产化的总结文档：设计决策、潜在问题、工程项三类条目的总登记处——已闭环条目在原地标 ✅、结论指向正式文档，待定条目随讨论与实验更新。

文档分层（详见 [README.md](README.md) 导航）：`design/` = 全局设计（D1/D2/code-architecture）与专项设计（kv-prefetch，仅 copy）；`demo/` = precopy demo 记录（仅验证 copy 一项的端到端链路）；本文 = 登记处。

编号：**D** = 设计决策（语义/选型未定，先决策）；**P** = 潜在问题（待评估）；**E** = 工程项。编号创建后不复用；条目闭环后在原位置标 ✅，结论落对应正式文档（`design/` 系设计文档 / `demo/` 系 demo 文档）。

依赖主线：D1（已闭环）/ D2 是设计主线（E5 已闭环，结论见本文 E 节）；E10→E11→E12/E13/E14 是服务化主线（重构先行、协议随后）；P1-P8 大多随 D1/D2 定型一并回答。

状态标记：✅ 已闭环（附结论写在哪个文档）｜🟡 部分闭环｜无标记 = 待定。

## 一、设计决策

**D1. Router 集成与请求调度** — ✅ 已闭环

最终形态：router 决策请求发哪个实例 → precopy 预热该实例 → 并决定哪些 KV 释放。工作文档：[D1-router-scheduling.md](design/D1-router-scheduling.md)（事实基线 F1-F15 已核实，含复制幂等性与 lease 语义；六问全闭环，「待定问题」已转「设计决策」）。

六个问题（全部闭环，结论写在 D1 对应小节）：

1. **选路输入**——✅（D1 §1）
   - 选路逻辑分两层：
     - 索引：订阅事件流，维护「block hash → 实例」的表
     - Router：自己算请求的 hash 链去查索引（RFC #1403 `/query` 支持 token_ids 或 hash key 两种入参，不需要原始 token 文本）
   - 索引的数据来源两个：引擎原生事件 + kvcm 补发（预复制放置）；kvcm 负责补齐引擎看不到的部分
   - Router 不访问池 key——池里的位置信息是控制面的事
   - 索引做成什么进程形态（Router 内置 / 独立进程 / 内置但对外暴露查询——全量索引只有一份；辅助预测树单独决策）与事件开关、传输接线归 D4
2. **可见性**（索引怎么知道 KV 放在哪、删了没）——✅（D1 §2）。拆两问：
   - **放置** = 方案 a：控制面补发 `BlockStored` 事件
     - 为什么只能补发：池事件不带机器位置、复制路径本身不发布任何事件（任何版本都如此，F14）
     - 补发时机：READY 核验通过后，即「全 rank 都备好、可以路由」——master 的单 key 粒度给不了这个判断
   - **删除** = 升级 mooncake、开 master publisher
     - 首选：索引侧直接订阅 publisher，解析 PoolKey 得出 block hash（Conductor `ParseVllmObjectKey` 现成；Dynamo `shared_cache.rs` 自己维护 hash↔key 映射，思路相同，F14）
     - 备选：kvcm 订阅后翻译成引擎格式重发
     - 前提：0.3.11 不含 publisher，须升级（E8 已核）
   - 明确不做：轮询比对（只为删除信号不值得）、索引条目 TTL（与放置无关的独立兜底，不需要）、Router 直读 master（排除）
   - 方案 a 的复用要点：事件字段全是 keys.py 的中间产物，发布直接 import 上游 `ZmqEventPublisher`；事件身份 = 端点（每个目标实例一个补发端点，「端点 → 实例」注册归 D4/E13；索引做成独立进程时走 RFC #1403 `/register` 注册）
3. **「哪个实例拥有前缀」怎么定义**——✅（D1 §3）
   - 实例 X 拥有前缀 P：P 的连续满块链的 KV 在 X 本机——HBM（gpu 事件）和本机池 seg（cpu 事件 / kvcm 补发）都算
   - 命中长度 = 连续满块数 × block_size
   - HBM 与 DRAM 的差异留到 §5 打分处理
   - rank 维度不用管：事件只在全 rank 齐备时才发，Router 不需要 per-rank 索引
4. **时序**——✅（D1 §4）
   - 请求不等 READY：照常走 F3 兜底链（副本在池里就远端读），复制在后台追
   - 复制的三个时机并存：
     - 顺手补：请求路过时顺带触发
     - 提前备：前端 agent/coder 钩子在请求发出前先把信号给控制面（最终形态，归 D4）
     - 直接用现成的：Router 前缀亲和本来就倾向把请求发到已有所需 KV 的实例
   - intent 以（前缀 × 目标实例）为幂等键去重
   - 边界：kvcm 不决策选路；Router 不直接下复制命令，用 intent 表达意图（Router → kvcm 下行）；事件上行（kvcm → 索引）
5. **选路目标函数**——✅（D1 §5）
   - 归 Router 内部实现，kvcm 不参与决策（§1-§3 之后 Router 的输入已齐）
   - 候选已列：分级兜底 / 打分制（Dynamo 的分层成本可借来当初值）/ 传输成本两种算法
   - 权重待真机校准（P7）
6. **release 归谁**——✅（D1 §6）
   - 分工：Router 提供会话生命周期线索，控制面决策执行，池驱逐兜底
   - 三个手段：
     - 主路径：停保活（软释放——有读就自动续租，动不了正在用的数据）
     - 中间级：demote 降层（DRAM→SSD，不删，归 D2）
     - 例外：主动 `Remove`（key 是全局的，别的实例上的原始副本会一起删）——仅限「确认没人用」

已知现状：

- 引擎事件通道上看不到池副本删除（F10）→ 索引里池副本条目只增不减，条目过时只损性能（F3）
- GPU 侧有 Removed 事件（F8）
- 池原生 publisher 有 removed 事件，但 0.3.11 不含、且载荷只说 key 怎么了、不说在哪台机器（F14，E8 已核）——升级后首选索引侧直接订阅解析（D1 §2 方案 b），kvcm 翻译重发为备选
- 选路方进程形态与 D4 耦合；E11-E14 依赖设计定型，E10/E11 骨架可先行

**D2. KV 调度操作集（pin / demote / prefetch / retain / release）**

独立控制面进程对 KV 的完整操作面，每项定语义、与池驱逐策略的优先级、失败处理：

- 操作集框架：✅ 已闭环（结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §1）——八项操作 copy/move/keepalive/pin/demote/promote/retain/remove（原子动作 / 元数据标记 / 目的型三类），复合流程由原子操作组合（prefetch = copy+核验+保活注册；会话结束降层 = 停 keepalive+demote；下线迁移 = copy+remove 或 move 一步）；状态记录四类（任务状态机 / 保活注册表 / 副本台账 / 会话绑定）；kvcm 单实例跟随 master，状态存 sqlite。
- prefetch：✅ 已验证（`CreateCopyTask` → READY）。
- pin：READY 后防驱逐——🟡 候选已列出，结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §2
  - soft pin 级（驱逐时排最后，极端情况仍可逐）——空写 upsert 一个方案：
    - 好处：不动全局 lease、断线窗口 30 分钟级
    - 代价：空写通道（Python 绑定没有分离接口）+ client_id 问题
  - hard pin 级（绝对不可逐）——两个候选：
    - ① lease 调大 + keepalive：零改动、零client_id 问题；代价 = 全局读租约变长 + kvcm 必须活着续租
    - ② 读删写：kvcm 失联也安全；代价 = 数据要经过 kvcm 本地 + client_id 问题
  - 取消方式：① 停续租等过期 / 空写传 false 或 `SoftPinAction::DISABLE` / hard pin 没有 unpin，只能再读删写一遍
  - 读锁方案已否决：refcnt 是任务级保护，两个版本都没有读者引用 RPC
  - client_id 问题两个方案并列（空写与读删写都要面对）：
    - client_id 改归 kvcm：不改 mooncake；语义变（Revoke/`BatchReplicaClear` 权限归 kvcm，kvcm 死期间锁死；死透时逃生门 = 任一 client upsert 把 client_id 改写成自己、接管权限，upsert 不验身份）；与副本存亡无关——两方案下引擎下线行为一致（E5 已闭环）
    - 借原 client_id UUID：语义不变；需 master 加「key → client_id」查询 RPC（内部数据现成）
    - 决策条件：mooncake 能不能改（上游 PR / fork，独立成本）+ kvcm 实现语言（E10/E11）
  - 机制事实：见 D2 文档 F1-F12（pin 形态、upsert 行为、client_id 管辖、lease 与驱逐、UUID 与空写通道）
- retain：🟡 机制已定，结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §4
  - 正式路径 = main 的两条池机制并列：
    - retained client：断线后数据保留、等重连接管（`AddReplicaForRetainedClient` + client 库 `BufferRegistrationAction::kRetained` 注册）——应对引擎进程下线；kvcm 用同一个库也能注册
    - DFS 副本：写共享 3FS，不随 client 死被清——应对整机灭失；前提是部署 3FS
  - 0.3.11 下 demote 解决不了 retain：盘副本绑引擎 client，引擎死了照样清
  - 0.3.11 的过渡办法（E5 已闭环）：move 到活 seg 有效；改 client_id 对副本存亡无效（只动鉴权字段）；kvcm 自挂 SSD 兜底（不依赖任何引擎存活；main 下 kvcm 以 retained 身份注册，则 kvcm 自己死了数据也保留）
  - 计划下线：`GracefulUnmountSegment`（0.3.11）/ `CreateDrainJob`（两版本都有，job 级可取消）
  - 长期未命中 → demote 到 SSD，由策略层定
- demote / promote：🟡 设计完成，结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §3
  - 框架：语义是原子的，实现是组合动作（池通道或 kvcm 读写组合 + 核验）；存储统一归 mooncake（引擎实例挂本机 SSD 做分层，kvcm 自己不持存储）
  - demote = 池开 `enable_offload`：
    - 写穿 / 写回两模式（≈ HiCache write_through / write_back）
    - 写回模式须先停保活、等 lease 过期——保活挡住驱逐，也就挡住落盘
  - promote 三条路：
    - `promotion_on_hit`：命中时池被动提回
    - kvcm 主动①：反复调 `GetReplicaList` 看命中计数，过线就触发
    - kvcm 主动②：读删写自己搬
    - promote 执行端 = 引擎来取（`PromotionObjectHeartbeat`，两版本都有，触发链不变）
  - copy/move 到不了 SSD（disk seg 没有 seg 名，两版本同）
  - 触发时机（会话结束 / 变冷 / DRAM 压力 / 计划下线）归策略层
  - 真正的功能缺口：写回模式下确定性的 demote——没有 per-key 降层 RPC，`BatchReplicaClear` 又限 key 的 client_id，offload 取消也不对外 → 需要池侧扩展
  - 远期：kvcm 自己挂 SSD + `NotifyOffloadSuccess` 上报登记（`AddReplica` 不是独立 RPC，是它的内部步骤）
  - 每个操作都有完成信号；READY 特指 DRAM 副本写好、可以调度
- release：✅ 已闭环，结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §4
  - 决策归属 D1 §6
  - kvcm 可用：`Remove` / `BatchRemove` / `RemoveByRegex`（要配 key 命名空间才有意义）/ `RemoveAll`
  - `BatchReplicaClear` 限 key 的 client_id 自己调——kvcm 动不了别人的 key
  - `EvictDiskReplica`（两版本）是通知型：`LOCAL_DISK` 限登记 client（kvcm 自挂 SSD 后可用来收尾）、`DISK` 不验 client_id（误用风险已登记，不作正式路径）
  - `force` 仅限「确认没人用」
  - 收尾 = 清台账 + 同步索引
- 机制查缺补漏：✅ 已闭环（结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) F13⑤/③、§1 对照表、§3 远期方案、§4 retain/release）
  - 做法：八项操作逐项过一遍「还能利用什么池机制」+ 三版本 RPC 注册表全量 diff（v0.3.11 与 v0.3.11.post1 完全相同）
  - 机制结论：
    - 动态副本（main）：kvcm 能触发、不能指定目标（反复调 `GetReplicaList` 观测，和 promote 同一种做法，观测在同一个函数里）——只适合池级自动调节，满足不了预热语义
    - offload / promotion 的执行端两版本都是引擎来取（`OffloadObjectHeartbeat` / `PromotionObjectHeartbeat`），kvcm 触发链不变
    - offload 取消不对外（`CancelQueuedOffloadTask` 唯一调用点是 `UpsertStart` 内部）
    - `EvictDiskReplica`（两版本）：通知型；`LOCAL_DISK` 限登记 client（kvcm 自挂 SSD 后可用来收尾），`DISK` 不验 client_id（误用风险已登记）
    - `AddReplica` 不是独立 RPC（是 `NotifyOffloadSuccess` 的内部步骤）——kvcm 自挂 SSD 走 `NotifyOffloadSuccess` 上报登记
    - retained client 注册在 client 库（`BufferRegistrationAction::kRetained`），kvcm 同库可注册——main 下 kvcm 以 retained 身份注册，kvcm 死亡数据也保留
    - `BatchQueryIp`（两版本）：client_id→IP，副本位置解析的最后一环
    - `GetStorageUsageSnapshot`（main）：分层容量快照，demote 触发输入
    - 台账失效通知（main）：KV 事件流 removed/cleared；stored 事件没有位置信息，代替不了定期比对
    - `MountNoFSegment`：挂盘形态（盘不绑机器）
    - `ReMountSegment` 排除：重连时重新登记，不是复活旧副本
    - 租户配额五件套（main）：tenant 在 `Client::Create` 绑定，client 级粒度，正好匹配「一个模型版本一个租户」
  - 否决项：
    - Upsert：同 size 原位复用 buffer，只剩盘副本时写盘 buffer 提不回 DRAM，两版本同行为
    - copy / move / keepalive / pin 没有新机制可用
  - 教训：「用不了」不等于「不需要懂」——对照性机制要挖到能回答「为什么不是它」的深度
- 事实输入：E6（已闭环，见本文 E6 条）。

**D3. 副本生命周期与 READY 语义** — 🟡 部分已定（READY/保活机制/副本存亡已定；保活策略未定）

- 事实：
  - 副本没有 pin，READY 后池仍可驱逐
  - 任一 client 下线后，master 只清它 seg 上的副本（含其登记的盘副本）；他机副本保留、继续可读——副本存亡绑 seg 所在 client，不绑写入者（E5 已闭环：10-20 复验 32/32 + 字节级读回，[EXPERIMENTS.md](demo/EXPERIMENTS.md) 总表 #15）——D1 F5
  - lease 是 key 粒度：读会续租、`CopyStart` 不续租、TTL 到期或驱逐时删整个 key、释放全部副本——D1 F15
- READY 语义：✅ 已定型（D2 §1 完成信号，八项逐一点名）
  - 什么是 READY：目标实例的 DRAM 副本写好、kvcm 核验通过后，上报 Router「可以把请求调度过来」
  - 谁有：copy / move / promote 三个——做完后某实例新具备「请求发过来就能直接用本机 KV」的条件
  - 谁没有：keepalive / pin / retain（不动数据）、demote（只记台账）、release（通知索引删除）
  - 「本机」= 本机 HBM + 本机池 seg 两层（D1 §3）
- 保活：🟡 机制已定、策略未定（机制 D2 §1/F9，事实 D1 F15）
  - 机制已定：keepalive 周期调 `ExistKey` 续租约（lease）——`batch_is_exist` 调用会顺带续租（`ProbeKey` 不续），TTL = master 配置（D2 F9），保护窗口 = 租约时长
  - 预复制不续租的话，一个 TTL 窗口内没有读流量就可能被驱逐
  - 约束已定：保活循环必须有界——无限续租事实上等于 pin；kvcm 断线后租约到期自然失效，不残留
  - 策略未定：续多久、何时停——候选 = 会话期 / 固定窗口 / 首流量后停租，未选定
- key 的 client_id 下线后的副本存亡：🟡 机制已定（D2 §4 retain）——main 有两条池机制并列（retained client / DFS 副本）；0.3.11 过渡手段 E5 已闭环：move 到活 seg 有效、改 client_id 对副本存亡无效、kvcm 自挂 SSD 兜底
- 计划下线迁移：归 D2 retain 条（`CreateDrainJob` 两版本都有，把指定 seg 上的数据迁空到目标 seg，带并发上限、job 级可取消）
- 落盘（写到 SSD）：Mooncake 的 DRAM→SSD 是池内冷热分层（client 侧 `enable_ssd_offload`，默认 `false`），不是「预复制时直接指定写盘」；SSD 只作容量/恢复用的异步 demote 去向（机制归 D2 demote 条）
- 直接影响故障恢复时已预热前缀的可用性（D5，已闭环，见本文）。事实输入：E5（已闭环，见本文 E 节）。

**D4. 编排集成**

- 待定：READY 信号如何接 gateway/router：谁发起预取、何时放行、失败重试。过载/准入仍归 gateway，推理侧只管执行。
- 待定：Prefetch Orchestrator 形态（独立进程或并入 Router）：收 intent → 调 B2 → READY 确认 → 放行。
- 边界（随 D1 §2 方案 a，已闭环）：索引消费引擎 kv event，预复制放置由控制面补发事件注入（D1 §2）；控制面侧的 seg→实例拓扑由编排层提供（测试床里的 resolve.py 探测链只是排查手段，不是生产路径）。
- 已定：~~kvcm 兼做索引~~（否决）——kvcm 额外维护一整套索引（前缀树、查询服务、HA）等于把 Router 侧的索引系统重造一遍（Conductor / Dynamo standalone indexer 都是现成的），还会把 kvcm 从后台控制面拖成在线服务；kvcm 保持纯池控制面（复制 / 保活 / 释放 / 补发事件），「告知放置」只走补发事件（D1 §4 职责边界）。
- 待定：消费侧索引形态——**Router 团队选型，kvcm 接口不变**（D1 §2 方案 a 与形态无关：一套端点、一套引擎格式，kvcm 零适配零分支）
  - 方案 A：Router 内置索引
    - 先例：llm-d EPP / Dynamo kv-router——索引与打分器紧耦合、请求路径少一跳
    - 代价：订阅 / 索引 / 事件流断档处理都要自己写
    - 变体：Dynamo remote indexer——内置但对外暴露查询服务，多个消费方共用
  - 方案 B：独立进程跑 Conductor
    - 现成：object_key parser、RFC #1403、vLLM 格式解码（`msg_decoder.cpp::ParseVllmEvent:365` 已核实）
    - 好处：多事件源聚合、独立 HA
    - 代价：请求路径多一跳 HTTP；同步选路路径有毫秒级延迟预算
  - **辅助预测树**单独决策（与上面的选型独立）：
    - 问题：同前缀的请求并发到达、引擎还没来得及发 stored——索引里查不到，这批请求互相错过
    - 先例：Dynamo predict-on-route 侧索引（短 TTL、取 max）、llm-d 推测索引（TTL 2s）
    - 侧树为什么要分立：hash 口径不同会污染主树；kvcm 补发与引擎同口径（keys.py），无此约束（D1 §2 方案 a）
- 登记：mooncake 自家 Conductor（`mooncake-conductor/`，C++）是 RFC #1403 索引契约的现成实现，与 kvcm **职责互补、不重叠**
  - Conductor = 独立索引进程：订阅引擎/池事件建全局前缀表，HTTP `/query` 支持 token_ids 或 hash key 两种入参；含 object_key→hash parser；不复制、不保活、不释放
  - kvcm = 池控制面：复制 / 保活 / 补发事件 / 释放——这些 Conductor 都没有
  - 生态：
    - Dynamo kv-router 直接订阅 master publisher 消费（`shared_cache.rs::HicacheSharedKvCache`）
    - 华为 MindIE Motor 官方「KV Cache 亲和性调度」= Conductor + Coordinator 查询
    - 社区试用：Discussion #1967（D1 F14 消费者现状节）
  - 与 kvcm 的重叠只有一点：object_key→hash 解析（代码级对照 `object_key_parser.cpp`）
  - 复用路径两条：
    - 组件级：索引直接用 Conductor——方案 b 的删除信号它自己订阅解析，kvcm 的翻译层消失；方案 a 的补发仍需要（经 RFC #1403 `/register` 注册端点即被它消费）
    - 代码级：自研索引则对照它的 parser
  - 待评估：成熟度（0.3.11 不含、随升级获得）、在线路径 HTTP 查询延迟 vs 进程内索引、HA、与 §5 打分逻辑的结合
- 是 D1（已闭环）时序问题的执行面。骨架先行：E12/E13。

**D5. 故障衔接** — ✅ 已闭环

- 事实：copy 期间源实例掉线即失败（master 把任务派给源 seg 所在 client，D1 F4）。
- 事实：源 seg 所在 client 宕机后，在途任务标记由 master 的定期清理线程（reaper）清掉（`master_service.cpp:10653-10700` @main）；清理完成前重发同一 copy 会收到 `OBJECT_HAS_REPLICATION_TASK` 报错（幂等语义见 D1 F11-F12）。
- 半成品副本的清理：✅ 已核实（两版本同行为）
  - 半成品副本 = copy/move 已把数据写到目标、但还没标完成的副本
  - master 的定期清理线程（reaper）发现任务超时（TTL = `put_start_release_timeout_sec_`）后，按任务记录的 `replica_ids` 精确丢弃这些半成品：
    - key 还有其它完成态副本（copy 场景的源副本）→ 保留 key，只丢半成品
    - 半成品是唯一副本 → 整个 key 删掉
  - 证据：`master_service.cpp:10653-10700` @main（清理动作写操作日志，主备切换后可恢复）；`:3259-3309` @0.3.11（注释原话「Discard expired CopyStart/MoveStart operations」）
  - 目标 seg 上占的物理内存：副本从 master 元数据丢弃后，由引擎自己的内存管理回收
- 故障重路由衔接：✅ 已定——两件都做，各管一段
  - 请求侧 = 容忍本次远端读：Router 按当时的集群状态把请求发到别的实例；前缀副本还在池里就跨机远端读——慢、不出错（F3）。这段不用 kvcm 做任何事，改道是 Router 每次选路的本职（D1 §4 职责边界）
  - KV 侧 = 重新预热：kvcm 按 intent 对新目标实例重新发起 copy（intent 幂等，同一前缀同一目标重发不重复搬，P2）
  - kvcm 在故障里的动作只有「收 intent、重新预热」，和平时没有区别——不需要故障专用预案；失败 copy 留下的在途任务标记与半成品副本由 master reaper 清（上两条已核实），也不用 kvcm 兜底

**D6. layerwise 选型**

- 候选：Mooncake block_key（逐层 range 读，HBM 仍要全层；`backend=mooncake` + `use_layerwise=true`）vs MemCache gva（HBM 可以减量，预热数据更多落在本机 DRAM；`backend=memcache`）。
- 待定：生产默认走 MemCache，还是留 Mooncake 并补测 layerwise + B2（get session 是否仍选本机 DRAM）。不建议自研 Mooncake GVA。
- 三个「layerwise」机制完整辨析：[../docs/research/vllm-ascend/layerwise-taxonomy.md](../docs/research/vllm-ascend/layerwise-taxonomy.md)。

**D7. 方案 A 去留**

- 待定：壳请求（方案 A 的预取请求：`max_tokens=1`、不真正生成、只为把 KV 带进 HBM，定义见 [kv-prefetch.md](design/kv-prefetch.md)）这条路是否保留、与 B2 的叠加顺序。

## 二、潜在问题

- **P1. KV 与模型版本绑定**——vLLM block hash 不含权重 revision，权重更新后旧 KV 会被误命中、输出错了也不报错。key 命名空间必须含 model_id/revision（要求已写入 D2 §4 release；落地随 kvcm 实现，E11/E12）。
- **P2. 并发预热去重**——🟡 池层幂等已核实（D1 F11-F13：重发不产生重复副本，在途报 `OBJECT_HAS_REPLICATION_TASK`）；仍待做：控制面（前缀 × 目标实例）幂等键合并，避免多请求/多 router 实例并发触发时重复占带宽。
- **P3. 快照语义**——copy 期间源前缀还在增长（在途请求继续产生 block），precopy 是快照，快照点之后的新 block 谁补。
- **P4. 读与驱逐竞态**——🟡 部分已由 D1 F15 回答：读路径（`BatchGetReplicaList:4566` / `GetReplicaList:4401`）调用即给 lease（时长 = master 配置，D2 F9），从查询到读完的窗口受 lease 保护（驱逐候选必须等 lease 过期）；副本级 refcount 与长读请求的语义仍待确认（与 D2 retain 耦合）。
- **P5. 多租户与配额**——多模型共池的隔离。tenant quota 五件套只在 main 有，0.3.11 没有——要用须升级 mooncake（D2 F13）；tenant 在 `Client::Create` 绑定，client 级粒度，正好匹配「一个模型版本一个租户」。
- **P6. 鉴权边界**——现状能连 master 就能 `create_copy_task`/`Remove`。生产需要接入控制；控制面权限应大于引擎（引擎只需读写自己 seg）。
- **P7. 预热成本核算**——预热后请求没来或去了别实例的浪费；READY 延迟（带宽×块数）vs 请求到达时间的窗口预测。与 D1 §4/§5 耦合（已闭环，口径见 D1）。
- **P8. 指标归因**——命中率要拆「预热贡献 vs 引擎内部 HBM cache」：测试床已遇到第二次命中被引擎内部 cache 吃掉（外部 get=0），生产监控同样会混淆。
- **P9. prefetch 终止**——请求改道/源消失时在途 copy 白白占用带宽。对照 HiCache `can_terminate_prefetch` 三策略；mooncake 0.3.11 只有 `CancelDrainJob`，copy/move 无取消 RPC——补法：池侧扩展（改 mooncake）或 kvcm 弱版（弃置 + 事后清副本）。来源：D2 操作集完备性审计（[D2-kv-operations.md](design/D2-kv-operations.md) §1）。

## 三、工程项

### A. 验证类（可直接开工，除注明外）

- **E1. 真机冒烟**——`demo/run_e2e.sh` 一键（同构 TP=2 + 异构双向）+ 同构跨机 rank↔seg 新路径重跑。实质路径已手工验证，单测 19 例 + dry run（不真正搬数据）覆盖，风险只剩编排层。
- **E2. key 一致性校验在容器内跑通**——`test_keys.py` 里有一个「上游漂移哨兵」用例：把 keys.py 算出的 key 与真实 vllm-ascend 的 PoolKey 逐字节比对，红了说明上游改了 key 格式。该用例要在 vllm-ascend 容器内跑（运行期 `batch_is_exist` 32/32、36/36 证据已齐）。
- **E3. 多卡自动匹配验证**——resolve 的 `VLLM::Worker_TP<N>` proctitle 匹配在多卡真机走通（现在是 `--targets` 手工指定对应关系；匹配失效时必须明确报错，不能默不作声地配错）。
- **E4. 单 token 不一致的根因**——输出里偶发有一个 token 与预期不同：旧非 DEBUG 实例偶发，新实例未复现；首次外部读的输出逐字一致，怀疑是不同执行路径的数值扰动。生产化前必须查清（[EXPERIMENTS.md](demo/EXPERIMENTS.md) #10）。
- **E5. 副本消失机制核实**——✅ 已闭环（10-20 受控复验，结论落 D1 F5 / [D2-kv-operations.md](design/D2-kv-operations.md) F6b）：副本存亡绑定所在 seg 所在 client（副本所在 client），key 的 client_id 下线只清其自己 seg 上的副本，远端副本保留且可读（kill A → TTL 过期 unmount → `batch_is_exist` 32/32 + 跨机字节级 get 3/3；master 日志零驱逐零 remove）。早期「key 连同全部副本消失、exist 0/32」是 key 名漂移假失败（控制面未带 `PYTHONHASHSEED`，[EXPERIMENTS.md](demo/EXPERIMENTS.md) 踩坑 1/3、总表 #15），不是级联删除
  - 对设计的落地：改 client_id 对副本存亡无效（只动鉴权字段）；move 到活 seg 有效；kvcm 自挂 SSD 是不依赖任何引擎存活的兜底；main 的 retained client / DFS 副本是正式路径
  - 剩余事实输入：lease TTL 11s（master 实配 `default_kv_lease_ttl=11000` 毫秒）+ 驱逐候选资格（lease 过期→驱逐首选）仍归 D3/TTL 议题
- **E6. 0.3.11 API 子集核对**——✅ 已闭环（结论写在 D2 F13）
  - 做法：三个版本的 RPC 注册表 diff（v0.3.11 与 v0.3.11.post1 完全相同）
  - 0.3.11 已有：lease / offload / promotion 心跳取任务、Admin 查询三件套、`EvictDiskReplica`、`BatchExistKey` 续租（D1 F15）、soft pin（D2 F9）
  - main 才有：`ProbeKey`/`BatchProbeKey`、零副作用 Admin 查询（`GetReplicaListForAdmin`）、租户配额五件套、KV 事件、容量快照、`UnmountLocalDiskSegment`
  - `default_kv_lease_ttl` 测试床配置 11s（D2 F9）
  - 缺口清单 = D2 F13⑥（per-key 降层 RPC、copy/move 取消、key→client_id 查询）——kvcm 侧组合达不到完整语义，要补得动 mooncake 池侧（copy 取消有 kvcm 弱版「弃置 + 事后清副本」，P9）；改不改、何时补未议
- **E7. MLA 实测**——`put_step>1` 时传输计划全部分配到 `targets[0]`、其余 rank 读远端：功能上推断正确但还没实测；hybrid/sparse 被上游的开关检查排除（异构不支持，同构可用）。
- **E8. mooncake kv_event schema 核对**——✅ 已闭环（结论写在 D1 F14）

publisher 含 stored/removed/cleared，**驱逐发 removed**；载荷 = key+medium+tenant+group，**无 seg/机器位置**；ZMQ 传输；CMake 默认 OFF、官方 wheel 构建 ON。**0.3.11.post1 不含此机制**（publisher 代码在 v0.3.11 之后进仓，tag 上无 `kv_event/` 目录、无构建开关）——D1 路径 b 要用须升级 mooncake 版本。

- **E9. vLLM 事件载荷核对**——✅ 已闭环（结论写在 D1 事实基线 F7-F10）
  - `BlockStored` 含 `medium` 字段
  - 两个发布源：引擎发的 GPU 块事件（含 Removed，上游既有）、连接器发的池 put 事件（无 Removed）
  - 预复制不经过引擎，结构上就没有事件——只能靠控制面补发

### B. 服务化重构（骨架可直接开工，接口依赖设计）

- **E10. 面向对象重构**——现 precopy 包是面向过程脚本（keys/resolve/common/precopy 四模块函数式），按五层架构重组（L3 原子操作抽象屏蔽后端语义 + L4 mooncake 适配器 + L2 编排层只调 L3），明确状态归属与接口边界，为进程化打基础。🟡 **进行中**——三步（Ledger / L3+L4 / L2+API）已落地，PR 审核中。设计：[code-architecture.md](design/code-architecture.md)
  - **已知边界（rebuild 任务归属）**：重启恢复（`MooncakeBackend.rebuild`）不区分任务归属——账本里全部 running 任务都按本实例的模型口径重算；多模型/多实例共用同一 Ledger 时会错算。当前单实例单模型无影响；**多模型前必须先加归属判定（model_id 不符则跳过）**。设计文档补全时写入
  - **后续已定（接口 KeySet 化）**：L3 加 `keys_of(prefix) -> KeySet`（不透明类型），操作方法入参从 Prefix 改为 KeySet——长序列下消除「一次预热算两遍 key」。方案与分层论证见 [code-architecture.md](design/code-architecture.md)「接口演进：KeySet」；本 PR 不动代码，合入后实施
- **E11. 进程化**——一次性脚本 → 常驻服务：接收预热 intent、并发处理多任务、任务状态机（排队/进行/READY/失败）、优雅退出（现 `store.close()` 只修了单次退出崩溃，进程化后生命周期要重构）。骨架可直接开工；intent 接口先用占位，随 E12 定型。分层与模块划分：[code-architecture.md](design/code-architecture.md)
- **E12. 与 router 通信**——intent（预热请求：前缀/目标实例）与 READY/失败回执的通道与协议（gRPC 候选，与仓内跨语言 RPC 选型一致）。协议字段依赖 D4 定型；通道骨架可先行。
- **E13. 与服务控制面通信**——推理实例部署/销毁事件接入：新实例启动 → 触发热点前缀预热；实例销毁 → 其 seg 副本清理/归属处理（与 D3 耦合；D5 已闭环，见本文）。事件源选型（K8s watch / 部署系统通知）依赖 D4；接入骨架可先行。
- **E14. KV event 通信**——随 D1 §2 方案 a/b（已闭环），控制面自己产生引擎格式的事件
  - **发布端（pub）**：
    - 直接 import 上游 `ZmqEventPublisher`（`kv_events.py:304`）
    - 事件字段取自 keys.py 的中间产物（hash 链 / token ids）
    - READY 核验后按目标实例各发一条 `BlockStored`
    - 每个目标实例一个发布端点；「端点 → 实例」注册归 D4/E13
  - **删除信号（升级 mooncake 后）**：
    - 首选：索引侧直接订阅 master publisher、解析 object_key（Conductor 现成，D1 §2 方案 b）
    - 备选：kvcm 加订阅端 + 翻译——订阅 master publisher（F14），从 PoolKey 解析出 block hash、按 rank 去重，重发 `BlockRemoved` / `AllBlocksCleared`；解析对照 mooncake-conductor `object_key_parser.cpp:160-276`（`ParseVllmObjectKey`，已覆盖 vLLM-Ascend key 布局）
    - 引擎事件的订阅端归索引侧
  - **端点生命周期**：
    - 何时为实例开补发端点（实例上线随 E13 事件 / 首次预热该实例时）、实例下线时回收
    - 端口分配与上报编排（与 E12/E13 同通道）
    - 补发事件 `data_parallel_rank` 字段的取值与消费侧归组行为（目标实例 DP>1 时）；上游 DP 端口偏移先例 `offset_endpoint_port`（`kv_events.py:528`）
  - 发布端骨架可直接开工

### C. 性能与生产化

- **E15. copy 并行与限速**——🟡 **网卡利用率限速仍然不做**。并行窗口已落地（E11：`asyncio.Semaphore`，默认 4，`--copy-submit-concurrency`）。10 月 29 日跨机（[demo/EXPERIMENTS.md](demo/EXPERIMENTS.md) #17，与 #16 单机 `TRANSFER_FAIL` 不是同一次实验）：42 条 `create_copy_task` 在窗口 4 下约一半失败，主错误是 `NO_AVAILABLE_HANDLE`（`max retries reached: 10`），master 留内存失败；14 条全成。提交前只查计划第一条的目标段，只加落在这一段上的新字节；其它段的剩余和字节都不参与，这一段不够就一个 key 都不发。没查的段仍可能在 `CopyStart` 时 `NO_AVAILABLE_HANDLE`。这已经在本分支落地；它不代替 E15，也不做网卡采样。实验当时也没跑这条预检。另一条前缀已经提交、但还没在目标段划出缓冲区时，剩余里看不见，两条都可能放行。彻底避开代价高，接受这个窗口。Router 侧用网卡信号打分归 D1 §5。
- **E16. 生产化缺口**——EP 拓扑实测、master HA、监控指标（READY 延迟、copy 失败率、external hit；命中率归因口径与 P8 耦合）。

## 参考锚点

- Dynamo KV-aware router 外部重算 hash 链：[../docs/research/dynamo/overview.md](../docs/research/dynamo/overview.md)
- llm-d EPP 推测索引（事件确认前先用推测条目，TTL 2s 自动过期）：[../docs/research/llm-d/](../docs/research/llm-d/)
- AIBrix ZMQ 事件同步：[../docs/research/aibrix/](../docs/research/aibrix/)
- mooncake master API（submodule 源码，符号锚点）：`master_service.h`（`CreateCopyTask` / `SubmitReplicaActionProposal` / `Remove` / `BatchEvict` / `CreateDrainJob`）、`master_config.h`（`enable_kv_events` 开关，E8）、`master_service.cpp`（`CopyStart:6444` 幂等三层防护；`CreateCopyTask:13677` 提交层无去重；reaper `:10653-10700`）
- mooncake Conductor（池事件的自家消费者）：`mooncake-conductor/src/kvevent/object_key_parser.cpp`（`ParseVllmObjectKey:160` / `ParseSglangObjectKey:99`）、`src/zmq/msg_decoder.cpp`（`ParseVllmEvent:365` / `DecodeVllmEventBatch:886`——vLLM 引擎格式解码，kvcm 补发「一套接口」可被消费的实现证据）、`docs/source/design/conductor/conductor-architecture-design.md`
- 事件/索引标准化与消费先例：RFC #1527（引擎 → KV 感知 router 事件线格式）、RFC #1403（Indexer API：`/register` 注册事件源端点、`/query` 与 `/query_by_hash` 两种查询入参）；Dynamo 池事件消费先例 `lib/llm/src/kv_router/shared_cache.rs::HicacheSharedKvCache`；Dynamo RFC #1403 实现 `lib/kv-router/src/services/indexer/`（`standalone-indexer.md`）
- 关键差异：现有系统的事件都是单源（引擎侧或池侧），我们是多源（引擎原生事件 + kvcm 补发 + 池 publisher 删除事件），权威归属与合并规则要自己定（D1 §2/§3）。
- vLLM-Ascend AI-QoS（流量分类 + UB 交换机 Virtual Lane 优先级调度；KVCache 传输与集合通信/算子下发冲突的生产级证据；执行层 QoS，与我们调度层避让互补）：vllm-project/vllm-ascend `tools/ai_qos/` + `docs/source/user_guide/feature_guide/Ai_QoS_introduction_en.md`（消费点：D1 §5 选路打分、E15 预热带宽控制）
