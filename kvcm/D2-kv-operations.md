# D2: KV 调度操作集

- 本文解决什么问题
  - kvcm（独立 KV 管理控制面）需要对池里的 KV 做调度操作（复制、防驱逐、降层、保留、释放）
  - 本文定这些操作的语义、实现机制、失败处理
- 状态：**设计完成**
  - §1 操作集框架：闭环，含七项操作的业界逐项对照与完备性审计
  - §2 pin：候选已摆平；落地裁决依赖外部条件（mooncake 可改性、kvcm 语言 E10/E11），不阻塞
  - §3 demote：设计完成
  - §4 retain / release：定型；retain 的保底手段（demote）确定，补充手段（改属主 / move）待 E5 实验裁决（F6b）
  - 挂账：copy 快照语义（backlog P3）、prefetch 终止（backlog P9）、级联删除口径矛盾（F6b → E5）
- 上游问题：backlog D2——独立控制面进程对 KV 的完整操作面，每项定语义、与池驱逐策略的优先级、失败处理
- 版本锚点（mooncake 两个版本并存引用，文中标注 `@版本`）
  - **0.3.11.post1**：测试床实际部署的 wheel
  - **main @ 1fc27b62**：submodule 源码，代表升级后获得的能力

## 参考实现

| 参考 | 锚点 | 借什么 | 关键差异 |
|------|------|--------|----------|
| Mooncake master 写路径 | mooncake 仓内 `master_service.cpp::UpsertStart`（@0.3.11） | 已有 key 事后加/解 soft pin 的唯一现成通道：同尺寸 upsert 会顺带设置/清除 soft pin（F3） | 该路径为「覆盖写」设计；本文的「空写」（声明更新但不写数据，只为触发元数据变更）是复用其副作用，非官方用法 |
| LMCache 后端接口 + controller | `lmcache/v1/storage_backend/storage_backend_interface.py`（七方法）+ `lmcache/v1/cache_controller`（lookup/pin/move/clear） | 与 kvcm 七项操作一一对应，操作集合理性的直接证据 | LMCache 无全局强一致元数据（controller 是协调非权威）；kvcm 有 master 权威视图 |
| KVCR | `src/kvcr/remote_fw_dram.py`（hint/query/fetch 时序）+ Guard 组件 | 传输窗口 pin（`claim/pin`→`release_pin`）；Guard 旁挂进程持池（retain 对照） | KVCR 目的地发起 + 派生索引 hint；kvcm 集中式 + master 权威视图 |

- 七项操作的逐项业界对照见 §1 末；各产品降层机制对照见 §3；mooncake 机制锚点散布在事实基线各 F 条目

## 事实基线（代码核实）

### 背景概念

- **key 与副本**：池里一把 key 存一份 KV 数据；同一 key 可有多个副本（replica），落在不同机器的 segment（seg，一段注册进池的 DRAM）上
- **副本两个状态**：写中（PROCESSING，不可读）与就绪（COMPLETE，可读）
- **属主（owner）**：每把 key 的元数据里记录 `client_id` = 写入它的客户端 UUID
- **lease（读租约）**
  - 读路径（`GetReplicaList` / `ExistKey`）授予，lease 期内该 key **绝对不可逐**（驱逐硬过滤直接跳过，F8）
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

### 属主字段与级联删除

- **F6 属主字段代码上只管两件事**（全库 `metadata.client_id` 仅三处：Revoke 校验 ×2、upsert 改写 ×1）
  1. **写中止权限**：Revoke 校验属主身份（F5）
  2. **upsert 改写**：「谁最后写谁是属主」（F3①）
  - **不管**：copy / move / get / exist——复制与读取路径完全不读属主字段（F7 代码核实）
