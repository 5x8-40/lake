# D2: KV 调度操作集

- 本文解决什么问题
  - kvcm（独立 KV 管理控制面）需要对池里的 KV 做调度操作（复制、防驱逐、降层、保留、释放）
  - 本文定这些操作的语义、实现机制、失败处理
- 状态: **讨论中**
  - 操作集框架已闭环（§1）
  - pin 机制方案已收敛、属主处理两方案并列待裁（§2）
  - demote / retain / release 待议（[backlog.md](backlog.md) D2）
- 上游问题: backlog D2——独立控制面进程对 KV 的完整操作面，每项定语义、与池驱逐策略的优先级、失败处理
- 版本锚点（mooncake 两个版本并存引用，文中标注 `@版本`）
  - **0.3.11.post1**：测试床实际部署的 wheel
  - **main @ 1fc27b62**：submodule 源码，代表升级后获得的能力

## 参考实现

| 参考 | 锚点 | 借什么 | 关键差异 |
|------|------|--------|----------|
| Mooncake master 写路径 | mooncake 仓内 `master_service.cpp::UpsertStart`（@0.3.11） | 已有 key 事后加/解 soft pin 的唯一现成通道：同尺寸 upsert 会顺带设置/清除 soft pin（F3） | 该路径为「覆盖写」设计；本文的「空写」（声明更新但不写数据，只为触发元数据变更）是复用其副作用，非官方用法 |
| lake 池冻结语义 | [`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「引用数 >0 冻结」 | 「被引用的块不可驱逐」的同义设计 | lake 池自长 radix 自带引用计数；Mooncake 无内容寻址，pin 只能借写路径元数据 |

## 事实基线（代码核实）

### 背景概念

- **key 与副本**：池里一把 key 存一份 KV 数据；同一 key 可有多个副本（replica），落在不同机器的 segment（seg，一段注册进池的 DRAM）上
- **副本两个状态**：写中（PROCESSING，不可读）与就绪（COMPLETE，可读）
- **属主（owner）**：每把 key 的元数据里记录 `client_id` = 最初写入它的客户端 UUID；每个 mooncake 客户端进程有一个 UUID
- **属主管辖范围**（F6 详述）：只影响「属主下线时 key 是否级联删除」和「谁能撤销未完成的写」，与复制、读取无关

### pin 机制

- **F1 pin 的两种形态**
  - **soft pin** = 限时驱逐保护
    - 到期自动失效，需要保活续期才能长期有效
    - 保护不是绝对的：master 驱逐时分两轮——第一轮只从无 soft pin 的 key 里挑受害者；第一轮凑不够要释放的空间、且配置 `allow_evict_soft_pinned_objects`（默认 true）允许时，第二轮才逐有 soft pin 的 key。所以 soft pin 的实际语义是「最后才被逐」，不是「不逐」
  - **hard pin** = 不可驱逐（绝对保护，无到期概念）
  - 版本差异（设置方式）
    - 0.3.11：写请求配置 `ReplicateConfig.with_soft_pin / with_hard_pin` 两个布尔开关（`replica.h:84-85`）；soft pin 时长取全局配置 `default_kv_soft_pin_ttl`
    - main：soft pin 升级为**三态** `SoftPinAction`（PRESERVE 保持现状 / ENABLE 开启 / DISABLE 关闭，`replica.h:64-68` @main）+ 可单独指定 `soft_pin_ttl_ms`（默认 30min、上限 24h，`types.h:85-89` @main）——三态让「这次写入不动已有 pin」成为显式选项；无新增 RPC 入口
  - 可达性（关键约束）
    - 新建 key：写入时两种 pin 都可带
    - 已有 key：只能改 soft pin（经 upsert，F3）；hard pin 在创建时固化、之后改不了（`master_service.cpp:1737` 注释 @0.3.11，原话「upsert does not change the eviction protection level」）
  - `CreateCopyTask` 不接受写请求配置（两版本同）→ kvcm 的复制路径带不了 pin，pin 必须事后单独加
- **F2 UpsertStart 的三种情形**（`master_service.cpp:1564` @0.3.11）
  - master 按「key 是否存在、尺寸是否一致」分三条路径
    - Case A（key 不存在）：按新 key 分配 buffer
    - Case B（存在且尺寸一致）：**原位更新**——复用原有 buffer 地址，不重新分配；pin 相关行为都发生在这条路径（F3）
    - Case C（存在但尺寸不同）：丢弃旧副本、按新尺寸重分配；旧 pin 设置保留/合并到新元数据
  - 拒绝条件
    - 副本正被读取 → `OBJECT_REPLICA_BUSY`（改写中的 buffer 会破坏正在进行的 RDMA 读）
    - 该 key 有 copy/offload 任务在途 → `OBJECT_HAS_REPLICATION_TASK`
  - 参数校验在分支判断之前：要求副本数等配置合法（`replica_num≥1`）——空写虽然不走分配，config 也须填合法值
- **F3 Case B 的四个行为**（`:1698-1737` @0.3.11）
  1. **改写属主**：`metadata.client_id = client_id` 无条件执行——「谁最后写，谁是属主」是 upsert 的固有语义，真实写数据与空写都一样触发
  2. **设置/清除 soft pin**：按调用方 config 的 `with_soft_pin`——带 true 且当前无 pin → 授予；带 false 且当前有 pin → 清除
  3. **hard pin 不动**（见 F1 可达性）
  4. **副本标为写中**：COMPLETE→PROCESSING，该 key 对所有读者不可见，直到 UpsertEnd 标回——空写的「不可读窗口」就来自这里
- **F4 UpsertStart 不验身份**
  - 对调用方 client_id 无任何校验（不要求等于原属主、不要求活跃注册，只查参数合法性）
  - 推论：kvcm 调用时把**原属主的 UUID 原样传回**，行为 1 就把属主赋回它自己 = 属主实际不变，pin 照加（「借名」方案的机制基础）
  - 一个例外条件：upsert 开头 master 先做一轮失效清理（Step 0 `CleanupStaleHandles`）——若该 key 的副本全部挂在已死客户端的 seg 上，元数据被当作垃圾清掉，请求落入 Case A 新建。所以借名 upsert 要求原属主当时在线（预热场景下成立）
- **F5 Revoke = 写中止**
  - `UpsertEnd / UpsertRevoke` 直通 `PutEnd / PutRevoke`（`:1770` 附近 @0.3.11）
  - 用途：PutStart/UpsertStart 分配了 buffer、副本标为写中之后，客户端写失败或放弃 → 调 Revoke 把写中的副本释放掉
  - 约束
    - 校验属主：非属主调用 → `ILLEGAL_CLIENT`（只有发起写的人能中止）
    - 只撤写中副本；副本已就绪（COMPLETE）则拒绝 → `INVALID_WRITE`
  - 与 kvcm 的关系：空写若在 Start/End 之间崩溃，副本卡在写中状态——恢复时扫描任务记录，补发 End 或 Revoke 清理（归 §1 状态记录①的崩溃恢复）

### 属主字段管辖范围

- **F6 属主管辖两件事，其余一概不管**
  1. **级联删除**：属主进程心跳超时被 master 判定下线后，它写入的 key 连同所有副本（包括 kvcm 预热到其他机器上的）一起被元数据级删除（D1 F5，实测 exist 0/32）
  2. **写中止权限**：Revoke 校验属主身份（F5）
- **不管**：copy / move / get / exist——复制与读取路径完全不读属主字段（F7 代码核实）
  - 推论：属主改成谁，都不影响预热复制的照常执行

### copy / move 机制

- **F7 CreateCopyTask**（`master_service.cpp:5777` @0.3.11）
  - 入参：key + 目标 seg 名列表；**调用方不能指定从哪个副本复制**
  - 源选择：master 从该 key 现有副本所在的 seg 里**随机挑一个**（`:5816`）
  - 执行模型：master 不搬数据，只生成任务记录；任务派给「源 seg 所属的客户端进程」（由它从本地读出、写到目标 seg）——所以复制要求源属主在线
  - 校验：key 存在、目标 seg 已挂载且可分配
- **CreateMoveTask**（key, source, target，`:5845`）
  - 唯一能指定源的路径；语义是**移动**——完成后源副本删除

### UUID 与客户端接口

- **F8 kvcm 拿不到引擎的 client UUID**
  - UUID 在客户端进程构造时随机生成（`client_service.cpp:431`），无法按实例名推算
  - C++ 客户端有公开 `getClientId()`（`client_service.h:67`），但 **Python 绑定 0.3.11 与 main 都未暴露**——vLLM 走 Python，读不到自己的 UUID
  - master 的 RPC 没有「key → 属主 UUID」查询
    - `GetReplicaList` 返回副本描述符（descriptor：副本的位置信息），内存副本的描述符只有 seg 名和地址，不含 client_id（`replica.h:240`）
    - master 内部有现成的「seg 名 → client UUID」查询 `GetClientIdBySegmentName`（CreateCopyTask 自己就在用），但没暴露成 RPC
  - kvcm 与引擎之间**没有直接通道**：kvcm 的视野来自 master 的 segment 信息（seg 名 = `IP:rpc_port`，见 [README.md](README.md)）；「端点 → 实例」的注册方向是 kvcm 注册给索引（backlog D4/E13），不是引擎注册给 kvcm
  - 旁证：`AddReplica`（`master_service.cpp:1426` @0.3.11）是磁盘副本登记接口（客户端在本地盘存了数据后向 master 报备），只接受 LOCAL_DISK 类型、不带 pin——对内存副本加 pin 无帮助
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
- **状态记录四类**（按可丢性分级）
  1. **任务状态机**（可丢）：在途任务的进度记录；丢了可重发——master 侧操作幂等（重复 copy 同 key 同目标不产生重复副本）
  2. **保活注册表**（不可丢）：哪些 key 在保活循环里；丢了就停止保活，等效于提前释放
  3. **副本台账**（半可丢）：「前缀 × 目标实例」的副本分布记录；丢了可从 master 查询重建，代价是一轮全量对账
  4. **会话绑定**（不可丢）：key 与会话的归属关系；丢了就不知道何时该停保活——保活永远不停 = 事实上的永久 pin
- **部署形态**
  - kvcm 单实例跟随 mooncake master：master 单实例则 kvcm 单实例；master 将来多实例时再议 kvcm 的 HA 与共享存储
  - 状态存 **sqlite**（零新组件；上 HA 时再换 Redis/etcd）

## §2 pin（机制方案已收敛，属主两方案并列待裁）

### 要解决的问题

- kvcm 预热的副本在池中无任何驱逐保护（D1 F5）：池满时被 LRU 逐掉，预热白做
- pin 操作 = kvcm 把指定 key 保护起来，不被驱逐

### 被否决的候选

- **不做，靠驱逐兜底**
  - 否决理由：kvcm 只会 pin 自己预热的少数 key，pin 与未 pin 的差异正是优先级语义——不能没有
- **引擎写入时源头带 pin**
  - 否决理由：侵入改引擎；且引擎不知道哪些 key 该保护，若全 pin 则等于没有 pin
- **读出→删除→重写作为唯一途径**
  - 否决理由：对 soft pin 代价过重（数据白过一趟）；但它恰好是 hard pin 的正确答案（见下）

### 确立结论

- pin 由 **kvcm 自主实现，soft / hard 都支持**（kvcm 是管理面定位；预热只是其中一个场景）

| 操作 | 机制 | 数据流 | 代价 |
|---|---|---|---|
| soft pin 加/解 | 空写：`UpsertStart`(key, 原尺寸, with_soft_pin=true/false) → **一个字节都不写** → `UpsertEnd`；原尺寸由 `GetReplicaList` 先查 | 零 | 两个 RPC 之间该 key 不可读（副本被标写中，F3④），毫秒级 |
| hard pin 加/解 | 读出数据 → `Remove` 删 key → 带 hard_pin 重新 put → 校验副本数 | 全量数据过 kvcm 一趟 | kvcm 本地持有数据直到校验成功，中途失败可重试；只有 kvcm 本地也丢才真丢 |

- 保活与 pin 的协同
  - 读路径每次访问会续租约；若 key 已有 soft pin，续租时 pin 的到期时间一并续（`GrantLease`，`master_service.h:938` @0.3.11）
  - 所以保活循环顺带维持 pin 不过期；停止保活，pin 随 TTL 自然失效——soft pin 的「解」可以不主动发，靠自然到期

### 属主问题：两个并列方案

- 起因：空写必然触发 F3①——属主被无条件改成调用方（kvcm）。要不要接受这个改变，分出两个方案

| | 方案一：属主改归 kvcm | 方案二：不改属主（借名） |
|---|---|---|
| 做法 | kvcm 传自己的 UUID（sqlite 持久化，重启后用同一 UUID 重新注册） | kvcm 先查得原属主 UUID，调用时原样传回（F4） |
| mooncake 代码 | **不改**（前提：空写通道不落 Python，F9） | **要改**：master 加一个「key → 属主 UUID」查询 RPC（数据内部现成，纯暴露，F8） |
| 属主语义 | **改变**：引擎下线后 key 不再级联删除（正是 retain 想要的收益）；代价是 kvcm 下线 → 它名下 key 全删（新单点；缓解 = UUID 持久化 + 重启快于 master 心跳超时） | **不变**：引擎下线 key 删（维持现状语义） |
| 引擎配合 | 无 | 无（补丁打在 master，引擎零感知） |

- **决策条件（外部依赖，非单方可定）**
  1. mooncake 代码可改性——多人合作的开源项目，不是单方说了算
     - 上游提 PR：纯暴露只读接口，接受度预期高，但合并节奏不可控
     - fork 维护本地 patch：升级为拿 publisher 本来就要走这条路（backlog E8），多一个 patch 是边际成本
  2. kvcm 实现语言（backlog E10/E11 未定）
     - 若落 Python，方案一也需要绑定补丁 → 两个方案都要改 mooncake，差异消失
- 属主语义若要变，与 retain 议题（和 D3 副本生命周期耦合）统一评估，不在 pin 这里单独定

## §3 demote / retain / release

- 待议，现状描述在 [backlog.md](backlog.md) D2
- retain 的已有输入
  - 与属主语义（§2 两方案）、D3 副本生命周期直接耦合
  - `AddReplicaForRetainedClient`（master 为「保留副本的客户端」补登记副本的接口）为 main 独有、0.3.11 不含（E6 已核）→ 候选机制，届时评估
