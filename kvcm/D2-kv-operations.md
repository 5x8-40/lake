# D2: KV 调度操作集

- 本文解决什么问题
  - kvcm（独立 KV 管理控制面）需要对池里的 KV 做调度操作（复制、防驱逐、降层、保留、释放）
  - 本文定这些操作的语义、实现机制、失败处理
- 状态: **讨论中**
  - 操作集框架已闭环（§1）
  - pin：**设计完成**——soft 三候选、hard 两候选、属主两方案已摆平（§2）；裁决依赖外部条件（mooncake 可改性、kvcm 语言），不阻塞后续议题
  - demote：**已闭环**——会话级降层 = 停保活 + 落 SSD 两路径（§3）
  - retain / release 待议（[backlog.md](backlog.md) D2）
- 上游问题: backlog D2——独立控制面进程对 KV 的完整操作面，每项定语义、与池驱逐策略的优先级、失败处理
- 版本锚点（mooncake 两个版本并存引用，文中标注 `@版本`）
  - **0.3.11.post1**：测试床实际部署的 wheel
  - **main @ 1fc27b62**：submodule 源码，代表升级后获得的能力

## 参考实现

| 参考 | 锚点 | 借什么 | 关键差异 |
|------|------|--------|----------|
| Mooncake master 写路径 | mooncake 仓内 `master_service.cpp::UpsertStart`（@0.3.11） | 已有 key 事后加/解 soft pin 的唯一现成通道：同尺寸 upsert 会顺带设置/清除 soft pin（F3） | 该路径为「覆盖写」设计；本文的「空写」（声明更新但不写数据，只为触发元数据变更）是复用其副作用，非官方用法 |

## 事实基线（代码核实）

### 背景概念

- **key 与副本**：池里一把 key 存一份 KV 数据；同一 key 可有多个副本（replica），落在不同机器的 segment（seg，一段注册进池的 DRAM）上
- **副本两个状态**：写中（PROCESSING，不可读）与就绪（COMPLETE，可读）
- **属主（owner）**：每把 key 的元数据里记录 `client_id` = 写入它的客户端 UUID
- **属主管辖范围**（F6 详述）：只影响「属主下线时 key 是否级联删除」和「谁能撤销未完成的写」，与复制、读取无关
- **lease（读租约）**
  - 读路径（`GetReplicaList` / `ExistKey`）授予，lease 期内该 key **绝对不可逐**（驱逐硬过滤直接跳过，F11）
  - **新 put 的 key 出生无租约**：`PutEnd` 里 `GrantLease(0, ...)` 把 lease_timeout 设为 now——「the object has no lease at beginning」（`master_service.cpp:1422` 注释 @0.3.11）；第一次被读/touch 才获得保护
- **kvcm 自身 = mooncake client**：client 是库不是独立进程；kvcm 调 copy/move/upsert 必须链接它，构造时自动持有一个 UUID——§2 方案一中「kvcm 自己的 UUID」零额外成本

### pin 机制