- **F6b 级联删除：实测现象与代码核实不一致（归因归 E5）**
  - **实测**（D1 F5 / EXPERIMENTS #3）：属主 A 重启 → client TTL 过期 → key 记录连同全部副本记录（**含预热到 B seg 上的**）消失，`batch_is_exist` 0/32；现象疑似「副本存活性绑定属主 client」
  - **代码核实**（0.3.11.post1）：client 过期处理（`master_service.cpp:4930-5015`）= unmount 该 client 全部 seg + `ClearInvalidHandles` 全量扫——清理是**副本级**的（`CleanupStaleHandles :2530`）：内存副本随死 seg 清、disk 副本随死登记 client 清。**活 seg 上其他 client 的副本按代码不该被清**；全库无「按 key 属主删」路径
  - **不一致点**：实测时 B 的 seg 活着、B 副本 COMPLETE 可读（B 命中 88.2%），按代码应存活，实测却消失——待 E5 对照实验厘清（master 是否同机陪葬 / 副本实际状态 / 其他清理路径）
  - **设计含义**：两种口径下 retain 有效手段不同（§4）；**demote 落 kvcm SSD 在两口径下都成立**（数据物理离开死 seg + disk 副本登记 client = kvcm）

### copy / move 机制

- **F7 CreateCopyTask**（`master_service.cpp:5777` @0.3.11）
  - 入参：key + 目标 seg 名列表；**调用方不能指定从哪个副本复制**
  - 源选择：master 从该 key 现有副本所在的 seg 里**随机挑一个**（`:5816`）
  - 执行模型：master 不搬数据，只生成任务记录；任务派给「源 seg 所属的客户端进程」执行——复制要求源属主在线
  - 校验：key 存在、目标 seg 已挂载可分配
- **CreateMoveTask**（key, source, target，`:5845`）
  - 唯一能指定源的路径；语义是**移动**——完成后源副本删除
  - 执行模型与 copy 相同：任务派给源 seg 属主执行（`GetClientIdBySegmentName(source)`），同样要求源在线；失败则源保留

### lease 与驱逐

- **F8 驱逐算法的完整过滤与排序**（`BatchEvict`，`master_service.cpp:4467` 起 @0.3.11）
  - 硬过滤（两轮通用，满足任一即跳过）
    - hard pin → 永不候选
    - lease 未过期 → 跳过（lease 期内绝对安全）
    - 副本 refcnt>0（正被读引用）→ 该副本不可逐（per-副本字段 `get_refcnt()`）
  - 第一轮：无 soft pin 的候选；不够且 `allow_evict_soft_pinned_objects_` 时进第二轮：soft pin 候选
  - **两轮排序依据都是 `lease_timeout`（租约到期早晚 ≈ 最近被读时间）**，不是 LRU 计数、也不是 soft pin 剩余 TTL
    - soft pin TTL 的作用仅是「pin 还有效吗」——决定进哪一轮，不作排序依据
    - kvcm 同周期 touch 的 key 剩余 TTL 趋同，本就无排序区分度
- **F9 一次 touch 同续两个时钟**（`GrantLease`，`master_service.h:938` @0.3.11）
  - `lease_timeout` 续到 `now + default_kv_lease_ttl`；若 key 已有 soft pin，`soft_pin_timeout` 续到 `now + default_kv_soft_pin_ttl`（max 语义，只延不缩）
  - 配置值
    - 0.3.11：两个 TTL 均为 master 必填配置（`master_config.h:119-120`）；测试床配 `default_kv_lease_ttl=11s`（`start_master.sh:34`），soft pin TTL 未显式配置
    - main：soft pin TTL 默认 30min（`types.h:86`）；lease TTL 仍必填
  - `ExistKey` / `BatchExistKey` 顺带续租（`grant_lease=true`）；`ProbeKey` 不续（纯探测：结果为 true 只说「调用那一刻在」，`master_service.h:268` @main）——kvcm 的 touch 用前者；**`ProbeKey` 为 main 新增、0.3.11 不含**
- **F10 lease 时长是全局读租约，不是 kvcm 私有参数**
  - 调大（如 5min）的连锁反应：任何 key 被读一次 → 窗口内不可逐 → 驱逐候选池缩小；极端（高流量池）候选不足时，压力集中到无 lease 的 key——**新 put 的 key 恰好出生无租约（背景概念），首当其冲**
  - soft pin 的设计意义正是解耦：lease 保持短（驱逐候选充足）+ pin 给少数 key 定向长保护

### UUID 与客户端接口

