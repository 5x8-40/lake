# KVCM demote/promote PR 临时评审跟踪（合入前必须删除）

> **性质：临时文档。** 本文件只属于 `feat/kvcm-demote-promote` 这个 stacked PR；**合入前必须删除**，PR 描述注明“临时跟踪文档已删除”。
>
> 代码状态以 git 为准；本 PR 基于 `feat/kvcm-ops-core`，core 的 tracker 仍在树里，本文件只列 demote/promote 的增量。

## Stack

| PR | 分支 | base | 内容 |
|---|---|---|---|
| core | `feat/kvcm-ops-core` | `github/main` | move/remove/admin、prefetch/TaskEngine/daemon、keepalive、A07 |
| demote/promote | `feat/kvcm-demote-promote` | core | `demote` / `promote` / `check_promote`、PromoteFlow、lease/promote 参数 |
| soft pin | `feat/kvcm-soft-pin` | demote/promote | native 空写、pin(SOFT)、NoF 收尾 |

## Scope

**包含**

- `backend.demote`：先 `unregister_keepalive`，等满 `lease_ttl_s`，再用 `GET /batch_query_keys` 确认每个 key 的 LOCAL_DISK；DFS 不算成功。
- `backend.promote`：只打 `promote_hits` 次 `GetReplicaList` 触发。
- `backend.check_promote` + `flows/promote.py::PromoteFlow`：轮询 target memory replica 后返回。
- daemon `--promote-hits`；`--lease-ttl-s` 已在 core（remove 用），本 PR 复用于 demote。
- capability `has_offload` / `has_promotion` 置 YES。

**不包含**

- soft pin / native / NoF 收尾（soft pin PR）。
- core 的 move/remove/admin/prefetch（core PR）。

## 设计判断（本 PR）

| 原判断 | 结论 | 证据 |
|---|---|---|
| demote 写完就完成 | 停保活 → 等满 lease TTL → 查 LOCAL_DISK；DFS 不算成功；没有剩余租约查询 | D2 §3 / `backend.demote` |
| promote 立即 READY | L3 只触发，不返回 TaskHandle；`PromoteFlow.run()` 轮询 `check_promote` 为真才返回 | D2 §3 / `flows/promote.py` |
| promote 可以用 offload 端点对 seg 名 | 不能；offload 端点与 seg 名不是同一套 | D2 §3 / `check_promote` |
| 没有实例→client_id 也能保证落点 | 不能事先证明；open，见 R05 | R05 |

## 代码 vs 设计

| 检查项 | 结果 | 证据 |
|---|---|---|
| demote 只认 LOCAL_DISK | ✅ | `backend.demote` |
| demote 失败/取消恢复原保活行、不重置窗口 | ✅ | `backend._restore_demote_keepalive` |
| promote 只触发、无 TaskHandle | ✅ | `backend.promote` |
| PromoteFlow 轮询到 target memory replica 才返回 | ✅ | `flows/promote.py` |
| promote 不做读删写 | ✅ | 同上 |
| capability `has_offload` / `has_promotion` | ✅ YES | `capability.py` |

**注意**：这里说的是 `PromoteFlow.run()` 返回，不是 `TaskStatus.READY`；promote 没有任务型 READY。

## 剩余问题

- **R05 没有实例 → client_id（OPEN）**
  - promote 无法事先证明 target 是盘副本登记实例；现在靠 `PromoteFlow` 轮询 target memory replica。
  - 方向：开放 promote 前加注册表，或明确写成假阴性边界。
- **A16 / A17（DEFER，跨 PR）**
  - 同前缀操作串行化、操作粒度统一不在本 PR；见 core tracker。
- **A06（DEFER，跨 PR）**
  - move/remove 事件发布在 E14；promote 不涉及事件。

## 本 PR 已修的小问题

- demote 能力检查消息不再写死“0.3.11 的 `/batch_query_keys`”，改成“当前能力集没有盘副本字段”。
- `promote` / `check_promote` 的 `has_promotion` 检查抽成 `_require_promotion()`，不再重复。
- tracker / 代码注释统一用“`PromoteFlow.run()` 返回”，不再把 promote 说成任务型 READY。

## 合入前删除清单

- [ ] 本 PR 的 OPEN / DEFER 已关闭或明确接受
- [ ] `git rm kvcm/design/pr73-demote-promote-tracker.md`
- [ ] 最后一个 commit 说明“删除 demote/promote 临时跟踪文档”
- [ ] PR 描述注明“临时跟踪文档已删除”

## 维护日志

- **2026-11-04**：从 core tracker 拆出；补 demote 消息、promote 重复检查、promote 返回语义三项清理。