- **F1 pin 的两种形态**
  - **soft pin** = 限时驱逐保护
    - 到期自动失效，需保活续期才能长期有效
    - 保护不是绝对的：master 驱逐分两轮——第一轮只从无 soft pin 的 key 里挑；凑不够且 `allow_evict_soft_pinned_objects`（默认 true）时，第二轮才逐有 soft pin 的。语义 =「最后才被逐」，不是「不逐」
  - **hard pin** = 不可驱逐（绝对保护，无到期概念）；被 hard pin 的 key 仍可正常读写、upsert
  - 设置方式（per-key，config 随每次写请求携带）
    - 0.3.11：`ReplicateConfig.with_soft_pin / with_hard_pin` 布尔开关（`replica.h:84-85`）；soft pin 时长取全局配置 `default_kv_soft_pin_ttl`
      - 坑：布尔默认 false——upsert 一个已有 pin 的 key 时忘带 true，已有 pin 被清除（F3②）
    - main：soft pin 升级**三态** `SoftPinAction`（`replica.h:64-68`）——`PRESERVE`（=0，**默认值**，不动现状）/ `ENABLE`（开启，可带 `soft_pin_ttl_ms` 单次覆盖，省略用 master 默认 30min、上限 24h，`types.h:85-89`）/ `DISABLE`（清除）——三态修掉了 0.3.11 的布尔坑；无新增 RPC 入口
  - 可达性（关键约束）
    - 新建 key：写入时两种 pin 都可带
    - 已有 key：只能改 soft pin（经 upsert，F3）；hard pin 创建时固化、之后改不了（`master_service.cpp:1737` 注释 @0.3.11）
  - `CreateCopyTask` 不接受写请求配置（两版本同）→ kvcm 的复制路径带不了 pin，pin 必须事后单独加
  - 排除项：`AddReplica`（`master_service.cpp:1426` @0.3.11）是磁盘副本登记接口（客户端在本地盘存了数据后向 master 报备），只收 LOCAL_DISK、不带 pin——对「已有 key 事后加 pin」此路不通
- **F2 UpsertStart 的三种情形**（`master_service.cpp:1564` @0.3.11）
  - master 按「key 是否存在、尺寸是否一致」分三条路径
    - Case A（key 不存在）：按新 key 分配 buffer
    - Case B（存在且尺寸一致）：**原位更新**——复用原有 buffer 地址，不重新分配；pin 相关行为都发生在这条路径（F3）
    - Case C（存在但尺寸不同）：丢弃旧副本、按新尺寸重分配；旧 pin 设置保留/合并到新元数据
  - 拒绝条件
    - 该 key 的副本正被读引用 → `OBJECT_REPLICA_BUSY`（改写中的 buffer 会破坏在途 RDMA 读；检查是 per-key 的，不影响其他 key）
    - 该 key 有 copy/offload 任务在途 → `OBJECT_HAS_REPLICATION_TASK`
  - 参数校验在分支判断之前：要求副本数等配置合法（`replica_num≥1`）——空写虽不走分配，config 也须填合法值
- **F3 Case B 的四个行为**（`:1698-1737` @0.3.11）
  1. **改写属主**：`metadata.client_id = client_id` 无条件执行——「谁最后写，谁是属主」是 upsert 的固有语义，真实写数据与空写都触发
  2. **设置/清除 soft pin**：按 config 的 `with_soft_pin`——true 且当前无 pin → 授予；false 且当前有 pin → 清除
  3. **hard pin 不动**（F1 可达性）
  4. **副本标为写中**：COMPLETE→PROCESSING，该 key 对所有读者不可见，直到 UpsertEnd 标回——空写的「不可读窗口」来自这里
- **F4 UpsertStart 不验身份**
  - 对调用方 client_id 无任何校验（不要求等于原属主、不要求活跃注册，只查参数合法性）
  - 推论：kvcm 调用时把**原属主的 UUID 原样传回**，行为 1 就把属主赋回它自己 = 属主实际不变，pin 照加（「借名」方案的机制基础）
  - 一个例外条件：upsert 开头 master 顺手做**单 key 惰性清理**（`CleanupStaleHandles`，`:2530`）——只遍历当前这把 key 自己的副本（O(副本数)），把挂在已死客户端 seg 上的副本清掉；若清完无有效副本，请求落入 Case A 新建。所以借名 upsert 要求原属主当时在线（预热场景成立）
- **F5 Revoke = 写中止**
  - `UpsertEnd / UpsertRevoke` 直通 `PutEnd / PutRevoke`（`:1770` 附近 @0.3.11）
  - 用途：PutStart/UpsertStart 分配了 buffer、副本标为写中之后，客户端写失败或放弃 → 调 Revoke 把写中的副本释放掉
  - 约束
    - 校验属主：非属主调用 → `ILLEGAL_CLIENT`（只有发起写的人能中止）
    - 只撤写中副本；副本已就绪则拒绝 → `INVALID_WRITE`
  - 与 kvcm 的关系：空写若在 Start/End 之间崩溃，副本卡写中——恢复时扫描任务记录，补 End 或 Revoke 清理（归 §1 状态记录①的崩溃恢复）

