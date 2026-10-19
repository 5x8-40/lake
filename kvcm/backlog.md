# KVCM backlog：遗留问题与下一步计划

来源：[issue #54](https://github.com/5x8-40/lake/issues/54)（KV-CM 遗留问题总清单）；本文是活文档，条目状态随讨论与实验更新。

编号：**D** = 设计决策（语义/选型未定，先决策）；**P** = 潜在问题（待评估）；**E** = 工程项。编号创建后不复用；条目闭环即从本文删除，结论落对应正式文档（D 系文档 / `kv-prefetch.md` / README）。

依赖主线：D1/D2 是设计主线（E5/E6/E8/E9 是其事实输入）；E10→E11→E12/E13/E14 是服务化主线（重构先行、协议随后）；P1-P8 大多随 D1/D2 定型一并回答。

状态标记：✅ 已闭环（附落点）｜🟡 部分闭环｜无标记 = 待定。

## 一、设计决策

**D1. Router 集成与请求调度** — ✅ 已闭环

最终形态：router 决策请求发哪个实例 → precopy 预热该实例 → 并决定哪些 KV 释放。工作文档：[D1-router-scheduling.md](D1-router-scheduling.md)（事实基线 F1-F15 已核实，含复制幂等性与 lease 语义；六问全闭环，「待定问题」已转「设计决策」）。

六个问题（全部闭环）：

1. **选路输入**——✅ 已闭环（落 D1 §1）：选路面逻辑分两层——索引（订阅事件流维护「block hash → 实例」）与 Router（自算请求的 hash 链查索引，RFC #1403 `/query` 支持 token_ids 或 hash key 两种入参，不依赖原始 token 文本）；索引来源两个——引擎原生事件 + kvcm 补发（预复制放置），kvcm 负责补齐引擎看不到的部分；Router 不碰池 key（池位置是控制面平面的事）。索引进程形态（内置 / 外挂 / 内置但暴露查询服务——全量索引一份；辅助预测树独立决策）与事件开关、传输接线归 D4。
2. **可见性**——✅ 已闭环（落 D1 §2）。拆两问：**放置** = 方案 a 控制面补发 `BlockStored`（池事件不带机器位置、复制路径零发布，任何版本只能靠它，F14；补发语义 = READY 核验后「全 rank 齐备可路由」，master 单 key 粒度给不了）；**删除** = 升级 mooncake 开 master publisher，首选索引侧直订、解析 PoolKey 出 block hash（Conductor `ParseVllmObjectKey` 现成、Dynamo `shared_cache.rs` 自持 hash↔key 映射同构，F14；kvcm 订阅翻译重发降级为备选；0.3.11 不含，E8 已核）。轮询对账（只为删除得不偿失）与索引条目 TTL（与放置无关的独立兜底）不做；直读 master 排除。方案 a 复用要点：事件字段全是 keys.py 中间产物，发布直接 import 上游 `ZmqEventPublisher`；身份 = 端点（每目标实例一个补发端点，「端点 → 实例」注册归 D4/E13，外挂形态走 RFC #1403 `/register`）。
3. **「哪个实例拥有前缀」怎么定义**——✅ 已闭环（落 D1 §3）：实例 X 拥有前缀 P = P 的连续满块链 KV 在 X 本机——HBM（gpu 事件）与本机池 seg（cpu 事件 / kvcm 补发）都算；命中长度 = 连续满块数 × block_size；HBM 与 DRAM 差异留 §5 打分；rank 维度由事件语义消除（全 rank 才发），router 无需 per-rank 索引。
4. **时序**——✅ 已闭环（落 D1 §4）：请求不等 READY（照常走 F3 兜底链，复制后台追）；复制三时机并存（顺手补 / 提前备——最终形态前端 agent/coder 钩子在请求发出前先把信号给控制面，归 D4 / 挑现成的 = router 前缀亲和本职）；intent 以（前缀 × 目标实例）为幂等键去重。边界：kvcm 不决策选路，router 不直接下复制命令，经 intent 表达意图（router → kvcm 下行），事件上行（kvcm → 索引）。
5. **选路目标函数**——✅ 已闭环（落 D1 §5）：归 router 内部实现，kvcm 无决策（§1-§3 后 router 输入已齐）；候选在案——分级兜底 / 打分制（Dynamo 分层计价借初值）/ 传输成本两档；权重待真机校准（P7）。
6. **release 归谁**——✅ 已闭环（落 D1 §6）：router 提供会话生命周期线索，控制面决策执行，池驱逐兜底；手段三个——主路径停保活软释放（有读自动续租，动不了在用的数据）、中间档 demote 降层（DRAM→SSD 不删，归 D2）、例外主动 Remove（key 全局，A 的原本一起删）仅限「确认没人用」。

已知现状：引擎事件通道上看不到池副本删除（F10）→ 索引中池副本条目只增不减，条目过时只损性能（F3）；GPU 侧有 Removed（F8）；池原生 publisher 有 removed 但 0.3.11 不含、载荷只说 key 怎么了不说在哪台机器（F14，E8 已核）——升级后首选索引侧直订解析（D1 §2 方案 b），kvcm 翻译重发为备选。选路方进程形态与 D4 耦合；工程实现 E11-E14 依赖设计定型，E10/E11 骨架可先行。

**D2. KV 调度操作集（pin / demote / prefetch / retain / release）**

独立控制面进程对 KV 的完整操作面，每项定语义、与池驱逐策略的优先级、失败处理：

- 操作集框架：✅ 已闭环（落 [D2-kv-operations.md](D2-kv-operations.md) §1）——八项操作 copy/move/keepalive/pin/demote/promote/retain/remove（原子动作 / 元数据标记 / 目的型三类），复合流程原子组合（prefetch = copy+核验+保活注册；会话结束降层 = 停 keepalive+demote；下线迁移 = copy+remove 或 move 一步）；状态记录四类（任务状态机 / 保活注册表 / 副本台账 / 会话绑定）；kvcm 单实例跟随 master，状态存 sqlite。
- prefetch：✅ 已验证（`CreateCopyTask` → READY）。
- pin：READY 后防驱逐——🟡 候选已摆平，落 [D2-kv-operations.md](D2-kv-operations.md) §2
  - soft pin 档（驱逐排最后，极端可逐）：空写 upsert 一方案——不动全局 lease、断线窗口 30min 级；代价 = 空写通道（Python 绑定无分离接口）+ 属主问题
  - hard pin 档（绝对不可逐）两候选：① lease 调大 + keepalive（零改动零属主问题；代价 = 全局读租约变长 + kvcm 须活着续命）；② 读删写（kvcm 失联也安全；数据过本地 + 属主问题）
  - 取消：①停 touch 等过期 / 空写传 false 或 `SoftPinAction::DISABLE` / hard pin 无 unpin、只能再读删写一遍
  - 读锁已证伪（被否决）：refcnt 是任务级保护，两版本无读者引用 RPC
  - 属主两方案并列（空写与读删写共同面对）
    - 改归 kvcm：不改 mooncake、语义变（引擎下线 key 不删；kvcm 成新单点）
    - 借原属主 UUID：语义不变、需 master 加「key → 属主 UUID」查询 RPC（内部数据现成）
    - 决策条件 = mooncake 可改性（上游 PR / fork，独立成本）+ kvcm 实现语言（E10/E11）
  - 机制事实：见 D2 文档 F1-F12（pin 形态、upsert 行为、属主管辖、lease 与驱逐、UUID 与空写通道）
- retain：🟡 机制定型（落 [D2-kv-operations.md](D2-kv-operations.md) §4）——正路 = main 池机制两条并列：retained client（断线数据保留等认领，`AddReplicaForRetainedClient`，解引擎进程下线）、DFS 副本（写共享 3FS，不随 client 死被清，解整机灭失，前提部署 3FS）；0.3.11 下 demote 解不了 retain（盘副本绑引擎 client，引擎死照样清），过渡 = 改属主/move（待 E5 口径裁决）/ kvcm 自挂 SSD（不依赖 E5 的兜底）；计划下线编排有 `GracefulUnmountSegment`（0.3.11）/ `CreateDrainJob`（两版本同有，job 级可取消）；长期未命中 → demote SSD，策略归编排。
- demote / promote：🟡 设计完成（落 [D2-kv-operations.md](D2-kv-operations.md) §3）——语义原子、实现为组合动作（池通道 / kvcm 主动触发 / 读删写 + 核验），存储统一归 mooncake（引擎实例挂本机 SSD 多级 tier，kvcm 不持存储）。demote = 池开 `enable_offload`（写穿/写回两模式 ≈ HiCache write_through/write_back），写回模式须先停保活等 lease 过期（保活挡驱逐即挡落盘）；promote = `promotion_on_hit` 被动提回 + kvcm 主动两条（`GetReplicaList` 连调刷满命中计数触发 / 读删写自搬）。copy/move 接不了 SSD（disk seg 无 seg 名，两版本同）。触发（会话结束/变冷/DRAM 压力/计划下线）归策略层。真功能缺口：写回模式确定性 demote（无 per-key 降层 RPC + `BatchReplicaClear` 限属主）→ 池侧扩展；远期 = kvcm 自己挂 SSD + `AddReplica` 登记。每操作有完成信号，READY 特指 DRAM 就位可调度。
- release：✅ 已闭环（落 [D2-kv-operations.md](D2-kv-operations.md) §4）——决策归属 D1 §6；kvcm 可用 `Remove`/`BatchRemove`/`RemoveByRegex`（配 key 命名空间才有意义）/`RemoveAll`；`BatchReplicaClear` 限 key 属主自调（kvcm 调不动别人的 key）；`force` 仅限「确认没人用」；收尾 = 清台账 + 索引同步。
- 事实输入：E6。

**D3. 副本生命周期与 READY 语义**

- 事实：副本无 pin，READY 后池可驱逐；属主实例下线后 key 连同远端副本被 master 元数据级移除（字节成无索引残留；exist 0/32 实测见 [EXPERIMENTS.md](EXPERIMENTS.md)）——D1 F5。lease 为 key 粒度：读续租、`CopyStart` 不续租、TTL/驱逐删 key 释放全部副本——D1 F15。
- 待定：READY 是否 =「本机 DRAM 必达」；保活策略；属主下线后副本归属（保留/驱逐/迁移）。
- 保活机制事实（D1 F15）：`batch_is_exist` 顺带续租（`ProbeKey` 不续），默认 TTL 10s——预复制不续租则一个 TTL 窗口内无流量即可驱逐；保活循环必须有界（会话期 / 固定窗口 / 首流量后停租），无限续租事实上等于 pin。策略（续多久、何时停）本节定型。
- 下线迁移事实：main 有 `CreateDrainJob`（`master_service.h:872`，把指定 seg 优雅排空到目标 seg，带并发上限）——实例计划下线前的迁移路径，归本节评估。
- 落盘子项：Mooncake DRAM→SSD（`enable_ssd_offload`，本目录默认 `false`）是池内冷热，不是「预复制直指定 disk」；SSD 仅作容量/恢复异步 demote。
- 直接影响故障恢复时已预热前缀的可用性（D5）。事实输入：E5。

**D4. 编排集成**

- 待定：READY 信号如何接 gateway/router：谁发起预取、何时放行、失败重试。过载/准入仍归 gateway，推理侧只管执行。
- 待定：Prefetch Orchestrator 形态（旁路进程或并入 Router）：收 intent → 调 B2 → READY 确认 → 放行。
- 边界（随 D1 §2 方案 a，已闭环）：索引消费引擎 kv event，预复制放置由控制面补发事件注入（D1 §2）；控制面侧 seg→实例拓扑由编排层提供（测试床 resolve.py 探测链是取证手段，不是生产路径）。
- 已裁决：~~kvcm 兼做索引~~（否决）——kvcm 额外维护一整套索引（前缀树、查询服务、HA）等于重造选路面轮子（Conductor / Dynamo standalone indexer 现成），且把 kvcm 从后台控制面拖成在线服务；kvcm 保持纯池控制面（复制 / 保活 / 释放 / 补发事件），「告知放置」只走补发事件（D1 §4 职责边界）。
- 待定：消费侧索引形态（**Router 团队选型，kvcm 接口不变**——D1 §2 方案 a 形态无关性：一套端点、一套引擎格式，零适配零分支）——方案 A：Router 内置索引（llm-d EPP / Dynamo kv-router 形态：索引与 scorer 紧耦合、请求路径少一跳；代价 = 订阅/索引/gap 处理自写；变体：Dynamo remote indexer，内置但暴露查询服务给多 consumer）/ 方案 B：外挂 Conductor（现成：object_key parser、RFC #1403、vLLM 格式解码 `msg_decoder.cpp::ParseVllmEvent:365` 已核实；多事件源聚合、独立 HA；代价 = 请求路径多一跳 HTTP，同步选路路径毫秒级延迟预算）。**辅助预测树**独立决策：兄弟请求批内竞态（同前缀请求并发到达、引擎尚未发 stored）——Dynamo predict-on-route side indexer（短 TTL 侧树取 max）与 llm-d 推测索引（TTL 2s）先例；侧树分立根因是 hash 口径不同会污染主树，kvcm 补发与引擎同口径（keys.py）无此约束（D1 §2 方案 a）。
- 登记：mooncake 自家 Conductor（`mooncake-conductor/`，C++）是 RFC #1403 索引契约的现成实现，与 kvcm **职责互补不重叠**——Conductor = 外挂索引（订阅引擎/池事件建全局前缀表，HTTP `/query` 支持 token_ids 或 hash key 两种入参；含 object_key→hash parser），不复制、不保活、不释放；kvcm = 池控制面（复制 / 保活 / 补发事件 / 释放），Conductor 都没有。生态：Dynamo kv-router `shared_cache.rs::HicacheSharedKvCache` 直订 master publisher 消费；华为 MindIE Motor 官方「KV Cache 亲和性调度」= Conductor + Coordinator 查询；社区试用 Discussion #1967（D1 F14 消费者现状节）。重叠仅一点：object_key→hash 解析（代码级对照 `object_key_parser.cpp`）。复用路径两条：组件级——外挂索引直接用 Conductor（方案 b 删除面它自己直订解析，kvcm 翻译层消失；方案 a 补发仍需要，经 RFC #1403 `/register` 注册端点即被它消费）；代码级——自研索引则对照 parser。待评估：成熟度（0.3.11 不含、随升级获得）、在线路径 HTTP 查询延迟 vs 进程内索引、HA、与 §5 打分逻辑的结合。
- 是 D1 时序问题的执行面。骨架先行：E12/E13。

**D5. 故障衔接**

- 事实：copy 期间源实例掉线即失败（master 给源属主派任务，D1 F4）。
- 事实：源属主挂死后，在途任务标记由 master reaper 清理（`master_service.cpp:10654` 附近）；清理窗口内重发同一 copy 吃 `OBJECT_HAS_REPLICATION_TASK`（幂等语义见 D1 F11-F12）；staged 副本是否随 reaper 一并清除未核实。
- 待定：与故障重路由如何衔接——重新预热还是容忍本次远端读。

**D6. layerwise 选型**

- 候选：Mooncake block_key（逐层 range 读，HBM 仍全层；`backend=mooncake` + `use_layerwise=true`）vs MemCache gva（HBM 可减量，预热落点更偏本机 DRAM；`backend=memcache`）。
- 待定：生产默认走 MemCache，还是留 Mooncake 并补测 layerwise + B2（get session 是否仍选本机 DRAM）。不建议自研 Mooncake GVA。
- 三个「layerwise」机制完整辨析：[../docs/research/vllm-ascend/layerwise-taxonomy.md](../docs/research/vllm-ascend/layerwise-taxonomy.md)。

**D7. 方案 A 去留**

- 待定：壳请求进 HBM 是否保留、与 B2 的叠加顺序。方案 A/B2 定义见 [kv-prefetch.md](kv-prefetch.md)。

## 二、潜在问题

- **P1. KV 与模型版本绑定**——vLLM block hash 不含权重 revision，权重更新后旧 KV 会误命中且输出静默错误。key 命名空间必须含 model_id/revision。随 D1 key 表定型一并回答。
- **P2. 并发预热去重**——🟡 池层幂等已核实（D1 F11-F13：重发不产生重复副本，在途报 `OBJECT_HAS_REPLICATION_TASK`）；仍待做：控制面（前缀 × 目标实例）幂等键合并，避免多请求/多 router 实例并发触发时重复占带宽。
- **P3. 快照语义**——copy 期间源前缀还在增长（在途请求继续产生 block），precopy 是快照，快照点之后的新 block 谁补。
- **P4. 读与驱逐竞态**——🟡 部分已由 D1 F15 回答：读路径（`BatchGetReplicaList:4566` / `GetReplicaList:4401`）即授 lease（默认 10s），查询到读完的窗口受 lease 保护（驱逐候选必须 lease 过期）；副本级 refcount 与长读请求的语义仍待确认（与 D2 retain 耦合）。
- **P5. 多租户与配额**——多模型共池的隔离（mooncake main 有 tenant quota，0.3.11 是否具备待核对 → E6）。
- **P6. 鉴权边界**——现状能连 master 就能 `create_copy_task`/`Remove`。生产需要接入控制；控制面权限应大于引擎（引擎只需读写自己 seg）。
- **P7. 预热成本核算**——预热后请求没来或去了别实例的浪费；READY 延迟（带宽×块数）vs 请求到达时间的窗口预测。与 D1 §4/§5 耦合（已闭环，口径见 D1）。
- **P8. 指标归因**——命中率要拆「预热贡献 vs 引擎内部 HBM cache」。测试床已遇到第二次命中被内部 cache 接住（外部 get=0），生产监控同样会混淆。
- **P9. prefetch 终止**——请求改道/源消失时在途 copy 白耗带宽。对照 HiCache `can_terminate_prefetch` 三策略；mooncake 0.3.11 只有 `CancelDrainJob`，copy/move 无取消 RPC——补法：池侧扩展（改 mooncake）或 kvcm 弱版（弃置 + 事后清副本）。来源：D2 操作集完备性审计（[D2-kv-operations.md](D2-kv-operations.md) §1）。

## 三、工程项

### A. 验证类（可直接开工，除注明外）

- **E1. 真机冒烟**——`run_e2e.sh` 一键（同构 TP=2 + 异构双向）+ 同构跨机 rank↔seg 新路径重跑。实质路径已手工验证，单测 19 例 + 干跑覆盖，风险限编排层。
- **E2. key 哨兵容器单跑**——`test_keys.py` 在容器内跑通（运行期 `batch_is_exist` 32/32、36/36 证据已齐）。
- **E3. 多卡自动匹配验证**——resolve 的 `VLLM::Worker_TP<N>` proctitle 匹配在多卡真机走通（现为 `--targets` + 手工对号；失效时明确报错而非静默错配）。
- **E4. 1-token 翻转根因**——旧非 DEBUG 实例偶发，新实例未复现；首次外部读输出逐字一致，疑路径交替数值扰动。生产化前必须查清（[EXPERIMENTS.md](EXPERIMENTS.md) #10）。
- **E5. 副本消失机制核实**——属主下线后 key+副本移除的时机与条件（TTL、GC 扫描窗口）对照实验 → D3 事实输入。**D2 代码核实新增裁决点**（[D2-kv-operations.md](D2-kv-operations.md) F6b）：代码上清理是副本级（活 seg 上他 client 副本不该被清、无按 key 属主删路径），与实测 0/32（B seg 副本也消失）矛盾——实验需裁决两口径：「按 key 属主删」vs「按 seg 清理」（前者则改属主有效、后者则 move 有效；0.3.11 下不依赖口径的兜底 = kvcm 自挂 SSD，main 池机制 retained client / DFS 副本是 E5 之外的独立解）；排查方向：master 是否同机陪葬 / 副本实际状态 / 其他清理路径。
- **E6. 0.3.11 API 子集核对**——D2 引用的 lease/offload/promotion 是 mooncake main 的符号，测试床 wheel 是 0.3.11.post1，逐个核对可用子集，缺的即池侧扩展工作量 → D2/P5 输入。新增核对：`BatchExistKey` 的 `grant_lease` 续租语义与 `BatchProbeKey`（D1 F15）、soft pin（D2 pin 候选）、`default_kv_lease_ttl` 实际配置值。
- **E7. MLA 实测**——`put_step>1` 时 plan 全落 `targets[0]`、其余 rank 读远端，功能推断正确未实测；hybrid/sparse 被上游 gate 排除（异构不支持，同构可用）。
- **E8. mooncake kv_event schema 核对**——✅ 已闭环（落 D1 F14）：publisher 含 stored/removed/cleared，**驱逐发 removed**；载荷 = key+medium+tenant+group，**无 seg/机器位置**；ZMQ 传输；CMake 默认 OFF、官方 wheel 构建 ON。**0.3.11.post1 不含此机制**（publisher 代码在 v0.3.11 之后进仓，tag 上无 `kv_event/` 目录、无构建开关）——D1 路径 b 要用须升级 mooncake 版本。
- **E9. vLLM 事件载荷核对**——✅ 已闭环（结论落 D1 事实基线 F7-F10）：`BlockStored` 含 `medium` 字段；两个发布源——引擎核发 GPU 块事件（含 Removed，上游既有）、连接器发池 put 事件（无 Removed）；预复制绕引擎结构性无事件。

### B. 服务化重构（骨架可直接开工，接口依赖设计）

- **E10. 面向对象重构**——现 precopy 包是面向过程脚本（keys/resolve/common/precopy 四模块函数式），重构成类结构（KeyComputer / SegResolver / StoreClient / Orchestrator），明确状态归属与接口边界，为进程化打底。**可直接开工**。
- **E11. 进程化**——一次性脚本 → 常驻服务：接收预热 intent、并发处理多任务、任务状态机（排队/进行/READY/失败）、优雅退出（现 `store.close()` 只修了单次退出崩溃，进程化后生命周期要重构）。骨架可直接开工；intent 接口先用占位，随 E12 定型。
- **E12. 与 router 通信**——intent（预热请求：前缀/目标实例）与 READY/失败回执的通道与协议（gRPC 候选，与仓内跨语言 RPC 选型一致）。协议字段依赖 D4 定型；通道骨架可先行。
- **E13. 与服务控制面通信**——推理实例部署/销毁事件接入：新实例拉起 → 触发热点前缀预热；实例销毁 → 其 seg 副本清理/归属处理（与 D3/D5 耦合）。事件源选型（K8s watch / 部署系统通知）依赖 D4；接入骨架可先行。
- **E14. KV event 通信**——随 D1 §2 方案 a/b（已闭环），控制面自产引擎格式事件。**pub 端**：直接 import 上游 `ZmqEventPublisher`（`kv_events.py:305`），事件字段取自 keys.py 中间产物（hash 链 / token ids），READY 核验后按目标实例各发一条 `BlockStored`；每目标实例一个发布端点，「端点 → 实例」注册归 D4/E13。**升级 mooncake 后删除面**：首选索引侧直订 master publisher、解析 object_key（Conductor 现成，D1 §2 方案 b）；备选才由 kvcm 加 sub 端 + 翻译——订阅 master publisher（F14），PoolKey 解析 block hash、rank 去重，重发 `BlockRemoved` / `AllBlocksCleared`，解析对照 mooncake-conductor `object_key_parser.cpp:160-276`（`ParseVllmObjectKey`，已覆盖 vLLM-Ascend key 布局）；引擎事件 sub 端归索引侧。**端点生命周期**：何时为实例开补发端点（实例上线随 E13 事件 / 首次预热该实例时）、实例下线回收、端口分配与上报编排（与 E12/E13 同通道）；补发事件 `data_parallel_rank` 字段取值与消费侧归组行为（目标实例 DP>1 时；上游 DP 端口偏移先例 `offset_endpoint_port`，`kv_events.py:528`）。pub 骨架可直接开工。

### C. 性能与生产化

- **E15. copy 并行与限速**——现逐 key 串行、与在线读共享带宽；大批量预热需并行化 + 带宽控制。可直接开工。
- **E16. 生产化缺口**——EP 拓扑实测、master HA、监控指标（READY 延迟、copy 失败率、external hit；命中率归因口径与 P8 耦合）。

## 参考锚点

- Dynamo KV-aware router 外部重算 hash 链：[../docs/research/dynamo/overview.md](../docs/research/dynamo/overview.md)
- llm-d EPP 推测索引（事件确认前先用推测条目，TTL 2s 自动过期）：[../docs/research/llm-d/](../docs/research/llm-d/)
- AIBrix ZMQ 事件同步：[../docs/research/aibrix/](../docs/research/aibrix/)
- mooncake master API（submodule 源码，符号锚点）：`master_service.h`（`CreateCopyTask` / `SubmitReplicaActionProposal` / `Remove` / `BatchEvict` / `CreateDrainJob`）、`master_config.h`（`enable_kv_events` 开关，E8）、`master_service.cpp`（`CopyStart:6444` 幂等三层防护；`CreateCopyTask:13677` 提交层无去重；reaper `10654` 附近）
- mooncake Conductor（池事件的自家消费者）：`mooncake-conductor/src/kvevent/object_key_parser.cpp`（`ParseVllmObjectKey:160` / `ParseSglangObjectKey:99`）、`src/zmq/msg_decoder.cpp`（`ParseVllmEvent:365` / `DecodeVllmEventBatch:886`——vLLM 引擎格式解码，kvcm 补发「一套接口」可被消费的实现证据）、`docs/source/design/conductor/conductor-architecture-design.md`
- 事件/索引标准化与消费先例：RFC #1527（引擎 → KV 感知 router 事件线格式）、RFC #1403（Indexer API：`/register` 注册事件源端点、`/query` 与 `/query_by_hash` 两种查询入参）；Dynamo 池事件消费先例 `lib/llm/src/kv_router/shared_cache.rs::HicacheSharedKvCache`；Dynamo RFC #1403 实现 `lib/kv-router/src/services/indexer/`（`standalone-indexer.md`）
- 关键差异：现有系统的事件都是单源（引擎侧或池侧），我们是多源（引擎原生事件 + kvcm 补发 + 池 publisher 删除事件），权威归属与合并规则要自己定（D1 §2/§3）。
