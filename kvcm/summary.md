# kvcm 总结（summary）

来源：[issue #54](https://github.com/5x8-40/lake/issues/54)（KV-CM 遗留问题总清单）。本文是 kvcm 预复制生产化的总结文档：设计决策、潜在问题、工程项三类条目的总登记处——已闭环条目在原地标 ✅、结论指向正式文档，待定条目随讨论与实验更新。

文档分层（详见 [README.md](README.md) 导航）：`design/` = 全局设计（D1/D2/D9/code-architecture）与专项设计（kv-prefetch，仅 copy）；调度中心不在 kvcm，见 [../scheduling/](../scheduling/)；`demo/` = precopy demo 记录（仅验证 copy 一项的端到端链路）；本文 = 登记处。

编号：**D** = 设计决策（语义/选型未定，先决策）；**P** = 潜在问题（待评估）；**E** = 工程项。编号创建后不复用；条目闭环后在原位置标 ✅，结论落对应正式文档（`design/` 系设计文档 / `demo/` 系 demo 文档）。

依赖主线：D1（已闭环）/ D2 是操作面主线，D9 是 kvcm 服务面扩展；调度中心（D8）属于外部组件/Router（E5 已闭环，结论见本文 E 节）；E10→E11→E12/E13/E14 是服务化主线（重构先行、协议随后）；P1-P9 大多随 D1/D2/D9 定型一并回答；D8 的调度问题在 `scheduling/` 里回答。

状态标记：✅ 已闭环（附结论写在哪个文档）｜🟡 部分闭环｜无标记 = 待定。

**当前目标版本：Mooncake v0.3.13.post1（master / client 同版本）。** 文中出现的 0.3.11 / main 对照是版本差异记录，不代表当前部署基线；D9 与新增服务设计按 v0.3.13.post1 写；调度中心 D8 也按同一版本，但不在本登记处。

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
- 选路方进程形态与 D4 耦合；E13/E14 仍依赖设计定型。E10、E11（daemon / TaskEngine / KeepaliveLoop）、E12（SubmitIntent / GetTask）已落地

**D2. KV 调度操作集（pin / demote / prefetch / retain / release）**

独立控制面进程对 KV 的完整操作面，每项定语义、与池驱逐策略的优先级、失败处理：

- 操作集框架：✅ 已闭环（结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §1）——八项操作 copy/move/keepalive/pin/demote/promote/retain/remove（原子动作 / 元数据标记 / 目的型三类），复合流程由原子操作组合（prefetch = copy+核验+保活注册；会话结束降层 = 停 keepalive+demote；下线迁移 = copy+remove 或 move 一步）；状态记录四类（任务状态机 / 保活注册表 / 副本台账 / 会话绑定）；kvcm 单实例跟随 master，状态存 sqlite。tasks 与 keepalive 有写入方。replicas 与 sessions 表在，还没有写入方。
- 遗留：move 的源侧 BlockRemoved、目标侧 BlockStored，以及 remove 的 BlockRemoved，等与 E14 发布端合入后接。本次不发。
- 遗留：`SubmitIntent` 现在只有预热（前缀 × 目标实例）。move / pin / demote / promote / remove 只有操作，没有 intent。这是大遗留，当前不扩协议、不加流程。同前缀操作串行化（prefetch / demote / remove / pin）也未做，归 A16，后续单独设计。操作粒度也未统一（copy 核验仍在 query_task；move/demote/remove 未拆 flow），归 A17，后续逐步改造。
- locate：进程内「实例 → rank 有序段名单」。intent 和操作入参没有 segment。没登记的实例报错。
- remove：**全局删所有副本，不支持 target**。无论 force 都先注销保活。`force=False` 再等满一个 lease TTL，然后 `batch_remove(..., force=False)`。`force=True` 注销保活后立刻 `batch_remove(..., force=True)`，不等；但它只跳过 lease，未完成副本和在途 copy/move 仍会拒删。失败或取消不把登记加回去，注销过就留注销，调用方自己重试，租约可以过期。
- 保活续租用 BatchExistKey（`renew_once` → `exists` → `batch_is_exist`），不用 ProbeKey。`register_keepalive` / `unregister_keepalive` 只登记或注销 Ledger 里的同一张表；续租由 `KeepaliveLoop` 调 `renew_once`。**lease 是 key 级，保这个 key 的所有副本；target 不是保活维度。**
- 副本核验用主线 `GET /batch_query_keys`，用传输端点比段还在不在。提回删掉了用 offload 端点对 seg 名的检查（offload RPC 用 port 0 由内核分配，seg 名是 `local_hostname_`，不是同一次真机测量），仍打会累计命中的 GetReplicaList（默认 2 次），再用内存副本传输端点对 `locate(target)`，同步核验，不轮询。daemon `--admin` 必填；`--capability` 默认 `main`。`v0311` 启动直接失败：那个端点有续租副作用，也没有盘副本字段。
- 软钉空写（设计；实现见后续 stacked PR）：`_get_pyclient_capsule` 把 `setup` 建好的那一个 client 传进单独的 `_empty_pin.so`，调用 `MasterClient::UpsertStart` / `UpsertEnd`，不写数据；要求 Case B（尺寸一致、单 slice、key 存在、无盘副本），pin 前 `exists` 持 lease，`UpsertStart` 后做 buffer 地址检查，不一致就 `UpsertRevoke`。lease 挡驱逐/非 force `Remove`，不挡 `Remove(force=true)` 或 Case C；Case C 旧 buffer 进 `discarded_replicas_` 延迟释放（默认 600s），检查时不会地址复用，地址复用残余主要来自 `exists` 后发生 force remove / stale 清理 / lease 过期驱逐，或 `UpsertStart` 惰性清理 / processing 抢占后无 COMPLETE 副本的 Case A。类型在 `mooncake_pin_api.h`，成员地址由编译器算，已与完整类定义对过。wheel 不带 C++ 头文件，扩展只链 `store.so`。`get_real_client()` 是退路：只有以后真实调用失败，才包含 `pyclient.h`，在对象还原成 `MooncakeStorePyWrapper` 之后取出已建 client。现在不走这条。client_id 用方案一：`UpsertStart` 发出的是这个 client 自己的 UUID，空写会把 `client_id` 写成 kvcm 的。目前没感受到权限影响。借名（方案二）仍然做不到，不是当前路径。同一次调用里 `UpsertEnd` 失败会 `UpsertRevoke`。进程死在 Start 与 End 之间时副本留在 PROCESSING。扫任务记录再补 End/Revoke 的崩溃恢复当前不做（D2 F5）。保留身份先不动。
- prefetch：✅ 已验证（`CreateCopyTask` → READY）。
- pin：READY 后防驱逐——设计；soft pin 实现在后续 stacked PR，core/demote 当前 `soft_pin_after = NO`。`pin(HARD)` 拒绝，hard 级 lease 保护用显式 `register_keepalive()`，结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §2
  - soft pin 级（驱逐时排最后，极端情况仍可逐）——**有条件可用空写（目标版本）**：
    - 好处：不动全局 lease；soft pin 标记断线窗口 30 分钟级（但 `exists` 的 lease 先过期，之后只在第二轮驱逐里排最后）
    - 代价：官方绑定没有拆开的 upsert。空写走单独的 `_empty_pin.so`（只链 `store.so`，见上）。client_id 用方案一
    - 限制：只有 Case B 安全；Case C-by-size / Case A / ABI / NoF-only 不支持（memory + NoF 可以）/ Start-End 崩溃 / 读者短暂不可读 / client_id 改写 / 不续期 / 并发重写是已知边界；地址检查挡住 A/C 的空 End，最坏重算。`pin(SOFT)` 只做一次 `exists`，不 `register_keepalive`；soft pin 只排第二轮驱逐，要 lease + soft pin 双保护需额外 `register_keepalive`。`soft_pin_after.channel = NO`（core/demote；soft pin PR 再置 YES）
  - hard pin 级：`pin(HARD)` **拒绝**（hard pin 创建时固化，事后设置不了；`hard_pin_after = NO`）。要 lease 保护请显式 `register_keepalive()`：同一张 Ledger 表，不读数据、不 Remove、不重 put、不空写、不改 hard pin 位。lease 时长是 master 的 `default_kv_lease_ttl`，不是 kvcm 对单个 key 改标记；kvcm 不改这个配置。它只是 lease 窗口，不是 hard pin
  - hard pin 候选 ② 读删写代价太大，不做
  - 取消：hard 级 lease 保护（显式 `unregister_keepalive()`）停续租，等满一个 lease TTL。没有马上撤销 lease 的调用，也不查剩余租约。空写 soft pin 的取消是主线 `SoftPinAction::DISABLE`。扩展只对着主线，0.3.11 的 soft_pin 通道是 NO
  - 完整限制、原因与 case（Case A/B/C、lease 边界、地址复用、ABI、版本）见 [D2-kv-operations.md](design/D2-kv-operations.md) §2「限制、原因与 case」
  - 版本依赖：本 native 扩展只承诺 **v0.3.13.post1**。0.3.11 / 0.3.12 是 `with_soft_pin` bool + `UpsertEnd(string)`，不兼容；0.3.13-rc/0.3.13 机制相同但未单独验证；0.3.14-pre 起有 `has_read_lease` gate，有租约时同尺寸也会走 Case C，不能用。完整表见 D2 §2「4. 版本依赖」
  - 读锁方案已否决：refcnt 是任务级保护，两个版本都没有读者引用 RPC
  - client_id：方案一是当前路径（空写写成 kvcm 的 UUID，目前没感受到权限影响）。方案二借名仍做不到，不是当前路径；机制说明留在 D2 §2
  - 机制事实：见 D2 文档 F1-F12（pin 形态、upsert 行为、client_id 管辖、lease 与驱逐、UUID 与空写通道）
- retain：🟡 机制已定，结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §4
  - 依据：清理只看副本挂在谁身上（内存副本跟段、盘副本跟登记它的 client），不看 key 的 client_id。要保留的是 KV 本身，不是那块内存
  - 四条可行路，顺序与 D2 时序图一致：
    - DFS 副本（main，正式路径）：写共享 3FS，下线清理不清它；但用量到高水位后 lease 过期且没 pin 的会被逐，要续租
    - move/copy 到还活着的 seg（E5 已证）
    - kvcm 自己挂段挂盘：`setup(global_segment_size)` + `setup(enable_ssd_offload, ssd_offload_path)` 启动时把 DRAM 段和本机盘一起挂上，再把副本 copy/move 进自己的段，由盘侧心跳落到自己的盘，最后 `NotifyOffloadSuccess` 登记（内部走 `AddReplicaForRetainedClient`）
    - 用旧 `client_id` 接管盘上数据：`MasterClient(旧id)` + `MountSegment`（参数自己填）+ `NotifyOffloadSuccess`（会更新已有 LOCAL_DISK 副本的 endpoint）
  - `AddReplicaForRetainedClient` 只登记元数据、不写盘：它的 `LocalDiskDescriptor` 只有 `client_id / object_size / transport_endpoint`，没有 `file_path`。必须先写文件再上报
  - 内存里那份随进程消失，所以要在引擎消失前把它转成别的载体。什么时候转、转去哪台，归编排层
  - 降到源引擎自己的盘、但没人接管：master 清登记，文件还在。同一台机器的 store 再启动会扫盘重新登记；引擎不再来或换机器读，就断了
  - 改 key 的 `client_id` 留不住：改的是鉴权字段，副本还在原段上
  - `#2991` 只是同一个 client_id 的重连窗口。窗口内 Ping 保住的是登记，字节仍在死进程里
  - 长期未命中 → demote 到 SSD，由策略层定。这是防驱逐，不是换 client
- demote / promote：结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §3
  - 框架：demote 是组合动作；promote 的 L3 只触发，核验与 READY 由 PromoteFlow 完成。存储统一归 mooncake（引擎实例挂本机 SSD 做分层，kvcm 自己不持存储）
  - demote：先确认前缀能展开成 key、并且 admin 已配置，再 `unregister_keepalive`。然后等满一个 lease TTL（`--lease-ttl-s`，默认 10，须与 master 的 `default_kv_lease_ttl` 一致；测试床实配过 11s；0.3.11 代码默认 5s，main 默认 10s）。kvcm 不改 master 的这个值。再用 `GET /batch_query_keys` 确认每个 key 的 `local_disk`；DFS（`disk_values`）不算成功，因为 promotion 只从 LOCAL_DISK 提回。不用 `GetReplicaList`（会续租）。没有立刻搬到盘上的 RPC。写回且池不满时仍失败。不读数据落盘，不调 `MountLocalDiskSegment`。失败或取消只恢复原本就有的保活行，且不重置窗口；原来没有保活行不会新建。成功不加回
  - 池侧前提仍是 `enable_offload`：
    - 写穿 / 写回两模式（≈ HiCache write_through / write_back）
  - promote 三条路：
    - `promotion_on_hit`：命中时池被动提回
    - kvcm 主动①：L3 `promote` 只打 N 次 `GetReplicaList` 触发；`PromoteFlow` 用 `check_promote` 轮询目标 memory replica。不拿 offload 端点对 seg 名
    - kvcm 主动②：读删写自己搬
    - promote 执行端 = 引擎来取（`PromotionObjectHeartbeat`，两版本都有，触发链不变）
  - copy/move 到不了 SSD（disk seg 没有 seg 名，两版本同）
  - 触发时机（会话结束 / 变冷 / DRAM 压力 / 计划下线）归策略层
  - 真正的功能缺口：写回模式下确定性的 demote——没有 per-key 降层 RPC，`BatchReplicaClear` 又限 key 的 client_id，offload 取消也不对外 → 需要池侧扩展
  - 确定性路径 = kvcm 挂 DRAM 段与本机盘，把副本搬进自己的段再由盘侧心跳落盘（D2 §3 / §4）。挂段挂盘都在 `setup()` 时做，不用补通道。`AddReplica` 不是独立 RPC，是 `NotifyOffloadSuccess` 的内部步骤；代价 = kvcm 变数据面，承担带宽与可用性
  - 每个操作都有完成信号；READY 特指 DRAM 副本写好、可以调度
- release：✅ 已闭环，结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) §4
  - 决策归属 D1 §6
  - kvcm 可用：`Remove` / `BatchRemove` / `RemoveByRegex`（要配 key 命名空间才有意义）/ `RemoveAll`
  - `BatchReplicaClear` 限 key 的 client_id 自己调——kvcm 动不了别人的 key
  - `EvictDiskReplica`（两版本）是通知型：`LOCAL_DISK` 限登记 client（只有盘副本已经在 kvcm 名下才能清自己的；现有通道卸不来引擎字节）、`DISK` 不验 client_id（误用风险已登记，不作正式路径）
  - `force` 仅限「确认没人用」
  - 收尾 = 清台账 + 同步索引