### 属主字段管辖范围

- **F6 属主管辖两件事，其余一概不管**
  1. **级联删除**：属主进程心跳超时被 master 判定下线后，它写入的 key 连同所有副本（包括 kvcm 预热到其他机器上的）一起被元数据级删除（D1 F5，实测 exist 0/32）
  2. **写中止权限**：Revoke 校验属主身份（F5）
- **不管**：copy / move / get / exist——复制与读取路径完全不读属主字段（F7 代码核实）

### copy / move 机制

- **F7 CreateCopyTask**（`master_service.cpp:5777` @0.3.11）
  - 入参：key + 目标 seg 名列表；**调用方不能指定从哪个副本复制**
  - 源选择：master 从该 key 现有副本所在的 seg 里**随机挑一个**（`:5816`）
  - 执行模型：master 不搬数据，只生成任务记录；任务派给「源 seg 所属的客户端进程」执行——复制要求源属主在线
  - 校验：key 存在、目标 seg 已挂载可分配
- **CreateMoveTask**（key, source, target，`:5845`）
  - 唯一能指定源的路径；语义是**移动**——完成后源副本删除

### lease 与驱逐

- **F10 驱逐算法的完整过滤与排序**（`BatchEvict`，`master_service.cpp:4467` 起 @0.3.11）
  - 硬过滤（两轮通用，满足任一即跳过）
    - hard pin → 永不候选
    - lease 未过期 → 跳过（lease 期内绝对安全）
    - 副本 refcnt>0（正被读引用）→ 该副本不可逐（per-副本字段 `get_refcnt()`）
  - 第一轮：无 soft pin 的候选；不够且 `allow_evict_soft_pinned_objects_` 时进第二轮：soft pin 候选
  - **两轮排序依据都是 `lease_timeout`（租约到期早晚 ≈ 最近被读时间）**，不是 LRU 计数、也不是 soft pin 剩余 TTL
    - soft pin TTL 的作用仅是「pin 还有效吗」——决定进哪一轮，不作排序依据
    - kvcm 同周期 touch 的 key 剩余 TTL 趋同，本就无排序区分度
- **F11 一次 touch 同续两个时钟**（`GrantLease`，`master_service.h:938` @0.3.11）
  - `lease_timeout` 续到 `now + default_kv_lease_ttl`；若 key 已有 soft pin，`soft_pin_timeout` 续到 `now + default_kv_soft_pin_ttl`（max 语义，只延不缩）
  - 配置值
    - 0.3.11：两个 TTL 均为 master 必填配置（`master_config.h:119-120`）；测试床配 `default_kv_lease_ttl=11s`（`start_master.sh:34`），soft pin TTL 未显式配置
    - main：soft pin TTL 默认 30min（`types.h:86`）；lease TTL 仍必填
  - `ExistKey` / `BatchExistKey` 顺带续租（`grant_lease=true`）；`ProbeKey` 不续（纯探测：结果为 true 只说「调用那一刻在」，`master_service.h:268` @main）——kvcm 的 touch 用前者；**`ProbeKey` 为 main 新增、0.3.11 不含**
- **F12 lease 时长是全局读租约，不是 kvcm 私有参数**
  - 调大（如 5min）的连锁反应：任何 key 被读一次 → 窗口内不可逐 → 驱逐候选池缩小；极端（高流量池）候选不足时，压力集中到无 lease 的 key——**新 put 的 key 恰好出生无租约（背景概念），首当其冲**
  - soft pin 的设计意义正是解耦：lease 保持短（驱逐候选充足）+ pin 给少数 key 定向长保护

