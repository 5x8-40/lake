# KVCM backlog：遗留问题与下一步计划

来源：[issue #54](https://github.com/5x8-40/lake/issues/54)（KV-CM 遗留问题总清单）；本文是活文档，条目状态随讨论与实验更新。

编号：**D** = 设计决策（语义/选型未定，先决策）；**P** = 潜在问题（待评估）；**E** = 工程项。编号创建后不复用；条目闭环即从本文删除，结论落对应正式文档（D 系文档 / `kv-prefetch.md` / README）。

依赖主线：D1/D2 是设计主线（E5/E6/E8/E9 是其事实输入）；E10→E11→E12/E13/E14 是服务化主线（重构先行、协议随后）；P1-P8 大多随 D1/D2 定型一并回答。

状态标记：✅ 已闭环（附落点）｜🟡 部分闭环｜无标记 = 待定。

## 一、设计决策

**D1. Router 集成与请求调度** — ✅ 已闭环

最终形态：router 决策请求发哪个实例 → precopy 预热该实例 → 并决定哪些 KV 释放。工作文档：[D1-router-scheduling.md](D1-router-scheduling.md)（事实基线 F1-F15 已核实，含复制幂等性与 lease 语义；六问全闭环，「待定问题」已转「设计决策」）。

六个问题（全部闭环）：

1. **选路输入**——✅ 已闭环（落 D1 §1）：router 维护「block hash → 实例」索引，只消费引擎格式事件；来源两个——引擎原生事件 + kvcm 补发/翻译（预复制放置、池删除），kvcm 负责补齐引擎看不到的部分；不碰池 key（池位置是控制面平面的事）。事件开关与传输接线归 D4。
2. **可见性**——✅ 已闭环（落 D1 §2）。拆两问：**放置** = 方案 a 控制面补发 `BlockStored`（池事件不带机器位置、复制路径零发布，任何版本只能靠它，F14）；**删除** = 升级 mooncake 开 master publisher，由 kvcm 订阅并翻译成引擎格式重发（PoolKey 解析 block hash + rank 去重，router 不碰池格式；0.3.11 不含，E8 已核）。轮询对账（只为删除得不偿失）与 router 条目 TTL（与放置无关的独立兜底）不做；直读 master 排除。方案 a 复用要点：事件字段全是 keys.py 中间产物，发布直接 import 上游 `ZmqEventPublisher`；身份 = 端点（每目标实例一个补发端点，「端点 → 实例」注册归 D4/E13）。
3. **「哪个实例拥有前缀」怎么定义**——✅ 已闭环（落 D1 §3）：实例 X 拥有前缀 P = P 的连续满块链 KV 在 X 本机——HBM（gpu 事件）与本机池 seg（cpu 事件 / kvcm 补发）都算；命中长度 = 连续满块数 × block_size；HBM 与 DRAM 差异留 §5 打分；rank 维度由事件语义消除（全 rank 才发），router 无需 per-rank 索引。
4. **时序**——✅ 已闭环（落 D1 §4）：请求不等 READY（照常走 F3 兜底链，复制后台追）；复制三时机并存（顺手补 / 提前备——最终形态前端 agent/coder 钩子在请求发出前先把信号给控制面，归 D4 / 挑现成的 = router 前缀亲和本职）；intent 以（前缀 × 目标实例）为幂等键去重。边界：kvcm 不决策选路，router 不直接下复制命令，经 intent 表达意图（router → kvcm 上行），事件下行（kvcm → router）。
5. **选路目标函数**——✅ 已闭环（落 D1 §5）：归 router 内部实现，kvcm 无决策（§1-§3 后 router 输入已齐）；候选在案——分级兜底 / 打分制（Dynamo 分层计价借初值）/ 传输成本两档；权重待真机校准（P7）。
6. **release 归谁**——✅ 已闭环（落 D1 §6）：router 提供会话生命周期线索，控制面决策执行，池驱逐兜底；手段三个——主路径停保活软释放（有读自动续租，动不了在用的数据）、中间档 demote 降层（DRAM→SSD 不删，归 D2）、例外主动 Remove（key 全局，A 的原本一起删）仅限「确认没人用」。

已知现状：引擎事件通道上看不到池副本删除（F10）→ 索引中池副本条目只增不减，条目过时只损性能（F3）；GPU 侧有 Removed（F8）；池原生 publisher 有 removed 但 0.3.11 不含、载荷只说 key 怎么了不说在哪台机器（F14，E8 已核）——升级后由 kvcm 订阅、翻译成引擎格式重发（D1 §2 方案 b）。选路方进程形态与 D4 耦合；工程实现 E11-E14 依赖设计定型，E10/E11 骨架可先行。

**D2. KV 调度操作集（pin / demote / prefetch / retain / release）**

独立控制面进程对 KV 的完整操作面，每项定语义、与池驱逐策略的优先级、失败处理：

- prefetch：✅ 已验证（`CreateCopyTask` → READY）。
- pin：READY 后防驱逐。master 只有全局驱逐（`BatchEvict`、tenant watermark），无显式 pin API——靠 lease 机制（`SubmitReplicaActionProposal` → `ReplicaActionLease`）或池侧扩展。main 另有 **soft pin**（默认 30min / 上限 24h，`types.h:86-89`），挂在 `ReplicateConfig` 路径（put/replicate 时指定，非「给已有 key 加 pin」的独立 API），`allow_evict_soft_pinned_objects` 默认 true = 软保护。lake 文档有同义设计（「引用数 >0 冻结」，`../docs/architecture/kv-cache-pool.md`）可参考。
- retain：属主下线/长期未命中时副本存续，与 D3 直接相关；master 有 `AddReplicaForRetainedClient` 可考察。
- demote：DRAM→SSD 降层。master 有 offload/promotion 心跳与 `EvictDiskReplica`；READY 是否含落盘归入此项（若以落盘为 READY，等待落盘的时间会把预热收益抵消）。
- release：前缀失效主动释放，`Remove`/`BatchRemove`/`RemoveByRegex`/`BatchReplicaClear` 现成；归谁决策已闭环（D1 §6：router 提供会话生命周期线索，控制面决策，池兜底；含中间档 demote）。
- 事实输入：E6。

**D3. 副本生命周期与 READY 语义**

- 事实：副本无 pin，READY 后池可驱逐；属主实例下线后 key 连同远端副本被 master 元数据级移除（字节成无索引残留；exist 0/32 实测见 [EXPERIMENTS.md](EXPERIMENTS.md)）——D1 F5。lease 为 key 粒度：读续租、`CopyStart` 不续租、TTL/驱逐删 key 释放全部副本——D1 F15。
- 待定：READY 是否 =「本机 DRAM 必达」；保活策略；属主下线后副本归属（保留/驱逐/迁移）。
- 保活机制事实（D1 F15）：`batch_is_exist` 顺带续租（`ProbeKey` 不续），默认 TTL 10s——预复制不续租则一个 TTL 窗口内无流量即可驱逐；保活循环必须有界（会话期 / 固定窗口 / 首流量后停租），无限续租 = 变相 pin。策略（续多久、何时停）本节定型。
- 落盘子项：Mooncake DRAM→SSD（`enable_ssd_offload`，本目录默认 `false`）是池内冷热，不是「预复制直指定 disk」；SSD 仅作容量/恢复异步 demote。
- 直接影响故障恢复时已预热前缀的可用性（D5）。事实输入：E5。

**D4. 编排集成**

- 待定：READY 信号如何接 gateway/router：谁发起预取、何时放行、失败重试。过载/准入仍归 gateway，推理侧只管执行。
- 待定：Prefetch Orchestrator 形态（旁路进程或并入 Router）：收 intent → 调 B2 → READY 确认 → 放行。
- 边界（随 D1 §2 方案 a，已闭环）：router 消费引擎 kv event，预复制放置由控制面补发事件注入（D1 §2）；控制面侧 seg→实例拓扑由编排层提供（测试床 resolve.py 探测链是取证手段，不是生产路径）。
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

## 三、工程项

### A. 验证类（可直接开工，除注明外）

- **E1. 真机冒烟**——`run_e2e.sh` 一键（同构 TP=2 + 异构双向）+ 同构跨机 rank↔seg 新路径重跑。实质路径已手工验证，单测 19 例 + 干跑覆盖，风险限编排层。
- **E2. key 哨兵容器单跑**——`test_keys.py` 在容器内跑通（运行期 `batch_is_exist` 32/32、36/36 证据已齐）。
- **E3. 多卡自动匹配验证**——resolve 的 `VLLM::Worker_TP<N>` proctitle 匹配在多卡真机走通（现为 `--targets` + 手工对号；失效时明确报错而非静默错配）。
- **E4. 1-token 翻转根因**——旧非 DEBUG 实例偶发，新实例未复现；首次外部读输出逐字一致，疑路径交替数值扰动。生产化前必须查清（[EXPERIMENTS.md](EXPERIMENTS.md) #10）。
- **E5. 副本消失机制核实**——属主下线后 key+副本移除的时机与条件（TTL、GC 扫描窗口）对照实验 → D3 事实输入。
- **E6. 0.3.11 API 子集核对**——D2 引用的 lease/offload/promotion 是 mooncake main 的符号，测试床 wheel 是 0.3.11.post1，逐个核对可用子集，缺的即池侧扩展工作量 → D2/P5 输入。新增核对：`BatchExistKey` 的 `grant_lease` 续租语义与 `BatchProbeKey`（D1 F15）、soft pin（D2 pin 候选）、`default_kv_lease_ttl` 实际配置值。
- **E7. MLA 实测**——`put_step>1` 时 plan 全落 `targets[0]`、其余 rank 读远端，功能推断正确未实测；hybrid/sparse 被上游 gate 排除（异构不支持，同构可用）。
- **E8. mooncake kv_event schema 核对**——✅ 已闭环（落 D1 F14）：publisher 含 stored/removed/cleared，**驱逐发 removed**；载荷 = key+medium+tenant+group，**无 seg/机器位置**；ZMQ 传输；CMake 默认 OFF、官方 wheel 构建 ON。**0.3.11.post1 不含此机制**（publisher 代码在 v0.3.11 之后进仓，tag 上无 `kv_event/` 目录、无构建开关）——D1 路径 b 要用须升级 mooncake 版本。
- **E9. vLLM 事件载荷核对**——✅ 已闭环（结论落 D1 事实基线 F7-F10）：`BlockStored` 含 `medium` 字段；两个发布源——引擎核发 GPU 块事件（含 Removed，上游既有）、连接器发池 put 事件（无 Removed）；预复制绕引擎结构性无事件。

### B. 服务化重构（骨架可直接开工，接口依赖设计）

- **E10. 面向对象重构**——现 precopy 包是面向过程脚本（keys/resolve/common/precopy 四模块函数式），重构成类结构（KeyComputer / SegResolver / StoreClient / Orchestrator），明确状态归属与接口边界，为进程化打底。**可直接开工**。
- **E11. 进程化**——一次性脚本 → 常驻服务：接收预热 intent、并发处理多任务、任务状态机（排队/进行/READY/失败）、优雅退出（现 `store.close()` 只修了单次退出崩溃，进程化后生命周期要重构）。骨架可直接开工；intent 接口先用占位，随 E12 定型。
- **E12. 与 router 通信**——intent（预热请求：前缀/目标实例）与 READY/失败回执的通道与协议（gRPC 候选，与仓内跨语言 RPC 选型一致）。协议字段依赖 D4 定型；通道骨架可先行。
- **E13. 与服务控制面通信**——推理实例部署/销毁事件接入：新实例拉起 → 触发热点前缀预热；实例销毁 → 其 seg 副本清理/归属处理（与 D3/D5 耦合）。事件源选型（K8s watch / 部署系统通知）依赖 D4；接入骨架可先行。
- **E14. KV event 通信**——随 D1 §2 方案 a/b（已闭环），控制面自产引擎格式事件。**pub 端**：直接 import 上游 `ZmqEventPublisher`（`kv_events.py:305`），事件字段取自 keys.py 中间产物（hash 链 / token ids），READY 核验后按目标实例各发一条 `BlockStored`；每目标实例一个发布端点，「端点 → 实例」注册归 D4/E13。**升级 mooncake 后加 sub 端 + 翻译**：订阅 master publisher（F14），PoolKey 解析 block hash、rank 去重，重发 `BlockRemoved` / `AllBlocksCleared`；引擎事件 sub 端归 router 侧。pub 骨架可直接开工。

### C. 性能与生产化

- **E15. copy 并行与限速**——现逐 key 串行、与在线读共享带宽；大批量预热需并行化 + 带宽控制。可直接开工。
- **E16. 生产化缺口**——EP 拓扑实测、master HA、监控指标（READY 延迟、copy 失败率、external hit；命中率归因口径与 P8 耦合）。

## 参考锚点

- Dynamo KV-aware router 外部重算 hash 链：[../docs/research/dynamo/overview.md](../docs/research/dynamo/overview.md)
- llm-d EPP 推测索引（事件确认前先用推测条目，TTL 2s 自动过期）：[../docs/research/llm-d/](../docs/research/llm-d/)
- AIBrix ZMQ 事件同步：[../docs/research/aibrix/](../docs/research/aibrix/)
- mooncake master API（submodule 源码，符号锚点）：`master_service.h`（`CreateCopyTask` / `SubmitReplicaActionProposal` / `Remove` / `BatchEvict`）、`master_config.h`（`enable_kv_events` 开关，E8）、`master_service.cpp`（`CopyStart:6444` 幂等三层防护；`CreateCopyTask:13677` 提交层无去重；reaper `10654` 附近）
- 关键差异：现有系统的事件都是单源（引擎侧或池侧），我们是双源（引擎原生事件 + kvcm 补发/翻译的池事件），权威归属与合并规则要自己定（D1 §2/§3）。