- 机制查缺补漏：✅ 已闭环（结论写在 [D2-kv-operations.md](design/D2-kv-operations.md) F13⑤/③、§1 对照表、§3 远期方案、§4 retain/release）
  - 做法：八项操作逐项过一遍「还能利用什么池机制」+ 三版本 RPC 注册表全量 diff（v0.3.11 与 v0.3.11.post1 完全相同）
  - 机制结论：
    - 动态副本（main）：kvcm 能触发、不能指定目标（反复调 `GetReplicaList` 观测，和 promote 同一种做法，观测在同一个函数里）——只适合池级自动调节，满足不了预热语义
    - offload / promotion 的执行端两版本都是引擎来取（`OffloadObjectHeartbeat` / `PromotionObjectHeartbeat`），kvcm 触发链不变
    - offload 取消不对外（`CancelQueuedOffloadTask` 唯一调用点是 `UpsertStart` 内部）
    - `EvictDiskReplica`（两版本）：通知型；`LOCAL_DISK` 限登记 client（盘副本已在 kvcm 名下才能清自己的），`DISK` 不验 client_id（误用风险已登记）
    - `AddReplica` 不是独立 RPC（是 `NotifyOffloadSuccess` 的内部步骤）。引擎自己落盘后上报走这条；kvcm 把副本落进自己的盘后也走这条上报登记
    - 没有保留身份注册。`kRetained` 是外部内存引用计数；`AddReplicaForRetainedClient` 是 master 内部的盘副本登记。`#2991` 是同一个 client_id 的重连窗口，不搬走字节
    - `BatchQueryIp`（两版本）：client_id→IP，副本位置解析的最后一环
    - `GetStorageUsageSnapshot`（main）：分层容量快照，demote 触发输入
    - 台账失效通知（main）：KV 事件流 removed/cleared；stored 事件没有位置信息，代替不了定期比对
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
  - 谁没有：keepalive / pin（不动数据）、retain（不把请求调回源实例；move/copy 那次搬运自己的完成信号另计）、demote（只记台账）、release（通知索引删除）
  - 「本机」= 本机 HBM + 本机池 seg 两层（D1 §3）