### UUID 与客户端接口

- **F8 kvcm 拿不到引擎的 client UUID**
  - UUID 在客户端进程构造时随机生成（`client_service.cpp:431`），无法按实例名推算
  - C++ 客户端有公开 `getClientId()`（`client_service.h:67`），但 **Python 绑定 0.3.11 与 main 都未暴露**——vLLM 走 Python，读不到自己的 UUID
  - master 的 RPC 没有「key → 属主 UUID」查询
    - `GetReplicaList` 返回副本描述符（副本的位置信息），内存副本的只有 seg 名和地址，不含 client_id（`replica.h:240`）
    - master 内部有现成的「seg 名 → client UUID」查询 `GetClientIdBySegmentName`（CreateCopyTask 自己就在用），但没暴露成 RPC
  - kvcm 与引擎之间**没有直接通道**：kvcm 的视野来自 master 的 segment 信息（seg 名 = `IP:rpc_port`，见 [README.md](README.md)）；「端点 → 实例」的注册方向是 kvcm 注册给索引（backlog D4/E13），不是引擎注册给 kvcm
- **F9 空写的发送通道**
  - master 协议层：`UpsertStart` / `UpsertEnd` 是公开 RPC（`master_service.h:331/:338` @0.3.11）
  - Python 绑定：只有「带数据、一次完成」的一体化接口 `upsert_tensor`，没有 start/end 分开调的接口——空写从 Python 发不出来
  - kvcm 要实现空写
    - 用编译型语言客户端库（C++/Go/Rust）或直接按协议发 master RPC → 不用改 mooncake
    - 继续用 Python → 需要给绑定层打补丁（改 mooncake）

## §1 操作集框架（已闭环）

- **七项原子操作**：copy / move / keepalive / pin / demote / retain / remove
  - 复合操作由原子操作组合而成，不写成一体状态机
  - 理由：kvcm 定位是通用 KV 管理控制面，不只是为预热一个场景服务

| 复合操作 | 组合 |
|---|---|
| prefetch（预热） | copy + 核验 + 保活注册 |
| 软释放 | 停 keepalive（有读者在用时租约会自动续上，动不了在用的数据） |
| 下线迁移 | copy + remove（或 move 一步完成） |

- copy 与 move 的分工
  - **copy 保副本数**：新增副本，源由 master 随机选（F7）
  - **move 减副本数/搬迁**：指定源→目标，完成后源删（F7）
  - 用途：下线迁移、副本归拢用 move
- **防驱逐相关操作的语义界定**（keepalive / pin / retain 的区别）
  - **keepalive**（动作）：周期 touch 续 lease（F11）——窗口 = lease_ttl、轻、高频；kvcm 断线，lease 过期即失效
  - **pin**（元数据标记）：设一次长期有效——soft = 驱逐排最后 + TTL 窗口；hard = 绝对不可逐
  - **retain**（目的，不是机制）：「属主下线/长期未命中时副本继续活着」——手段 = keepalive + pin + 属主方案（§2）
  - touch 哪些 key、何时开始、周期多少 = **kvcm 策略层逻辑**（保活注册表驱动），不是原子操作语义的一部分
- **状态记录四类**（按可丢性分级）
  1. **任务状态机**（可丢）：在途任务进度；丢了可重发——master 侧操作幂等（重复 copy 同 key 同目标不产生重复副本）
  2. **保活注册表**（不可丢）：哪些 key 在保活循环里；丢了就停止保活，等效提前释放
  3. **副本台账**（半可丢）：「前缀 × 目标实例」分布；丢了可从 master 查询重建，代价一轮全量对账
  4. **会话绑定**（不可丢）：key 与会话的归属；丢了不知何时停保活——保活永不停 = 事实永久 pin
- **部署形态**
  - kvcm 单实例跟随 mooncake master：master 单实例则 kvcm 单实例；master 将来多实例时再议 kvcm 的 HA 与共享存储
  - 状态存 **sqlite**（零新组件；上 HA 时再换 Redis/etcd）

