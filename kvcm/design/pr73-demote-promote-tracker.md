# KVCM demote/promote PR 临时评审跟踪（合入前必须删除）

> **临时文档。** 只属于 `feat/kvcm-demote-promote`；**合入前 `git rm`**，PR 描述注明已删。
> 基于已合入 main 的 #80（`5e2e120`），只记 demote/promote 增量。

## Stack

| PR | 分支 | base | 内容 |
|---|---|---|---|
| core | #80 已合入 main | — | move/remove/admin、prefetch/TaskEngine/daemon、keepalive |
| demote/promote | `feat/kvcm-demote-promote` | `main`（含 #80） | demote/promote、PromoteFlow 整包、TaskEngine 接线 |
| soft pin | `feat/kvcm-soft-pin` | demote/promote | native 空写、pin(SOFT)、NoF |

## 能力范围

- L3 demote / promote / `check_promote(target=)`
- PromoteFlow：无 target 提回；有 target 整包（可套 Prefetch）；可注入带闸 prefetch
- TaskEngine：intent 一律 `PromoteFlow(target=)`；闸只挡 copy；不扩 proto

**有**

- `demote` / `promote` / `check_promote` / `memory_instance`
- `PromoteFlow.run(prefix, *, target=None)` → `PromoteResult.ready`
- TaskEngine → PromoteFlow；注入 `_gated_prefetch`（`check_segment_room` + 前缀闸 + PrefetchFlow）
- copy 后 `check_promote` 的 `QueryError` → 未就绪重试；`AdmissionWait` 原样上抛

**没有**

- soft pin（#82）；SessionEnd 等；A16 前缀串行

## 结论

| 项 | 结论 |
|---|---|
| L3 promote | 不带 target，只触发 |
| PromoteFlow 整包 | 传 target：已在 / promote / Prefetch；完成 = `check_promote(target)` |
| 闸 | 只挡真要 copy；已在 target / 只提回不过闸 |
| intent | TaskEngine 一律 PromoteFlow(target=) |
| R05 | 关闭于 L3；实例可用靠 L2 整包 |

## 代码核对

| 检查项 | 结果 |
|---|---|
| demote / promote L3 | ✅ |
| PromoteFlow 整包 + 软核验 | ✅ |
| TaskEngine 注入闸 prefetch | ✅ |
| intent 三条路径单测 | ✅ |

## 剩余（本 PR 不修）

- A16 OPEN；A17 / A06 DEFER；VerifyError 命名未决

## 合入前

- [ ] PR 描述写口径（整包 + 闸仅 copy + TaskEngine 接线）
- [ ] `git rm` 本文件