- 保活：🟡 机制已定、策略未定（机制 D2 §1/F9，事实 D1 F15）
  - 机制已定：`register_keepalive` 登记，`KeepaliveLoop` 调 `renew_once` → `ExistKey` 周期续租约（lease）——`batch_is_exist` 调用会顺带续租（`ProbeKey` 不续），TTL = master 配置（D2 F9），保护窗口 = 租约时长
  - 预复制不续租的话，一个 TTL 窗口内没有读流量就可能被驱逐
  - 约束已定：保活循环必须有界——无限续租事实上等于 pin；kvcm 断线后租约到期自然失效，不残留
  - 策略未定：续多久、何时停——候选 = 会话期 / 固定窗口 / 首流量后停租，未选定
- key 的 client_id 下线后的副本存亡：🟡 机制已定（D2 §4）——副本跟着所在 client。死前 move/copy 到活段留得住；kvcm 自己挂段挂盘把副本收进名下也留得住（挂段挂盘在 setup 时做）；DFS 副本下线不清，用量到高水位后 lease 过期且没 pin 会被逐，要续租。源引擎自己的盘：登记会被清，文件还在，这台机器的 store 再启动会重新登记。没有保留身份注册。拿旧 client_id 去 Ping 只保住窗口内的登记
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

**D8. 统一调度中心** — 外部设计，不在 kvcm 登记