- **F11 kvcm 拿不到引擎的 client UUID**
  - UUID 在客户端进程构造时随机生成（`client_service.cpp:431`），无法按实例名推算
  - C++ 客户端有公开 `getClientId()`（`client_service.h:67`），但 **Python 绑定 0.3.11 与 main 都未暴露**——vLLM 走 Python，读不到自己的 UUID
  - master 的 RPC 没有「key → 属主 UUID」查询
    - `GetReplicaList` 返回副本描述符（副本的位置信息），内存副本的只有 seg 名和地址，不含 client_id（`replica.h:240`）
    - master 内部有现成的「seg 名 → client UUID」查询 `GetClientIdBySegmentName`（CreateCopyTask 自己就在用），但没暴露成 RPC
  - kvcm 与引擎之间**没有直接通道**：kvcm 的视野来自 master 的 segment 信息（seg 名 = `IP:rpc_port`，见 [README.md](README.md)）；「端点 → 实例」的注册方向是 kvcm 注册给索引（backlog D4/E13），不是引擎注册给 kvcm
- **F12 空写的发送通道**
  - master 协议层：`UpsertStart` / `UpsertEnd` 是公开 RPC（`master_service.h:331/:338` @0.3.11）
  - Python 绑定：只有「带数据、一次完成」的一体化接口 `upsert_tensor`，没有 start/end 分开调的接口——空写从 Python 发不出来
  - kvcm 要实现空写
    - 用编译型语言客户端库（C++/Go/Rust）或直接按协议发 master RPC → 不用改 mooncake
    - 继续用 Python → 需要给绑定层打补丁（改 mooncake）

## §1 操作集框架

- **操作集七项**：copy / move / keepalive / pin / demote / retain / remove
  - 按性质分三类
    - **原子动作**：copy / move / keepalive / remove——单次调用完成的动作
    - **元数据标记**：pin——设一次长期有效的标记
    - **复合 / 目的**：demote（停 keepalive + 落 SSD 登记，§3）；retain（目的不是机制，由 demote / move / pin 组合实现，§4）
  - 复合操作由其他操作组合而成，不写成一体状态机
  - 理由：kvcm 定位是通用 KV 管理控制面，不只是为预热一个场景服务

| 复合操作 | 组合 |
|---|---|
| prefetch（预热） | copy + 核验 + 保活注册 |
| 软释放 | 停 keepalive（有读者在用时租约会自动续上，动不了在用的数据） |
| demote（降层） | 停 keepalive + 读 DRAM 副本 → 写 kvcm SSD → `AddReplica` 登记 + 核验（§3） |
| 下线迁移 | copy + remove（或 move 一步完成） |

- copy 与 move 的分工
  - **copy 保副本数**：新增副本，源由 master 随机选（F7）
  - **move 减副本数/搬迁**：指定源→目标，完成后源删（F7）
  - 用途：下线迁移、副本归拢用 move
- **防驱逐相关操作的语义界定**（keepalive / pin / retain 的区别）
  - **keepalive**（动作）：周期 touch 续 lease（F9）——窗口 = lease_ttl、轻、高频；kvcm 断线，lease 过期即失效
  - **pin**（元数据标记）：设一次长期有效——soft = 驱逐排最后 + TTL 窗口；hard = 绝对不可逐
  - **retain**（目的，不是机制）：「属主下线/长期未命中时副本继续活着」——手段组合见 §4
  - touch 哪些 key、何时开始、周期多少 = **kvcm 策略层逻辑**（保活注册表驱动），不是原子操作语义的一部分
- **状态记录四类**（按可丢性分级）
  1. **任务状态机**（可丢）：在途任务进度；丢了可重发——master 侧操作幂等（重复 copy 同 key 同目标不产生重复副本）
  2. **保活注册表**（不可丢）：哪些 key 在保活循环里；丢了就停止保活，等效提前释放
  3. **副本台账**（半可丢）：「前缀 × 目标实例」分布；丢了可从 master 查询重建，代价一轮全量对账
  4. **会话绑定**（不可丢）：key 与会话的归属；丢了不知何时停保活——保活永不停 = 事实永久 pin
- **部署形态**
  - kvcm 单实例跟随 mooncake master：master 单实例则 kvcm 单实例；master 将来多实例时再议 kvcm 的 HA 与共享存储
  - 状态存 **sqlite**（零新组件；上 HA 时再换 Redis/etcd）