## §2 pin（候选已摆平，待裁）

### 要解决的问题

- kvcm 预热的副本在池中无任何驱逐保护（D1 F5），池满被 LRU 逐掉则预热白做
- pin 操作 = kvcm 把指定 key 保护起来，不被驱逐

### 被否决的候选

- **不做，靠驱逐兜底**
  - kvcm 只 pin 自己预热的少数 key，pin 与未 pin 的差异正是优先级语义——不能没有
- **引擎写入时源头带 pin**
  - 侵入改引擎；且引擎不知道哪些该 pin，全 pin 则第一轮驱逐无候选、直接进第二轮——pin 失去区分度 = 没有 pin

### soft 需求（防驱逐，可接受极端情况被逐）：三个候选

| | ① lease 调大 + keepalive | ② 空写 upsert | ③ 读锁 |
|---|---|---|---|
| 做法 | master 配置 `default_kv_lease_ttl` 调大（如 5min）+ kvcm 周期 touch（F11） | `UpsertStart`(key, 原尺寸, with_soft_pin=±) → 不写数据 → `UpsertEnd`；原尺寸由 `GetReplicaList` 查 | kvcm 对 key 的一个副本持续持有读引用（refcnt>0 挡驱逐，F10） |
| mooncake 代码 | **不改** | 不改（前提：空写通道不落 Python，F9） | **不改** |
| 属主问题 | **无**（不碰写路径） | **有**（F3①，见下） | **无** |
| 保护强度 | lease 期内**绝对不可逐**（比 soft pin 强） | 驱逐排最后，极端可逐 | 绝对不可逐 |
| 断线窗口 | = lease_ttl（分钟级） | = soft pin TTL（30min 级） | **零**——断线即失效 |
| 持续开销 | 每 key 每窗口一次 touch | 同左（频率可更低） | 每 key 一个永续读会话（还要续） |
| 副作用 | 全局读租约变长（F12：驱逐候选缩小，调参项，需真机观察定值） | 两 RPC 间 key 不可读（毫秒级） | key 被读锁死成只读（写/upsert/Remove 全拒）；只保护被锁的那一个副本，其余被逐需 copy 补齐 |

- ① 是当前最干净的候选：零改动、零属主问题、保护更强；核心代价是全局 lease 影响（F12）
- ③ 可行但脆，作保底

### hard 需求（绝对保护，kvcm 长期失联也安全）：两个候选

- **读删写**：读出数据 → `Remove` → 带 hard_pin 重新 put → 校验副本
  - kvcm 本地持有数据直到校验成功，中途失败可重试；只有 kvcm 本地也丢才真丢
  - 属主问题：Remove 后重建走 Case A 新建，PutStart 传谁 UUID 谁是属主——与空写共用下方两方案
- **读锁长期持有** = 事实 hard pin（脆，同 ③ 的断线即失效）

### 属主问题：两个并列方案（空写与读删写重建共同面对）

- 起因：upsert/put 必然把属主改成调用方（F3①）

| | 方案一：属主改归 kvcm | 方案二：不改属主（借名） |
|---|---|---|
| 做法 | kvcm 传自己的 UUID（自己就是 client，天然持有；sqlite 持久化，重启同 id 重注册） | kvcm 先查得原属主 UUID，调用时原样传回（F4） |
| mooncake 代码 | **不改**（前提：空写通道不落 Python，F9） | **要改**：master 加「key → 属主 UUID」查询 RPC（数据内部现成，纯暴露，F8） |
| 属主语义 | **改变**：引擎下线后 key 不再级联删除（正是 retain 想要的收益）；代价 = kvcm 下线 → 名下 key 全删（新单点；缓解 = UUID 持久化 + 重启快于 master 心跳超时） | **不变**：引擎下线 key 删（维持现状语义） |
| 引擎配合 | 无 | 无（补丁打在 master，引擎零感知） |