- 调度中心（SC）不属于 kvcm：可以是独立组件，也可以嵌入 Router（或编排/控制面内置）。设计见 [../scheduling/README.md](../scheduling/README.md) 与 [../scheduling/design/D8-joint-scheduling.md](../scheduling/design/D8-joint-scheduling.md)。
- kvcm 只执行 intent、核验、记账、发布事件；不选路、不扩缩、不持有前缀索引。D8 的能力事实与 D9 共用同一版本基线。

**D9. 基于 kvcm 的服务地图（调度中心之外）** — 🟡 设计草案

- 定位：kvcm 不做选路、不做扩缩决策、不持有前缀索引；在“执行 + 核验 + 记账 + 发布”边界内，把八项操作组合成服务。
- 服务面：会话 KV（Session）、预热（Prewarm）、反熵/核验（Anti-entropy）、账本/归因（Ledger）、容量/放置顾问（Capacity Advisor）、下线/迁移（Drain）、治理（Governance）、回放/仿真（Replay）、分支/跨集群（Branch/Geo）。
- 优先级：P0 = 会话 KV（SESSION 级）+ 预热 + 反熵 + 账本最小集；P1 = 容量顾问 + Drain + 治理；P2 = 回放/仿真、分支/跨集群/rollout。
- **收益排序**：会话保留（避免 resume 重算）> 预热（避免跨机传输/防 miss）> 反熵/账本（让 ROI 可信）> 容量/Drain（保命中率）。
- 关键约束：session 权威在 kvcm ledger；保留默认软释放（停 keepalive）；soft pin 有条件可用、hard pin 拒绝；反熵用 v0.3.13.post1 的零副作用 admin 查询。
- 工作文档：[D9-kv-services.md](design/D9-kv-services.md)。
- 待定：pin 前置条件、反熵成本、prewarm 取消、多模型/多租户、retain/跨集群。

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
  - 对设计的落地：改 key 的 client_id 对副本存亡无效（只动鉴权字段）；move 到活段有效；DFS 副本不挂任何 client。kvcm 挂自己的盘卸不来引擎上的字节，不作为现在的做法。没有保留身份注册
  - 剩余事实输入：lease TTL 11s（master 实配 `default_kv_lease_ttl=11000` 毫秒）+ 驱逐候选资格（lease 过期→驱逐首选）仍归 D3/TTL 议题