### 参考实现对照（七项操作逐项）

| kvcm 操作 | 业界对应 | 异同 |
|---|---|---|
| copy | LMCache `P2PBackend` 跨实例直传 + controller 命令（lookup/pin/**move**/clear，`lmcache/v1/cache_controller`）；KVCR hint 驱动 P2P（**目的地发起**，源侧 `claim/pin` 配合）；llm-d P2P（索引只作 hint、握手兜底）；HiCache `prefetch_from_storage`（L3→L2→L1，三策略终止） | 命令面与数据面分离是各家共同形态。kvcm 的 `CreateCopyTask`（master 指派源端执行）与 LMCache controller 同为集中式；KVCR/llm-d 的「目的地发起」依赖派生索引 + 握手，kvcm 有 master 权威视图，集中式成立 |
| move | LMCache controller 命令集含 `move`；KVCR `deliver`（纯搬运层模式） | move 作控制面原语在多家存在；`CreateMoveTask` 指定源、删源，语义一致 |
| keepalive | LMCache 后端接口 `touch_cache`；FlexKV lease「保证传输期间块有效」；llm-d 推测索引 TTL 2s（`defaultSpeculativeTTL`，代码注释自承是经验值） | 周期 touch 续租在多家存在；llm-d 的「TTL 是经验值」提示 lease_ttl 须真机校准（F10 已记） |
| pin | LMCache `pin`/`unpin`（后端接口级）；KVCR `claim/pin`/`release_pin`（**传输窗口专用**，`FETCHABLE` 须等 pin 成功才确定）；TensorCast `pinned` policy（必须驻留 + 溢出**拒绝写入**）；HiCache `lock_ref`（`protected_size_` vs `evictable_size_`，dec 到 0 ≠ 释放、仍可命中） | mooncake 三件套（lease/soft/hard）比多数系统细。差异：TensorCast `pinned` 溢出拒绝写入，mooncake hard pin 无此语义（创建时固化、之后改不了，F1）；KVCR 的传输窗口 pin 在 mooncake 由「copy 任务在途拒 upsert」覆盖（F2） |
| demote | §3 专节（FlexKV/LMCache/UCM/KVBM/MemCache 对照） | —— |
| retain | KVCR-Guard（**旁挂进程持池** + fencing 隔离失效者 + backup KVCR 接管，引擎崩溃后缓存仍可供远端读）；TensorCast 所有权二分（System-Owned vs **Caller-Leased**：KV 是调用者租借）；LMCache daemon/远程后端；FlexKV REMOTE | 「副本归引擎外实体」是多家共同选择；kvcm 登记 disk 副本（§3）相当于 Guard 模式的 mooncake 版。TensorCast Caller-Leased 对应 kvcm 的定位：kvcm 对引擎 KV 是**租借管理权**（只加保护，不没收读写）。fencing 在 kvcm 不需要——属主管辖只有 Revoke（F5/F6），引擎下线后无人 Revoke |
| remove | LMCache `remove`/`clear`；SGLang 分层放弃（`block-lifecycle.md`）：无 L3 时 `evict` 删叶 = 彻底；**有 L3 时引擎永不主动放弃**——删除下放 L3 后端独立 LRU/TTL，引擎看不到 | SGLang 的「释放 = 逐层剥离」（L1 摘节点降层 → L2 摘除 → L3 后端自决）对应 kvcm 三档（软释放 / demote / remove）。「L3 删除引擎不可见」提醒：kvcm remove 后索引侧条目需同步清（D1 已知：池副本删除事件通道缺失） |

- **总观察**
  1. LMCache 后端七方法 + controller 命令集与 kvcm 七项几乎一一对应——操作集合理性的直接证据
  2. TensorCast policy 预设（`cache/durable/ha/cold/warm/pinned`，`tensorcast/api/_config.py::StorePolicyProfile`）是声明式放置契约：kvcm 的操作可视为给 key 改 policy（demote≈`cold`、pin≈`pinned`、默认≈`cache`）；API 形态（声明式 policy vs 命令式操作）留待 E11 协议设计时参考
  3. SGLang「释放是逐层剥离不是单动作」——kvcm 软释放/demote/remove 三档粒度与之相同

