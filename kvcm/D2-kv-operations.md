# D2: KV 调度操作集

- 状态: **讨论中**
  - 操作集框架已闭环（§1）
  - pin 机制方案已收敛、属主处理两方案并列待裁（§2）
  - demote / retain / release 待议（[backlog.md](backlog.md) D2）
- 上游问题: backlog D2——独立控制面进程对 KV 的完整操作面，每项定语义、与池驱逐策略的优先级、失败处理

## 参考实现

| 参考 | 锚点 | 借什么 | 关键差异 |
|------|------|--------|----------|
| Mooncake master 写路径 | mooncake 仓内 `master_service.cpp::UpsertStart`（@0.3.11） | 已有 key 事后加/解 soft pin 的唯一现成通道：同 size upsert 会 reconcile soft pin | 该路径为「覆盖写」设计；空写（声明更新但不写数据）是复用其元数据副作用，非官方用法 |
| lake 池冻结语义 | [`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「引用数 >0 冻结」 | 「被引用的块不可驱逐」的同义设计 | lake 池自长 radix 自带引用计数；Mooncake 无内容寻址，pin 只能借写路径元数据 |

- 版本锚点
  - **0.3.11.post1**（测试床 wheel）
  - **main @ 1fc27b62**（submodule）
  - 引用处标注 `@版本`

## 事实基线（代码核实）

### pin 机制

- **F1 pin 两形态与可达性**
  - **soft pin** = key 粒度限时驱逐保护
    - 到期自动失效
    - 驱逐两轮制排最后；`allow_evict_soft_pinned_objects` 默认 true = 极端情况仍可逐
  - **hard pin** = 不可驱逐
  - 版本差异
    - 0.3.11：`ReplicateConfig.with_soft_pin/with_hard_pin`（`replica.h:84-85`）；TTL 由 `default_kv_soft_pin_ttl` 配置
    - main：三态 `SoftPinAction` + `soft_pin_ttl_ms`（默认 30min / 上限 24h，`types.h:85-89`）；无新入口
  - 可达性
    - 新建 key：两种都可带
    - 已有 key：只能改 soft pin（F3）；hard pin 创建时固化（`master_service.cpp:1737` 注释 @0.3.11）
  - `CreateCopyTask` 无 config（两版本同）→ copy 路径带不了 pin
- **F2 UpsertStart 三分支**（`master_service.cpp:1564` @0.3.11）
  - Case A（key 不存在）= 新建分配
  - Case B（存在且同 size）= in-place 更新
  - Case C（不同 size）= 丢弃旧副本重分配，保留/合并 pin
  - 拒绝条件
    - 副本被读 → `OBJECT_REPLICA_BUSY`
    - copy/offload 任务在途 → `OBJECT_HAS_REPLICATION_TASK`
  - 参数校验在分支之前：空写 config 也须合法（`replica_num≥1` 等），虽不走分配
- **F3 Case B 四个行为**（`:1698-1737` @0.3.11）
  1. `metadata.client_id = client_id` **无条件**改写
     - 「谁最后写谁是属主」是 upsert 固有语义，真写空写一样
  2. reconcile soft pin（config 带 = 授 / 不带 = 解）
  3. hard pin 不动
  4. 副本 COMPLETE→PROCESSING，key 不可读直到 UpsertEnd
- **F4 UpsertStart 零身份校验**
  - 不验证 client_id 是否原属主、是否活跃注册（仅参数/config 合法性）
  - → kvcm 把原属主 UUID 原样传回 = 属主不变、pin 照加
  - 例外：Step 0 `CleanupStaleHandles`——副本全在死 client 的 seg 上时 key 被清理、走 Case A
    - 借名 upsert 要求原属主在线（预热场景成立）
- **F5 Revoke 语义**
  - `UpsertEnd/UpsertRevoke` 直通 `PutEnd/PutRevoke`（`:1770` 附近 @0.3.11）
  - PutRevoke = **写中止**
    - 校验属主（非属主 `ILLEGAL_CLIENT`）
    - 只撤 PROCESSING 副本（已有 COMPLETE → `INVALID_WRITE`）
  - 用途：kvcm 空写在 Start/End 间崩溃 → 补 End 或 Revoke 清理
    - 归任务状态机崩溃恢复（§1 状态记录①）

### 属主字段管辖范围

- **F6 属主管两件事**
  1. 属主 client 心跳超时下线 → key 级联删除（D1 F5，exist 0/32 实测）
  2. Revoke 权限校验（F5）
- **不管** copy / move / get / exist → 预热与复制不受属主变更影响

### copy / move 机制

- **F7 CreateCopyTask**（`master_service.cpp:5777` @0.3.11）
  - 入参：key + 目标 seg 列表（**源不可指定**）
  - 源：master 从现有副本 seg **随机选**（`:5816`）
  - 执行：任务派给源 seg 的属主 client
  - 校验：目标 seg 已挂载可分配
- **CreateMoveTask**（key, source, target，`:5845`）
  - 唯一指定源的路径；完成后源副本删除

### UUID 与客户端接口

- **F8 client UUID 不可推算、Python 不可读**
  - UUID：client 构造时随机生成（`client_service.cpp:431`）
  - C++：`getClientId()` 公开（`client_service.h:67`）
  - Python：0.3.11 与 main 绑定均**未暴露**
  - master RPC：无「key → 属主 UUID」查询
    - GetReplicaList 返回副本 descriptor；内存副本不含 client_id（`replica.h:240`）
    - 内部 `GetClientIdBySegmentName` 现成未暴露
  - kvcm ↔ 引擎**无直接通道**
    - kvcm 视野来自 master 的 segment 信息（seg 名 = `IP:rpc_port`，[README.md](README.md)）
    - 「端点 → 实例」注册是 kvcm 端点注册给索引（D4/E13），方向相反
  - `AddReplica`（`master_service.cpp:1426` @0.3.11）= LOCAL_DISK 副本登记，不带 pin、不加内存副本 → 对 pin 无帮助
- **F9 空写通道**
  - master RPC 层：`UpsertStart/UpsertEnd` 公开（`master_service.h:331/:338` @0.3.11）
  - Python 绑定：只有一体化 `upsert_tensor`（必须带数据）
  - kvcm 空写实现
    - 编译型 client 库 / 自实现 RPC → 不改 mooncake
    - 继续 Python → 需绑定补丁（改 mooncake）

## §1 操作集框架（已闭环）

- **七项原子操作**：copy / move / keepalive / pin / demote / retain / remove
  - 复合操作由原子组合，非状态机写法
  - kvcm 定位 = 通用 KV 管理控制面，不只是 prefetch

| 复合操作 | 组合 |
|---|---|
| prefetch（预热） | copy + 核验 + 保活注册 |
| 软释放 | 停 keepalive（有读自动续租，动不了在用的数据） |
| 下线迁移 | copy + remove（或 move 一步） |

- copy vs move 分工
  - **copy 保副本数**：新增副本，源随机
  - **move 减副本数/搬迁**：指定源→目标，完成后源删（F7）
  - 用途：下线迁移、副本归拢用 move
- **状态记录四类**
  1. 任务状态机（可丢，master 幂等）
  2. 保活注册表（不可丢）
  3. 副本台账（前缀 × 目标实例，半可丢）
  4. 会话绑定（不可丢——保活漏停 = 事实 pin）
- **部署形态**
  - kvcm 单实例跟随 mooncake master（master 单 → kvcm 单；master 多实例时再议 HA/共享存储）
  - 状态存 **sqlite**（HA 时换 Redis/etcd）

## §2 pin（机制方案已收敛，属主两方案并列待裁）

### 要解决的问题

- kvcm 预热副本无驱逐保护（D1 F5），池满被 LRU 逐掉则预热白做
- pin = kvcm 把指定 key 保护起来

### 被否决的候选

- **不做（驱逐兜底）**
  - kvcm 只 pin 自己预热的少数 key，正是优先级语义——不能没有
- **引擎源头带 pin**
  - 侵入改引擎；引擎不知道哪些该 pin，全 pin = 没有 pin
- **读删写作唯一途径**
  - 对 soft pin 太重（但对 hard pin 正好是答案，见下）

### 确立结论

- pin 归 **kvcm 自主实现，soft / hard 都支持**（管理面定位；预热只是一场景）

| 操作 | 机制 | 数据流 | 代价 |
|---|---|---|---|
| soft pin 加/解 | `UpsertStart`(key, 原 size, ±soft_pin) → **不写数据** → `UpsertEnd`（空写；size 由 GetReplicaList 查） | 零 | 两 RPC 间 key 不可读（副本标 PROCESSING），毫秒级 |
| hard pin 加/解 | 读出 → `Remove` → 带 hard_pin 重 put → 校验副本 | 全量过 kvcm 一趟 | kvcm 本地持有数据直到校验成功，失败重试不丢 |

- 保活与 pin
  - `GrantLease` 只对已有 pin 的 key 顺带续 pin（`master_service.h:938` @0.3.11）
  - 保活循环可维持 pin 不过期
  - 停保活 → pin 随 TTL 自然失效 → soft pin 的「解」可靠自然到期

### 属主问题：两个并列方案

- 起因：空写触发 F3①，属主被无条件改成调用方

| | 方案一：属主改归 kvcm | 方案二：不改属主（借名） |
|---|---|---|
| 做法 | kvcm 传自己的 UUID（sqlite 持久化，重启同 id 重注册） | kvcm 查得原属主 UUID，原样传回（F4） |
| mooncake 代码 | **不改**（前提：空写通道不落 Python，F9） | **要改**：master 加「key → 属主 UUID」查询 RPC（内部数据现成，纯暴露，F8） |
| 属主语义 | **改变**：引擎下线 key 不删（D3 retain 收益）；kvcm 下线 → 名下 key 全删（新单点；缓解 = UUID 持久化 + 重启快于 master 心跳超时） | **不变**：引擎下线 key 删（现状） |
| 引擎配合 | 无 | 无（补丁打在 master，引擎零感知） |

- **决策条件（外部依赖，非单方可定）**
  1. mooncake 代码可改性
     - 上游 PR：纯暴露型，接受度预期高，节奏不可控
     - fork 维护：升级为 publisher 本来就要走，多一个 patch 是边际成本
  2. kvcm 实现语言（E10/E11）
     - 若落 Python，方案一也需绑定补丁 → 两方案都改 mooncake
- 属主语义若变 → 与 retain（D3 耦合）统一评估，不在 pin 单独定

## §3 demote / retain / release

- 待议，现状描述在 [backlog.md](backlog.md) D2
- retain
  - 与属主语义（§2）、D3 副本生命周期直接耦合
  - `AddReplicaForRetainedClient` 为 main 独有、0.3.11 不含（E6 已核）→ 候选机制输入