- **E6. 0.3.11 API 子集核对**——✅ 已闭环（结论写在 D2 F13）
  - 做法：三个版本的 RPC 注册表 diff（v0.3.11 与 v0.3.11.post1 完全相同）
  - 0.3.11 已有：lease / offload / promotion 心跳取任务、Admin 查询三件套、`EvictDiskReplica`、`BatchExistKey` 续租（D1 F15）、soft pin（D2 F9）
  - main 才有：`ProbeKey`/`BatchProbeKey`、租户配额五件套、容量快照、`UnmountLocalDiskSegment`；`has_read_lease` 门槛在 0.3.14-pre/main
  - **0.3.13.post1 补充**：零副作用 Admin 查询（`GetReplicaListForAdmin`/`GET /batch_query_keys`）、KV 事件 publisher 代码、`SoftPinAction` 三态已具备；publisher 默认构建 OFF/运行 false
  - `default_kv_lease_ttl` 测试床配置 11s（D2 F9）
  - 缺口清单 = D2 F13⑥（per-key 降层 RPC、copy/move 取消、key→client_id 查询）——kvcm 侧组合达不到完整语义，要补得动 mooncake 池侧（copy 取消有 kvcm 弱版「弃置 + 事后清副本」，P9）；改不改、何时补未议
- **E7. MLA 实测**——`put_step>1` 时传输计划全部分配到 `targets[0]`、其余 rank 读远端：功能上推断正确但还没实测；hybrid/sparse 被上游的开关检查排除（异构不支持，同构可用）。
- **E8. mooncake kv_event schema 核对**——✅ 已闭环（结论写在 D1 F14）