- **决策条件（外部依赖，非单方可定）**
  1. mooncake 代码可改性——多人合作的开源项目，不是单方说了算
     - 上游提 PR（纯暴露只读接口，接受度预期高，节奏不可控）或 fork 维护本地 patch——是**独立成本**；升级拿 publisher 是官方功能零 patch，与此无关
  2. kvcm 实现语言（backlog E10/E11 未定）
     - 若落 Python，空写通道也需绑定补丁 → 方案一同样改 mooncake
- 属主语义若要变，与 retain 议题（D3 耦合）统一评估，不在 pin 单独定
- **注意**：若 soft 需求选 ① 或 ③，属主问题只剩读删写（hard 需求）一处触发，权重下降

## §3 demote（设计完成）

### 语义与边界

- **触发**：会话结束（或策略层判定 KV 变冷）→ kvcm 把该会话的 KV 从 DRAM 降层到 SSD 保存，**不删**
- **回来路径**：会话回来时从盘读回；池配 `promotion_on_hit` 可由池自动把热的盘副本提回 DRAM
- **边界**：demote 保的是「会话间歇」场景（引擎实例还活着）；引擎下线后 key 是否级联删除（含 disk 副本）归 retain / D3，不在本节
- demote 在操作集中是**复合操作**（停 keepalive + 落 SSD），不是新的原子机制

### 落 SSD 的两条路径

| | 路径 A：主动登记 | 路径 B：池配置委托 |
|---|---|---|
| 做法 | kvcm 经 client 库 `MountLocalDiskSegment` 挂本地 SSD；对每把 key：读 DRAM 副本 → 写本地盘 → `AddReplica` 向 master 登记 disk 副本（`master_service.cpp:5386` @main；0.3.11 同接口，F1）→ 停 keepalive | 池开 `enable_offload` + `offload_on_evict`（默认 false，`master_config.h:195` @main；0.3.11 是否含 `offload_on_evict` 待 E6 核对）；kvcm 只停 keepalive |
| 降层时机 | **立即**——会话一结束 DRAM 即释放 | **驱逐时**——池满选中该 key 时，master 塞进 offload 队列 → 挂盘 client 心跳（`OffloadObjectHeartbeat`，`master_service.cpp:8509` @main）领任务写盘 → `NotifyOffloadSuccess` 报备登记 |
| mooncake 代码 | 不改 | 不改（纯配置） |
| 数据搬运 | kvcm 承担（读 + 写盘带宽） | 挂盘 client 承担（kvcm 零搬运） |
| 池不满时 | DRAM 照样释放（主动路径的代价） | key 留 DRAM——会话回来直接 DRAM 命中，比降层更优（懒降层） |
| 兜底 | —— | offload 队列超每周期上限（`offloading_queue_limit × offload_cap_ratio`）时，`offload_force_evict` 决定丢弃还是留内存 |

- **默认 B**：零搬运零改动，DRAM 无压力时白赚命中
- **A 作「立即释放 DRAM」档**：DRAM 金贵 / 会话长期不回时由策略层选用；两条可并存
- DRAM 副本失保护后随驱逐释放；key 因 disk 副本存活——两路径殊途同归

### READY 与落盘的关系

- **READY 不含落盘**：预热 READY = DRAM 副本就位即上报（D1 口径不变）
- 落盘是降层/后台路径，与 READY 判定无关——若以落盘为 READY，等待落盘的时间会抵消预热收益

## §4 retain / release（待议）

- 现状描述在 [backlog.md](backlog.md) D2
- retain 的已有输入
  - 与属主语义（§2 两方案）、D3 副本生命周期直接耦合
  - `AddReplicaForRetainedClient`（master 为「保留副本的客户端」补登记副本的接口，`master_service.cpp:5400` @main）为 main 独有、0.3.11 不含（E6 已核）→ 候选机制，届时评估