### 操作集完备性审计（双向对照）

- **别人有、kvcm 没有的**

| 操作 | 出处 | 评估 |
|---|---|---|
| **prefetch 终止/取消** | HiCache `can_terminate_prefetch` / `mark_terminate`（三策略：best_effort / wait_complete / timeout） | **真缺口**：请求改道 / 源消失时 copy 白耗带宽只能等自生自灭；且 mooncake 0.3.11 只有 `CancelDrainJob`，**copy/move 任务无取消 RPC**——补法：池侧扩展（改 mooncake）或「弃置 + 事后清副本」（kvcm 侧可做的弱版）。已登记 backlog P9 |
| compress（KV 压缩） | LMCache controller 命令集 | 远期能力，当前不补 |
| health / full_sync（对账） | LMCache controller 命令集 | 半缺口：状态记录③有「全量对账重建台账」概念但未操作化——应列为运维操作 |
| 在途状态查询 | KVCR `query` 四态（HIT / **FETCHING** / FETCHABLE / MISS） | 半缺口：kvcm 只有 exist（在/不在）；「预热中」状态存于 sqlite 任务状态机，是否暴露给索引归 D4 接口设计 |

- **kvcm 有、别人不做的**

| 操作 | 别人为什么不做 | kvcm 为什么要 |
|---|---|---|
| pin 显式标记 | UCM/FlexKV 交给后端 LRU/TTL；SGLang 有 L3 时引擎**永不主动放弃**（删除下放后端容量管理） | 预热副本**没有读者**，「读即续租」保不住——预热场景特有 |
| keepalive 独立保活 | 多数系统靠读路径被动续租，无控制面保活 | 同上（LMCache `touch_cache` 是同类例外） |
| move 显式搬迁 | FlexKV/UCM/HiCache 层间移动是驱逐/prefetch 的副作用，无显式 move | 下线迁移/副本归拢要确定性搬迁（LMCache controller 有 move，不孤立） |
| retain 属主下线存续 | 引擎插件层（FlexKV/UCM/HiCache L1L2）引擎死缓存失效拉倒；KVCR-Guard/LMCache daemon 才做 | kvcm 是独立控制面：预热投入要跨实例存亡保住 |

- **审计结论**：不做 pin/keepalive/retain 的系统，共同点是有读者替它们保活、或引擎死了认栽——kvcm 的预热副本恰好「无读者 + 要跨实例存亡」，这三样是定位需要，不是冗余。**唯一真缺口 = prefetch 终止**（backlog P9）

## §2 pin

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
| 做法 | master 配置 `default_kv_lease_ttl` 调大（如 5min）+ kvcm 周期 touch（F9） | `UpsertStart`(key, 原尺寸, with_soft_pin=±) → 不写数据 → `UpsertEnd`；原尺寸由 `GetReplicaList` 查 | kvcm 对 key 的一个副本持续持有读引用（refcnt>0 挡驱逐，F8） |
| mooncake 代码 | **不改** | 不改（前提：空写通道不落 Python，F12） | **不改** |
| 属主问题 | **无**（不碰写路径） | **有**（F3①，见下） | **无** |
| 保护强度 | lease 期内**绝对不可逐**（比 soft pin 强） | 驱逐排最后，极端可逐 | 绝对不可逐 |
| 断线窗口 | = lease_ttl（分钟级） | = soft pin TTL（30min 级） | **零**——断线即失效 |
| 持续开销 | 每 key 每窗口一次 touch | 同左（频率可更低） | 每 key 一个永续读会话（还要续） |
| 副作用 | 全局读租约变长（F10：驱逐候选缩小，调参项，需真机观察定值） | 两 RPC 间 key 不可读（毫秒级） | key 被读锁死成只读（写/upsert/Remove 全拒）；只保护被锁的那一个副本，其余被逐需 copy 补齐 |