publisher 含 stored/removed/cleared，**驱逐发 removed**；载荷 = key+medium+tenant+group，**无 seg/机器位置**；ZMQ 传输；CMake `ENABLE_KV_EVENTS` 默认 OFF，运行 `--enable_kv_events` 默认 false；copy 路径零发布。**0.3.11.post1 不含此机制**（publisher 代码在 v0.3.11 之后进仓）；**0.3.13.post1 含代码**，但是否编译进 wheel、是否运行开启要按部署确认——未显式开启时删除面仍不可见。

- **E9. vLLM 事件载荷核对**——✅ 已闭环（结论写在 D1 事实基线 F7-F10）
  - `BlockStored` 含 `medium` 字段
  - 两个发布源：引擎发的 GPU 块事件（含 Removed，上游既有）、连接器发的池 put 事件（无 Removed）
  - 预复制不经过引擎，结构上就没有事件——只能靠控制面补发

### B. 服务化重构（骨架可直接开工，接口依赖设计）

- **E10. 面向对象重构**——现 precopy 包是面向过程脚本（keys/resolve/common/precopy 四模块函数式），按五层架构重组（L3 原子操作抽象屏蔽后端语义 + L4 mooncake 适配器 + L2 编排层只调 L3），明确状态归属与接口边界，为进程化打基础。✅ **已落地**（Ledger / L3+L4 / L2+API）。设计：[code-architecture.md](design/code-architecture.md)
  - **已知边界（rebuild 任务归属）**：重启恢复（`MooncakeBackend.rebuild`）不区分任务归属——账本里全部 running 任务都按本实例的模型口径重算；多模型/多实例共用同一 Ledger 时会错算。当前单实例单模型无影响；**多模型前必须先加归属判定（model_id 不符则跳过）**。设计文档补全时写入
  - **后续已定（接口 KeySet 化）**：L3 加 `keys_of(prefix) -> KeySet`（不透明类型），操作方法入参从 Prefix 改为 KeySet——长序列下消除「一次预热算两遍 key」。方案与分层论证见 [code-architecture.md](design/code-architecture.md)「接口演进：KeySet」；当前不动代码，后续实施
- **E11. 进程化**——一次性脚本 → 常驻服务：接收预热 intent、并发处理多任务、任务状态机（排队/进行/READY/失败）、优雅退出（现 `store.close()` 只修了单次退出崩溃，进程化后生命周期要重构）。✅ **已落地**：daemon、TaskEngine、KeepaliveLoop 已在进程里跑。分层与模块划分：[code-architecture.md](design/code-architecture.md)
- **E12. 与 router 通信**——intent（预热请求：前缀/目标实例）与 READY/失败回执的通道与协议（gRPC，与仓内跨语言 RPC 选型一致）。✅ **已落地**：`SubmitIntent` / `GetTask`。协议字段里仍依赖 D4 定型的部分没有扩。
- **E13. 与服务控制面通信**——推理实例部署/销毁事件接入：新实例启动 → 触发热点前缀预热；实例销毁 → 其 seg 副本清理/归属处理（与 D3 耦合；D5 已闭环，见本文）。事件源选型（K8s watch / 部署系统通知）依赖 D4；接入骨架可先行。
  - **实例注册一次交全**（D1 §3b，已定，代码未接）：字段 = 实例 id、rank 有序段列表、补发端点、DP 编号、kind（可空）。管理面分配端点，kvcm 只校验不分配。
  - 与现有启动参数的关系：`--instance-segs` / `--kv-event-endpoint` 保留成 demo 兜底；同一实例不允许两处都登记，启动时报错。
  - kind 留空的实例，READY 后不发 `BlockStored`（只记日志）。宁缺一条，也不发一条 kind 错的。
  - 通知索引与实例注册同一流程：注册是别人告诉 kvcm，通知是 kvcm 告诉别人。通知失败只记日志、不回滚注册。
