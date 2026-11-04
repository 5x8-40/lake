# PR73 临时评审跟踪（合入前必须删除）

> **性质：临时文档。** 只记录当前仍未关闭的评审项 / 已知边界 / 合并门禁。
> **PR73 合入前必须删除本文件**：最后一个 commit `git rm kvcm/design/pr73-tracker.md`，PR 描述注明“临时跟踪文档已删除”。
>
> 代码状态以 git 为准；已解决的问题不再列出，需要追溯看 git 历史 / PR review。每轮 review 只改剩余项与结论。

## 快照

| 项 | 值 |
|---|---|
| PR | [#73 feat(kvcm): 接上主线 move、remove 与只读管理端点](https://github.com/5x8-40/lake/pull/73) |
| 分支 | `wip/kvcm-ops-rebased`（旧 PR73 分支保留为 backup） |
| base | `main` @ `296f166`（PR71/72/74 已合入） |
| 本地 worktree | `/home/witcher/lake-wt-ops` @ `wip/kvcm-ops-rebased` |
| 当前结论 | 已 rebase 到 main；A01/A07/A08 已处理；下一步拆 3 个 stacked PR |

## 版本基线

- mooncake 目标版本：**v0.3.13.post1**，master / client 同版本。
- 代码不写版本号、不按版本分叉；版本要求只在文档里约束。
- v0.3.13.post1：有 `SoftPinAction`；没有 `has_read_lease` gate（R01 判 N/A）；`/batch_query_keys` 有 `disk_values` / `local_disk_values`。
- main / v0.3.14 有 `has_read_lease` gate，**不在本 PR 目标内**。

## 已拍板结论（只留结论）

- lease 是 key 级，target 不是保活维度；保活按前缀窗口，停止时所有副本一起冷（R08 判 N/A）。
- `remove` 全局删、不支持 target；`force` 只绕过 lease，不绕过 replication task（A02 / A13）。
- `pin(HARD)` 拒绝；保活拆 `register_keepalive` / `unregister_keepalive` / `renew_once`（R13 / A09）。
- `move` 的 `source` 必填（A10）。
- `demote` 只认 `LOCAL_DISK`，DFS 不算成功（R11）。
- `promote` 只触发；`PromoteFlow` 用 `check_promote` 轮询到 target memory replica（R12 / A12）。
- `promote` 没有 TaskHandle；READY 只代表轮询时看到 target memory replica，不是任务型完成。
- admin 短回 / `ok=false` / 坏描述符 → `QueryError`；坏描述符仍整批失败，flow 首次日志 + 超时带 key（R10 / A11 / R17）。
- NoF 与 DRAM memory 分离；NoF-only 拒绝（R07 / A04）。
- `demote` 统一“停保活 → 等满 TTL → 查盘”，写穿部署也等；没有剩余租约查询。
- `remove` 失败 / 取消不把保活加回；副本可能提前变冷（已接受）。
- A07：`rebuild` 跳过 task 行数 != 全量 plan 的 partial handle；在途任务重启后不跟踪，由下一次 `exists` / 预热重判。
- A08 源码审计：当前 StoreClient RPC 路径只读共享 client；master RPC 走 `RpcClientPool`（`send_request` 线程安全）+ `hot_cache` 自有锁；未加全局锁。真机并发压测归 A14。

## 评审摘要（Step 1–6 压缩版）

> 只留判断、证据和结论；完整过程在 backup 分支 `backup/pr73-pre-rebase-20261104`（提交 `9661169`）和 git 历史里。

### 评审基线（Step 1）

- 分层：L2 只调 L3；L3 不出现 seg / lease / client_id / PoolKey / rank。
- 八项操作：copy / move / keepalive / pin / demote / promote / retain / remove；READY 只给 copy / move / promote，retain 跟做法走。
- Ledger 四表（tasks / keepalive / replicas / sessions）；rebuild 恢复 copy / move；keepalive 注册表不可丢。
- E11：单事件循环 + 协程 + RPC executor + Ledger 单线程；E12：SubmitIntent / GetTask。
- E13 / E14 / E15 未做；ReleaseLoop / RetainLoop 骨架；限速器只留接口。

### 设计改动（Step 2）

- **事实修正**：retained client 在 `1fc27b6` 不存在；`/batch_query_keys` 主线零副作用；promote 不能用 offload 端点对 seg 名；官方绑定没有空写 start/end；demote 查盘改走 admin；0.3.11 soft pin 通道 NO。
- **语义变更**：`pin(HARD)` 拒绝；demote 统一“停保活 → 等满 TTL → 查盘”；remove 总是先停保活；promote 只触发、由 PromoteFlow 验证；keepalive 拆成 register/unregister/renew_once；capability 增 tenant/DFS 字段。
- **新约定**：`--admin` 缺省从 master host 推 `:9003`；`--instance-segs` 做启动登记；intent 只覆盖预热；move/remove 事件推迟到 E14；Release/Retain 只留骨架。

### 代码地图（Step 4）

| 文件 | 职责 |
|---|---|
| `kvcm/ops/base.py` | L3 八操作接口 + query_task / exists / abandon；默认 Unsupported |
| `kvcm/backends/mooncake/backend.py` | L4 适配：copy/move/remove、keepalive、demote/promote、pin、Ledger、rebuild |
| `kvcm/backends/mooncake/client.py` | store 绑定封装：copy/move/remove/query/exists + soft_pin |
| `kvcm/backends/mooncake/admin_main.py` / `http_admin.py` | 主线只读 admin：batch_query_keys / tenant_quota；段容量准入 |
| `kvcm/flows/prefetch.py` / `flows/promote.py` | L2 编排：预热、提回 |
| `kvcm/loops.py` | KeepaliveLoop；Release/Retain 骨架 |
| `kvcm/task_engine.py` | intent 队列 + 传输闸 + READY 后保活/补发事件 |
| `kvcm/daemon.py` | 进程壳：装配、启动、优雅退出 |
| `kvcm/ledger.py` / `instances.py` | sqlite 状态；实例→seg 目录 |
| `kvcm/native/` | 空写 soft pin 扩展（`_empty_pin.so`） |

### 设计判断（Step 3：原设计不合理 / 已拍板）

| 原判断 | 结论 | 证据 |
|---|---|---|
| hard pin 可以事后设置 | 不可行；`pin(HARD)` 拒绝，lease 保护走显式 `register_keepalive()` | D2 §2 |
| demote 写完就完成 | 写穿/写回都要先停保活、等满 lease TTL，再查 LOCAL_DISK；DFS 不算成功 | D2 §3 |
| remove 可以按 target 删 | mooncake 没有可用接口；remove 只做全局删；force 只跳 lease，不跳 replication task | D2 §4 / F6② |
| promote 立即 READY | 只打 N 次 `GetReplicaList` 触发；READY 由 `PromoteFlow` + `check_promote` 轮询，L3 不返回 TaskHandle | D2 §3 |
| 保活按 target | lease 是 key 级，target 不是保活维度；保活按前缀窗口 | D2 F9 / R08 N/A |
| admin 短回/NoF 当不存在 | 短回、`ok=false`、坏描述符都转 `QueryError`；NoF 与 DRAM memory 分离 | D2 F11b / R07/R10/A11/R17 |
| soft pin 默认不可用 | 目标版本 v0.3.13.post1 有条件可用；Case B 前置 + 地址检查 + `UpsertRevoke` | D2 §2 / native |
| retain 靠 retained client | `1fc27b6` 没有 `AddReplicaForRetainedClient`；按 PR74 的四条路（DFS / move / kvcm 自挂段盘 / 旧 client_id） | D2 §4 |
| capability 是运行时探测 | 是显式声明，不做版本探测；版本要求由文档约束 | R15 / capability.py |

### 代码 vs 设计（Step 5）

| 检查项 | 结果 | 证据 |
|---|---|---|
| L2 只调 L3；L3 不出现 seg/lease/client_id | ✅ | `flows/prefetch.py`、`task_engine.py` |
| move READY + source 必填 | ✅（rebase 后 `_admit_new_bytes` 已接上） | `backend.move`、PR72 |
| keepalive 是周期续租 | ✅ 拆成 register/unregister + renew_once | `ops/base.py`、`loops.py`、`backend.py` |
| pin SOFT = 空写标记 | ⚠️ 通道可用；ABI / Case C / NoF 真机仍是残余 | 剩余项 soft pin |
| demote 只认 LOCAL_DISK | ✅ | `backend.demote` |
| promote READY = 目标可调度 | ✅ L3 只触发，flow 轮询 | `backend.promote`、`flows/promote.py` |
| remove 全局删 | ✅ 不支持 target | `backend.remove`、D2 §4 |
| admin 零副作用核验 | ✅ 走 `/batch_query_keys`；R17/R18 边界保留 | `backend._verify_missing` |
| READY 只给 copy/move/promote | ✅ 空任务号抛 `ValueError` | `ops/base.py`、`backend.query_task` |
| Ledger rebuild 恢复 copy/move | ✅ A07 后 partial handle 跳过 | `backend.rebuild`、`test_rebuild_skips_partial_handle` |
| InstanceWatcher / EventPublisher / Release / Retain / limiter | ⚠️ 已知缺口 | 范围 / 已知缺口 |
| 多模型 rebuild 归属 | ⚠️ 未按 model_id 过滤 | 范围 / 已知缺口 |

### 代码问题与已解决（Step 6）

| 项 | 结论 | 证据 |
|---|---|---|
| A01 move 依赖 PR72 | 已 rebase 到 main，A01 关闭 | `6a227c7` |
| A07 partial handle rebuild | rebuild 跳过 task 行数 != 全量 plan 的 handle | `test_rebuild_skips_partial_handle` |
| A08 StoreClient 并发 | 源码审计：`RpcClientPool::send_request` + `hot_cache` 锁；未加全局锁；真机压测归 A14 | 本期结论 |
| R07/R10/R11/R12/R13/A09–A13/R17 | 已修；分别见 D2、backend、loops、capability 与测试 | git 历史 / 编码 |
| A06 / A16 / A17 | DEFER | 剩余项 / 已知缺口 |

## 范围 / 已知缺口（不阻塞）

- `retain` 未实现：`backend.retain` 直接 `NotImplementedError`（D2 §4 已登记四条路）；`ReleaseLoop` / `RetainLoop` 只有骨架。
- intent 只覆盖预热；其他操作没有 intent（E13 / E14 / E15 未做）。
- `InstanceWatcher` 未接；实例目录只做启动登记。
- `EventPublisher` 已接通：prefetch READY 后按目标实例补发 `BlockStored`；move/remove 事件等 E14（A06）。
- 限速器只有接口，daemon 未接线（E15 接入点）。
- `rebuild` 不区分任务归属：多模型/多后端共用一个 Ledger 时会按本实例 KeyComputer 错算；单实例单模型无此问题，多模型前必须加 `model_id` 归属判定。
- 提交并发默认 4 是进程级；单机窗口实验 #16 的结论，跨机 #17 仍半挂，未纳入 PR 验收。

## 剩余问题

### 1. 重启 / 并发 / 保活

- **A03 保活注册 fire-and-forget（OPEN）**
  - `TaskEngine` READY 后调 `KeepaliveLoop.register`；`_submit_ledger` 不等待。
  - READY 可能先于 keepalive 行落盘；副本可能提前变冷。
  - 方向：接受并写明，或 READY 前 await 落账。
- **R16 保活量未算（OPEN）**
  - `KeepaliveLoop.sweep_once` 每 5s 逐前缀 `renew_once`（全 key `batch_is_exist`）。
  - 请求量/延迟未测量；长前缀可能拖慢 sweep。
  - 方向：测量、限流、分批或按前缀错峰。

### 2. admin / 部署

- **R06 段名 vs transport endpoint（OPEN）**
  - `_verify_missing` 用配置里的 seg 名比 admin 返回的 `transport_endpoint_`；`instances.py` 只存段名。
  - 段名/端点没有映射，部署错误时会误核验。
  - 方向：启动时校验段名与 admin 返回一致，或建立映射。
- **R18 8KB 分批依据（OPEN）**
  - `_MAX_BATCH_QUERY_REQUEST_BYTES = 8192` 只算请求行，不含 header；上游实际限制未校准。
  - 方向：按 master 实际限制校准，或改 POST。
- **R14 一进程一种 TP 组合（OPEN，文档边界）**
  - `daemon.py` 只建一个 `KeyComputer`；`--tp-size` / `--peer-tp-size` / `--put-step` 是进程级。
  - 多组合需要多 backend，文档写清即可。
- **R15 capability 是声明不是探测（OPEN，文档约束）**
  - `--capability` 按 flag 选能力集，启动不探 master。
  - 版本由文档约束，不做运行时探测/分叉；是否给通用启动项加警告另议。

### 3. promote

- **R05 没有实例 → client_id（OPEN）**
  - promote 无法事先证明 target 是盘副本登记实例；现在靠 `PromoteFlow` 轮询 target memory replica。
  - 方向：开放 promote 前加注册表，或明确写成假阴性边界。

### 4. soft pin 残余

- **R02 部分失败不回滚（已知边界）**
  - 一批 key 钉到一半失败，前面的 key 保持钉上；错误消息带 succeeded keys，无事务回滚。
- **R03 `put_step=1` 是否等于单 slice（待真机）**
  - guard 已拒绝 `put_step != 1`；`put_step=1` 的真实布局仍需实测。
- **R04 ABI（ACCEPT，残余）**
  - 手写头文件已对 0.3.13.post1 布局；没有编译期 `static_assert` / `offsetof`；运行时 ABI 仍需真机。
- **A04 NoF 收尾（已分离，待真机）**
  - memory + NoF 可空写；NoF-only 拒绝；`UpsertEnd(ALL)` 是否把 NoF 收干净仍需真机确认。
- **A15 Case A 地址复用（残余）**
  - 地址检查 + Revoke 兜住 Case C 和正常 Case A；Case C 旧 buffer 进 `discarded_replicas_` 延迟释放（默认 600s），检查时不会因 C 复用地址。
  - 只有 `exists` 后 key 被 force remove / stale 清理 / lease 过期驱逐，或 `UpsertStart` 惰性清理 / processing 抢占后无 COMPLETE 副本，且分配器复用同一地址时，可能误判为 B。最坏重算；ABI 仍需真机。
- **Start/End 崩溃（已知边界）**
  - key 可能卡 PROCESSING；没有恢复机制。
- **读者短暂不可读（已知边界）**
  - `UpsertStart` → `UpsertEnd` 窗口。
- **`client_id` 改写（已知边界）**
  - 空写把 key 的 `client_id` 写成 kvcm；UUID 未持久化，重启同 id 重注册未实现。
- **续期（已知边界）**
  - `GrantReadLease` 不续 soft pin；30min 到期要重新空写。`pin(SOFT)` 只做一次 `exists`，不 `register_keepalive`。
- **调用方（不在本 PR 范围）**
  - `pin(SOFT)` 目前只有测试调用；prefetch READY 后是否/何时 pin 由后续策略决定。
- **版本依赖（已文档化）**
  - 只承诺 v0.3.13.post1；0.3.11 / 0.3.12 是 bool + `UpsertEnd(string)`，不兼容；0.3.14-rc1 无 gate 但非目标基线、未验证；0.3.14-pre+ 有 gate，不可用。

### 5. 明确 DEFER

- **A06 move/remove 成功后不发事件（DEFER）**
  - 发布端在 E14；与 E14 合入时接。
- **A16 同前缀操作串行化（DEFER）**
  - TaskEngine 的全局闸不是前缀互斥；prefetch / demote / remove / pin 可并发。
  - R09（demote 等租约期间预热续租）、A05（remove 与 demote 租约竞态）归入此项。
  - 外部 offload 与 remove 的竞态也无法靠本地锁完全挡住。
  - 后续单独设计 backend 前缀锁或操作状态机，不放进 PR73。
- **A17 操作粒度未统一（DEFER）**
  - copy：`copy` 只提交，但查询 + 副本核验仍在 `backend.query_task`；PrefetchFlow 只轮询。
  - move：无 MoveFlow。
  - demote / remove：在 L3 内 sleep + 核验 / 删除。
  - 后续逐步拆 MoveFlow / DemoteFlow / ReleaseFlow；copy 核验是否移出 `query_task` 另定。

### 6. 测试 / 真机

- **A14 关键前提未真机验证（OPEN）**
  - 仍缺：`_empty_pin.so` 的 ABI、`buffer_address_` / `size_`、Case A/C、NoF 收尾、段名/端点不一致、partial rebuild、并发 RPC。
  - 单测全绿不能证明这些成立。

## 合入门禁

1. **基线**：已 rebase 到 `main`（PR71/72/74）；旧 PR73 分支只作 backup。
2. **prefetch 主路径**：A03 / R16 / R06 / R18。
3. **soft pin**：真机 ABI / NoF / Case A/C；版本按 v0.3.13.post1 部署。
4. **已知边界**：R02 / R03 / R04 / A04 / A15 / R05 / R14 / R15 / A06 / A16 / A17 写进文档即可。

建议按 **A03/R16 → R06/R18 → A14 真机** 推进；A16 / A17 / A06 不阻塞。

## PR 体量与拆分

- 当前相对 `github/main`：约 45 files、4729 行新增 / 552 行删除（squash rebase 后）。
- 体量主要来自：测试约 42%、生产代码约 27%、design 文档约 14%、native 约 6%。
- 原因：move/remove/admin、soft pin native、keepalive/demote/promote、E11/E12 修复和测试/文档混在一个长分支。
- 计划：拆 3 个 stacked PR（core / keepalive-demote-promote / soft pin）；测试和文档跟随各自 feature，不单独拆 docs/test PR。

## 合入前删除清单

- [ ] 所有 OPEN / DEFER 已关闭或明确接受为已知边界
- [ ] `git rm kvcm/design/pr73-tracker.md`
- [ ] 最后一个 commit 说明“删除 PR73 临时跟踪文档”
- [ ] `git diff main...HEAD --stat` 不再包含本文件
- [ ] PR 描述注明“临时跟踪文档已删除”

## 维护日志

- **2026-11-04**：整理 tracker：移除已关闭项（NoF/admin、pin HARD、keepalive 拆名、move source、demote LOCAL_DISK、promote flow、remove 全局、R17 日志），只保留剩余问题、DEFER 和合并门禁；补 PR 体量与拆分计划。
- **2026-11-04**：对照 HEAD 原文件逐项复核：原 18 条 review + 17 条额外发现中，所有 OPEN / DEFER / ACCEPT 残余均保留；补回 Case C 旧 buffer 600s、外部 offload 竞态、0.3.14-rc1 未验证、已拍板结论和推进顺序。
- **2026-11-04**：PR71/72/74 已合入 main；旧分支 squash rebase 到 `wip/kvcm-ops-rebased`，A01 关闭。
- **2026-11-04**：A07 关闭：`rebuild` 跳过 task 行数 != 全量 plan 的 partial handle，补 `test_rebuild_skips_partial_handle`。
- **2026-11-04**：A08 源码审计关闭：当前 RPC 路径只读共享 client，`MasterClient::invoke_rpc` 走 `RpcClientPool::send_request`（线程安全）+ `hot_cache` 锁；真机并发压测归 A14。