- ① 是当前最干净的候选：零改动、零属主问题、保护更强；核心代价是全局 lease 影响（F10）
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
| mooncake 代码 | **不改**（前提：空写通道不落 Python，F12） | **要改**：master 加「key → 属主 UUID」查询 RPC（数据内部现成，纯暴露，F11） |
| 属主语义 | **改变**：引擎下线后 key 不再级联删除（retain 收益——**有效性依赖 E5 口径**：若级联按 key 属主删则成立；若按 seg 维度清理则 seg 失效照清副本、与 key 属主无关，见 F6b）；代价 = kvcm 下线 → 名下 key 全删（新单点；缓解 = UUID 持久化 + 重启快于 master 心跳超时） | **不变**：引擎下线 key 删（维持现状语义） |
| 引擎配合 | 无 | 无（补丁打在 master，引擎零感知） |

- **决策条件（外部依赖，非单方可定）**
  1. mooncake 代码可改性——多人合作的开源项目，不是单方说了算
     - 上游提 PR（纯暴露只读接口，接受度预期高，节奏不可控）或 fork 维护本地 patch——是**独立成本**；升级拿 publisher 是官方功能零 patch，与此无关
  2. kvcm 实现语言（backlog E10/E11 未定）
     - 若落 Python，空写通道也需绑定补丁 → 方案一同样改 mooncake
- 属主语义若要变，与 retain 议题（§4）统一评估，不在 pin 单独定
- **注意**：若 soft 需求选 ① 或 ③，属主问题只剩读删写（hard 需求）一处触发，权重下降

## §3 demote

### 语义

- **触发**：会话结束 / 策略层判定 KV 变冷 / **实例计划下线前保存**——凡是「DRAM 里的 KV 需要转到 SSD 保存」的场景 → kvcm 把目标 KV 从 DRAM 降层到自己的 SSD，**不删**
- **回来路径**：client 读优先级 MEMORY → LOCAL_DISK → DISK（`real_client.cpp:2523-2553` @0.3.11）；任何 client 经 disk descriptor 的 `transport_endpoint` 走 offload RPC **远程读**——引擎跨机器读 kvcm 盘上的副本是现成能力；`promotion_on_hit`（0.3.11 含）可由池把热的盘副本提回 DRAM
- demote 是复合操作（停 keepalive + 落 SSD 登记），动作全部由 kvcm 自己完成

### 各产品降层对照（详见 [../docs/research/hbm-tier-and-offload.md](../docs/research/hbm-tier-and-offload.md)）

| 产品 | 触发 | 搬运者 | 引擎死后副本 |
|---|---|---|---|
| FlexKV | **请求结束**（`request_finished`→D2H） | 卸载组件自己 | 本机失效；REMOTE（Mooncake store 等）保留 |
| LMCache | forward 内逐层 `save_kv_layer` | 自己（`from_gpu`） | daemon / 远程后端保留 |
| UCM | 策略调 `dump` | connector 自己 | store 保留 |
| Dynamo KVBM | `request_finished` settlement | OffloadEngine | G4 保留，G2/G3 随实例 |
| MemCache（昇腾） | put 时定介质 | LocalService | 池对象 Meta 保留 |

- 一致模式：**触发 = 请求/会话结束**（不是池压力）；**搬运 = 卸载组件自己搬**；**降层副本放引擎之外才活得过引擎**
- Mooncake `offload_on_evict`（驱逐时源引擎写自己盘、副本挂引擎名下）是**池自己的冷热分层**，不是会话生命周期管理——不作为 demote 的实现路径

### 操作构成（kvcm 管自己）

1. kvcm 经 client 库挂本地 SSD（`MountLocalDiskSegment` / `enable_ssd_offload` 初始化 FileStorage，0.3.11 含）
2. 触发后对该批 key 逐个执行：读 DRAM 副本 → 写 kvcm 本地盘 → `AddReplica` 向 master 登记 disk 副本（`master_service.cpp:1426` @0.3.11）→ 停 keepalive
3. **核验**：`AddReplica` 后 `GetReplicaList` 确认自己的 disk 副本在册——每 key 只有一个 LOCAL_DISK 槽，已有他人 disk 副本时登记**静默无效**（返回成功但什么都不做；0.3.11 与 main 同语义）
4. DRAM 副本失保护后随驱逐释放；key 因 kvcm 的 disk 副本存活

### 副本归属与 retain