- **E14. KV event 通信**——随 D1 §2 方案 a/b（已闭环），控制面自己产生引擎格式的事件
  - **发布端（pub）**：
    - 直接 import 上游 `ZmqEventPublisher`（`kv_events.py:304`）
    - 事件字段取自 `hash_token_block_chain`。发出前每个 hash（以及非空的 `parent_block_hash`）过上游 `maybe_convert_block_hash`：默认发低 64 位整数，不发原字节，也不发十六进制字符串（Dynamo 不接收 bytes）。token 只到满块，个数 = len(block_hashes) × block_size，不满一块的尾巴不进事件，满块链为空则不发（D1 §3）。`medium` 用上游常量 `MEDIUM_CPU`，字符串是 `"CPU"`。`group_idx` 补发填 0，同时 `kv_cache_spec_kind="full_attention"`。`extra_keys` 这次不填：`hash_block_tokens` 传 `None`。这次只覆盖纯文本全注意力。滑动窗口、Mamba 不能用这个 kind
    - 操作对应：复制、移动的目标、主动提回 → `BlockStored`；移动的源、删除 → `BlockRemoved`；保活、钉住、停保活不发。本次代码只在预热 READY 发 `BlockStored`，移动和删除在另一分支，合入后再接
    - READY 核验后按目标实例各发一条 `BlockStored`（一条装下连续满块链）。重启后仍在跟踪的 copy，轮询前用 `target_of` 读出登记的目标实例，READY 后同样补发。补发失败（没有端点或发送抛错）只记日志，不把已经 READY 的操作判成失败。预热仍成功，保活照常登记
    - 每个目标实例一个发布地址，不按 seg 或 rank 拆。发布路径不调用 `/register`，也不订阅 master publisher；把地址交给索引仍归 D4/E13
    - 端点与 kind 的权威来源是实例注册（D1 §3b）：注册里 kind 留空的实例，READY 后不发 `BlockStored`，只记日志。启动参数 `--kv-event-endpoint` 是 demo 兜底，与注册同一实例不允许两处都填
  - **删除信号（升级 mooncake 后，本分支不实现）**：
    - 首选：索引侧直接订阅 master publisher、解析 object_key（Conductor 现成，D1 §2 方案 b）
    - 备选：kvcm 加订阅端 + 翻译——订阅 master publisher（F14），从 PoolKey 解析出 block hash、按 rank 去重，重发 `BlockRemoved` / `AllBlocksCleared`；解析对照 mooncake-conductor `object_key_parser.cpp:160-276`（`ParseVllmObjectKey`，已覆盖 vLLM-Ascend key 布局）。本分支不订阅、不翻译
    - 引擎事件的订阅端归索引侧
  - **端点生命周期**：
    - `--kv-event-endpoint instance_id=tcp://*:port`。引擎继续用自己的端口，kvcm 另绑这个地址
    - 当前 DP=1：`data_parallel_rank` 固定 0，不做端口偏移。上游 `offset_endpoint_port`（`kv_events.py:528`）在秩 0 时端口不动；目标实例 DP>1 时的消费侧归组仍未做
    - 第一次要给该实例补发时才构造 `ZmqEventPublisher` 并绑定。实例下线不关。`shutdown` 只在 kvcm 进程退出时调用
    - 端口交给索引仍归 E13，不在补发函数里做

### C. 性能与生产化

