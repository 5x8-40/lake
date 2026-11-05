# KVCM demote/promote PR 临时评审跟踪（合入前必须删除）

> **临时文档。** 只属于 `feat/kvcm-demote-promote`；**合入前 `git rm`**，PR 描述注明已删。
> 基于已合入 main 的 #80（`5e2e120`），只记 demote/promote 增量。

## Stack

| PR | 分支 | base | 内容 |
|---|---|---|---|
| core | #80 已合入 main | — | move/remove/admin、prefetch/TaskEngine/daemon、keepalive |
| demote/promote | `feat/kvcm-demote-promote` | `main`（含 #80） | `demote` / `promote` / `check_promote`、PromoteFlow、参数 |
| soft pin | `feat/kvcm-soft-pin` | demote/promote | native 空写、pin(SOFT)、NoF |

## 能力范围

库能力，未接线：有 L3/L2 实现、单测、daemon 参数；无 TaskEngine 消费者、不扩 `SubmitIntent`、daemon 无自动入口。调用方持 backend / `PromoteFlow` 自调。SessionEnd 等编排另开。

**有**

- `MooncakeBackend.demote`：`unregister_keepalive` → 等满 `lease_ttl_s` → `GET /batch_query_keys` 确认每 key `LOCAL_DISK`（DFS 不算）。失败/取消只恢复原先保活行与原 `registered_at`。
- `promote(prefix)`：打 `promote_hits` 次 `GetReplicaList`，不带 target、无 `TaskHandle`。
- `check_promote(prefix)`：每 key ≥1 memory；`memory_instance` 目录反查持有实例（可空）。
- `PromoteFlow.run(prefix)` → `PromoteResult`：已有 memory 不触发；否则轮询到齐。
- `--promote-hits`；`--lease-ttl-s`（core 已有，demote 复用）；`has_offload` / `has_promotion` 主线 YES。

**没有**

- soft pin / native / NoF（#82）
- move/remove/admin/prefetch（#80）
- A16 前缀串行；TaskEngine / intent 接线

## 结论

| 项 | 结论 |
|---|---|
| demote 完成条件 | 停保活 → 满 lease TTL → 每 key 有 LOCAL_DISK；不查剩余租约 |
| promote 完成条件 | 每 key ≥1 memory；不带 target；落点反查可空；指定机器用 copy |
| R05 | **关闭**（不再要求调用方指定盘主 target） |

## 代码核对

| 检查项 | 结果 |
|---|---|
| demote 只认 LOCAL_DISK | ✅ `backend.demote` |
| 失败/取消恢复原保活、不重置窗口 | ✅ `_restore_demote_keepalive` |
| promote 无 target、只触发 | ✅ `backend.promote` |
| 已有 memory 不触发 | ✅ `flows/promote.py` |
| `memory_instance` 可空 | ✅ |
| `has_offload` / `has_promotion` | ✅ YES（main） |

## 剩余（本 PR 不修）

- **A16（OPEN，必修）**：demote 已 unregister 后，同前缀 `exists` / 预热仍在 master 续租，可打穿 lease 等待。要前缀级串行；只拦 KeepaliveLoop 或加长 sleep 不够。不在本 PR / #82。见 `summary.md` A16。
- **A17（DEFER）**：demote sleep+核验在 L3，promote 等待在 L2；与 A16 不同项。
- **A06（DEFER → E14）**：move/remove 事件；promote 不发事件。
- **VerifyError 命名（未决）**：demote「无 LOCAL_DISK」复用 `VerifyError`；分类保留，是否改名另定。

## 合入前

OPEN/DEFER **不要求修完**即可合：A16 永久文档已挂；VerifyError 命名未决不挡；A17/A06 DEFER。

- [ ] PR 描述写上表口径（含 promote 无 target、R05 关闭）
- [ ] `git rm` 本文件；末 commit 说明删除；PR 描述注明已删