- 级联删除按代码是**副本级**的（`CleanupStaleHandles`，`master_service.cpp:2530` @0.3.11）：内存副本随 seg 属主死清、**disk 副本随登记 client 死清**；全无效副本则 key 删。全库无「按 key 属主整删」逻辑——但与实测现象（B seg 副本也消失）不一致，归因归 E5（F6b）
- **kvcm 登记的 disk 副本在两口径下都安全**：代码口径下登记 client（kvcm）活着副本即活；实测口径（绑定 key 属主）下数据已物理离开死 seg、disk descriptor 里登记的是 kvcm
- 所以「属主下线副本存续」（retain 核心诉求）由 demote 保底覆盖，不依赖改属主
- 对偶：引擎自己 offload 的副本（池冷热路径）登记 client = 引擎，随引擎死清——池冷热不保障会话存续

### 与池 offload 的关系：独立并存

- 池 `offload_on_evict`（0.3.11 含）是池自己的事：驱逐时 master 把任务推给**源副本 seg 属主 client**（`PushOffloadingQueue`），只有源引擎能领；kvcm 无 DRAM seg 领不到任务，也不参与
- kvcm 不管、不依赖池该配置；两路同 key 相遇时先到先得——kvcm 的会话信号通常早于池压力，核验兜底（见上）
- 边界：本节 demote 保「会话间歇 + 引擎下线」；「池整体弃置某 key」仍归 release / 池驱逐

### READY 与落盘的关系

- **READY 不含落盘**：预热 READY = DRAM 副本就位即上报（D1 口径不变）
- 落盘是降层/后台路径，与 READY 判定无关——若以落盘为 READY，等待落盘的时间会抵消预热收益

## §4 retain / release

### retain（属主下线后副本存续）：机制定型

- **目标**：写入引擎（属主）下线后，它写的 KV 副本仍可用
- **保底 = demote**（两口径通吃，§3）：计划下线前把该实例的 KV 降层到 kvcm SSD——数据物理离开死 seg + disk 副本登记 client = kvcm。意外下线来不及 demote 的，依赖池内他机残存副本（按代码口径应存活——E5 验证）
- **补充手段（有效性依赖 E5 口径，F6b）**
  - **改属主**（§2 方案一）：仅当 E5 证实「按 key 属主删」才有效
  - **move/copy 到其他活 seg**：仅当 E5 证实「按 seg 清理」才有效（副本挪到活 seg 即脱离清理范围）
  - 两者不互斥，E5 裁决后启用对应手段；demote 保底始终成立
- **计划下线的编排入口**
  - `GracefulUnmountSegment`（`master_service.cpp:669` @0.3.11）：引擎自调，宽限期后正式 unmount——宽限期即 demote/move 的执行窗口
  - `CreateDrainJob`（main 独有）：master 把指定 seg 优雅排空到目标 seg（带并发上限），归 D3 评估
- **长期未命中副本的存续** = 策略层（保活注册表驱动），机制已齐（keepalive/pin/demote）
- `AddReplicaForRetainedClient`（main 独有，retaining guard 语义）：E5 后评估
- 与 D3 的边界：副本生命周期（TTL / READY 语义）归 D3；本节只定「属主下线」场景的机制组合

### release（主动释放）：定型

- **决策归属已闭环**（D1 §6）：router 提供会话生命周期线索 → 控制面决策执行 → 池驱逐兜底；三档 = 软释放（停保活）/ demote（降层）/ 主动 Remove（确认没人用）
- kvcm 是管理控制面，Remove 系 API **全部可用，按场景选用**（`master_service.h:440-464` @0.3.11）
  - `Remove(key)` / `BatchRemove(keys)`：按 key 精确删
  - `RemoveByRegex(pattern)`：模式批量删（前缀族 / 模型版本整体下线）
  - `RemoveAll()`：全池清空（运维重置）
  - `BatchReplicaClear(keys, client_id, seg)`：清指定 client 指定 seg 上的副本——实例下线后主动清残（`CleanupStaleHandles` 惰性清理的主动版）
  - 公共参数 `force`：跳过 lease 与复制任务检查——仅「确认没人用」（D1 §6 例外档）使用；否则等 lease 过期自然释放
- **收尾责任**：remove 后 kvcm 清自己台账 + 索引条目同步清（池副本删除事件通道缺失，D1 F10/F14；llm-d 幽灵条目同坑）