- **E15. copy 并行与限速**——🟡 **网卡利用率限速仍然不做**。并行窗口已落地（E11：`asyncio.Semaphore`，默认 4，`--copy-submit-concurrency`）。10 月 29 日跨机（[demo/EXPERIMENTS.md](demo/EXPERIMENTS.md) #17，与 #16 单机 `TRANSFER_FAIL` 不是同一次实验）：42 条 `create_copy_task` 在窗口 4 下约一半失败，主错误是 `NO_AVAILABLE_HANDLE`（`max retries reached: 10`），master 留内存失败；14 条全成。提交前只查计划第一条的目标段，只加落在这一段上的新字节；其它段的剩余和字节都不参与，这一段不够就一个 key 都不发。没查的段仍可能在 `CopyStart` 时 `NO_AVAILABLE_HANDLE`。这已经在本分支落地；它不代替 E15，也不做网卡采样。实验当时也没跑这条预检。另一条前缀已经提交、但还没在目标段划出缓冲区时，剩余里看不见，两条都可能放行。彻底避开代价高，接受这个窗口。Router 侧用网卡信号打分归 D1 §5。
- **E16. 生产化缺口**——EP 拓扑实测、master HA、监控指标（READY 延迟、copy 失败率、external hit；命中率归因口径与 P8 耦合）。

### D. 服务化（D9）

- **E17. 反熵 + 账本最小集**——按 [D9](design/D9-kv-services.md)：热条目抽样核验（v0.3.13.post1 用 `GET /batch_query_keys` / `GetReplicaListForAdmin`，零副作用）；`LOST/REMOVED` 事实回写；ledger 增加 bytes/ROI/归因字段。
  - 验收：ghost 检出率、误判率、查询 QPS 预算；预取/保留 ROI 可算；不因核验阻塞请求。
- **E18. 会话 KV 服务（SESSION 级）**——Open/Attach/Pause/Resume/Close；keepalive 有界续租；TTL/预算；默认软释放。
  - 验收：resume hit rate、resume TTFT、wasted retention；PIN/DURABLE 随 D2 能力后置。

## 参考锚点

- Dynamo KV-aware router 外部重算 hash 链：[../docs/research/dynamo/overview.md](../docs/research/dynamo/overview.md)
- llm-d EPP 推测索引（事件确认前先用推测条目，TTL 2s 自动过期）：[../docs/research/llm-d/](../docs/research/llm-d/)
- AIBrix ZMQ 事件同步：[../docs/research/aibrix/](../docs/research/aibrix/)
- mooncake master API（submodule 源码，符号锚点）：`master_service.h`（`CreateCopyTask` / `SubmitReplicaActionProposal` / `Remove` / `BatchEvict` / `CreateDrainJob`）、`master_config.h`（`enable_kv_events` 开关，E8）、`master_service.cpp`（`CopyStart:6444` 幂等三层防护；`CreateCopyTask:13677` 提交层无去重；reaper `:10653-10700`）
- mooncake Conductor（池事件的自家消费者）：`mooncake-conductor/src/kvevent/object_key_parser.cpp`（`ParseVllmObjectKey:160` / `ParseSglangObjectKey:99`）、`src/zmq/msg_decoder.cpp`（`ParseVllmEvent:365` / `DecodeVllmEventBatch:886`——vLLM 引擎格式解码，kvcm 补发「一套接口」可被消费的实现证据）、`docs/source/design/conductor/conductor-architecture-design.md`
- 事件/索引标准化与消费先例：RFC #1527（引擎 → KV 感知 router 事件线格式）、RFC #1403（Indexer API：`/register` 注册事件源端点、`/query` 与 `/query_by_hash` 两种查询入参）；Dynamo 池事件消费先例 `lib/llm/src/kv_router/shared_cache.rs::HicacheSharedKvCache`；Dynamo RFC #1403 实现 `lib/kv-router/src/services/indexer/`（`standalone-indexer.md`）
- 关键差异：现有系统的事件都是单源（引擎侧或池侧），我们是多源（引擎原生事件 + kvcm 补发 + 池 publisher 删除事件），权威归属与合并规则要自己定（D1 §2/§3）。
- vLLM-Ascend AI-QoS（流量分类 + UB 交换机 Virtual Lane 优先级调度；KVCache 传输与集合通信/算子下发冲突的生产级证据；执行层 QoS，与我们调度层避让互补）：vllm-project/vllm-ascend `tools/ai_qos/` + `docs/source/user_guide/feature_guide/Ai_QoS_introduction_en.md`（消费点：D1 §5 选路打分、E15 预热带宽控制）
