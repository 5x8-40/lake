# PR73 临时评审跟踪（合入前必须删除）

> **性质：临时文档。** 只记录当前仍未关闭的评审项 / 已知边界 / 合并门禁。
> **PR73 合入前必须删除本文件**：最后一个 commit `git rm kvcm/design/pr73-tracker.md`，PR 描述注明“临时跟踪文档已删除”。
>
> 代码状态以 git 为准；已解决的问题不再列出，需要追溯看 git 历史 / PR review。每轮 review 只改剩余项与结论。

## 快照

| 项 | 值 |
|---|---|
| PR | [#73 feat(kvcm): 接上主线 move、remove 与只读管理端点](https://github.com/5x8-40/lake/pull/73) |
| 分支 | `feat/kvcm-ops-mainline` |
| base | `main` |
| 本地 worktree | `/home/witcher/lake-wt-ops` @ `feat/kvcm-ops-mainline` |
| 当前结论 | 语义和主要代码已收口；等 PR72 / PR74 合入后 rebase，再处理剩余项；**暂不合并** |

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

## 范围 / 已知缺口（不阻塞）

- `retain` 未实现：接口保留，直接 `UnsupportedCapabilityError`；`ReleaseLoop` / `RetainLoop` 只有骨架。
- intent 只覆盖预热；其他操作没有 intent（E13 / E14 / E15 未做）。
- `InstanceWatcher` 未接；实例目录只做启动登记。
- `EventPublisher` 未接通；move/remove 事件等 E14（A06）。
- 限速器只有接口，daemon 未接线（E15 接入点）。
- `rebuild` 不区分任务归属：多模型/多后端共用一个 Ledger 时会按本实例 KeyComputer 错算；单实例单模型无此问题，多模型前必须加 `model_id` 归属判定。
- 提交并发默认 4 是进程级；单机窗口实验 #16 的结论，跨机 #17 仍半挂，未纳入 PR 验收。

## 剩余问题

### 0. 合并顺序

- **A01 / PR72 依赖（OPEN）**
  - `feat/kvcm-segment-admission` 不是当前 HEAD 的祖先，`main` 也没有 `_admit_new_bytes`。
  - `backend.move` 会直接抛 `RuntimeError`，move 实际不可用。
  - 结论：先合 PR72 / PR74，再把 PR73 rebase；不 cherry-pick `_admit_new_bytes`。

### 1. 重启 / 并发 / 保活

- **A07 partial handle rebuild（OPEN）**
  - copy 部分失败只把已发任务号写 `_plans` / Ledger；`rebuild` 从 token_ids 重算全量 plan。
  - 重启后 partial handle 会核验从未提交的 key，误报 `VerifyError` / 超时；部分已成功的任务也被拖死。
  - 方向：Ledger 记已提交的 key/seg 子集，或 partial handle 不参与 rebuild；补重启 partial 用例。
- **A08 StoreClient 多线程并发（OPEN）**
  - `backend._rpc` 用 4~8 线程 executor；同一个 `MooncakeDistributedStore` 被 copy/query/exists/keepalive 并发使用。
  - 绑定内部是否线程安全未验证；可能出现竞态/崩溃。
  - 方向：查 binding/Client 线程安全；不行就给控制面 RPC 加锁；补并发压测。
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

1. **A01**：等 PR72 / PR74 合入后 rebase PR73；不 cherry-pick `_admit_new_bytes`。
2. **prefetch 主路径**：A07 / A08 / A03 / R16 / R06 / R18。
3. **soft pin**：真机 ABI / NoF / Case A/C；版本按 v0.3.13.post1 部署。
4. **已知边界**：R02 / R03 / R04 / A04 / A15 / R05 / R14 / R15 / A06 / A16 / A17 写进文档即可。

建议按 **A01 → A07/A08 → A03/R16 → R06/R18 → A14 真机** 推进；A16 / A17 / A06 不阻塞。

## PR 体量与拆分

- 当前相对 `main`：约 52 commits、44 files、4974 行新增 / 575 行删除。
- 体量主要来自：测试约 42%、生产代码约 27%、design 文档约 14%、native 约 6%。
- 原因：move/remove/admin、soft pin native、keepalive/demote/promote、E11/E12 修复和测试/文档混在一个长分支。
- 计划：先把剩余问题收完，再按 feature 拆 PR（core / soft pin / keepalive-demote-promote / 文档测试），不在问题未收口时切。

## 合入前删除清单

- [ ] 所有 OPEN / DEFER 已关闭或明确接受为已知边界
- [ ] `git rm kvcm/design/pr73-tracker.md`
- [ ] 最后一个 commit 说明“删除 PR73 临时跟踪文档”
- [ ] `git diff main...HEAD --stat` 不再包含本文件
- [ ] PR 描述注明“临时跟踪文档已删除”

## 维护日志

- **2026-11-04**：整理 tracker：移除已关闭项（NoF/admin、pin HARD、keepalive 拆名、move source、demote LOCAL_DISK、promote flow、remove 全局、R17 日志），只保留剩余问题、DEFER 和合并门禁；补 PR 体量与拆分计划。
- **2026-11-04**：对照 HEAD 原文件逐项复核：原 18 条 review + 17 条额外发现中，所有 OPEN / DEFER / ACCEPT 残余均保留；补回 Case C 旧 buffer 600s、外部 offload 竞态、0.3.14-rc1 未验证、已拍板结论和推进顺序。
