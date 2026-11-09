# KVCM soft pin PR 临时评审跟踪（合入前必须删除）

> **性质：临时文档。** 本文件只属于 `feat/kvcm-soft-pin` 这个 stacked PR；**合入前必须删除**，PR 描述注明“临时跟踪文档已删除”。
>
> 本 PR 基于已合入的 #81（`main` @ `ddfc4c4`）；只列 soft pin 增量。

## Stack

| PR | 分支 | base | 内容 |
|---|---|---|---|
| core | #80 已合入 | — | move/remove/admin、prefetch/TaskEngine/daemon、keepalive |
| demote/promote | #81 已合入 | — | demote / promote / PromoteFlow |
| soft pin | `feat/kvcm-soft-pin` | `main` | native 空写、pin(SOFT)、NoF |

## Scope

**能力范围（库能力，未接线）**

- 有 L3 `pin(SOFT)` / native / 单测；daemon / TaskEngine / intent **不会**自动调用。
- prefetch READY 后是否 pin：不在本 PR。

**包含**

- `kvcm/kvcm/native/`：`_empty_pin.so`、`mooncake_pin_api.h`、构建脚本。
- `client.soft_pin` + `backend.pin`：空写 upsert、地址检查、`UpsertRevoke`。
- `capability.soft_pin_after` YES（main）；NoF 与 DRAM memory 分离。
- soft pin 测试与版本依赖文档。

**不包含**

- core / demote/promote（#80 / #81）
- TaskEngine / intent / SessionEnd 接线

## 设计判断（本 PR）

| 原判断 | 结论 | 证据 |
|---|---|---|
| soft pin 默认不可用 | 目标 v0.3.13.post1 有条件可用；Case B 前置 + 地址检查 + `UpsertRevoke` | D2 §2 / native |
| 空写可以跨版本通用 | 不通用：0.3.11/0.3.12 是 bool + `UpsertEnd(string)`；0.3.14-pre+ 有 `has_read_lease` gate | D2 §2「4. 版本依赖」 |
| `pin` 自己的 `exists` 只是防驱逐 | 目标版本无 gate，Case B；**升级到有 gate 的版本后，这个自授 lease 会单独触发 Case C**，不是只防外部读者 | D2 §2「升级注意」/ `backend.pin` 注释 |
| 地址检查能证明绝不丢数据 | 不能；Case C/A 的地址复用残余仍存在，最坏重算 | D2 §2 / A15 |

## 代码 vs 设计

| 检查项 | 结果 | 证据 |
|---|---|---|
| `pin(SOFT)` 走空写、不写数据 | ✅ | `backend.pin` / `client.soft_pin` |
| `UpsertStart` 后地址检查，不一致 Revoke | ✅ | native / `empty_soft_pin.cpp` |
| NoF 与 DRAM memory 分离；NoF-only 拒绝 | ✅ | `admin_main.py` / `backend.pin` |
| `pin(HARD)` 拒绝 | ✅ | `backend.pin` |
| 版本依赖写清 | ✅ | D2 §2「4. 版本依赖」 |
| ABI / Case A/C 真机验证 | ⚠️ A14 | 见下 |

## 剩余问题

- **A14 真机验证（OPEN）**
  - 仍缺：`_empty_pin.so` 的 ABI、`buffer_address_` / `size_`、Case A/C、NoF 收尾、段名/端点不一致、并发 RPC。
  - 单测全绿不能证明这些成立。
- **R02 部分失败不回滚（已知边界）**
  - 一批 key 钉到一半失败，前面的 key 保持钉上；错误消息带 succeeded keys，无事务回滚。
- **R03 `put_step=1` 是否等于单 slice（待真机）**
  - guard 已拒绝 `put_step != 1`；`put_step=1` 的真实布局仍需实测。
- **R04 ABI（ACCEPT，残余）**
  - 手写头文件已对 0.3.13.post1 布局；没有编译期 `static_assert` / `offsetof`；运行时 ABI 仍需真机。
- **A04 NoF 收尾（已分离，待真机）**
  - memory + NoF 可空写；NoF-only 拒绝；`UpsertEnd(ALL)` 是否把 NoF 收干净仍需真机确认。
- **A15 Case A 地址复用（残余）**
  - 地址检查 + Revoke 兜住 Case C 和正常 Case A；Case C 旧 buffer 延迟释放（默认 600s），检查时不会因 C 复用地址。
  - 只有 `exists` 后 key 被 force remove / stale 清理 / lease 过期驱逐，或 `UpsertStart` 惰性清理 / processing 抢占后无 COMPLETE 副本，且分配器复用同一地址时，可能误判为 B。最坏重算；ABI 仍需真机。
- **Start/End 崩溃（已知边界）**：key 可能卡 PROCESSING；没有恢复机制。
- **读者短暂不可读（已知边界）**：`UpsertStart` → `UpsertEnd` 窗口。
- **`client_id` 改写（已知边界）**：空写把 key 的 `client_id` 写成 kvcm；UUID 未持久化，重启同 id 重注册未实现。
- **续期（已知边界）**：`GrantReadLease` 不续 soft pin；30min 到期要重新空写。`pin(SOFT)` 只做一次 `exists`，不 `register_keepalive`。
- **调用方（不在本 PR 范围）**：`pin(SOFT)` 目前只有测试调用；prefetch READY 后是否/何时 pin 由后续策略决定。
- **版本依赖（已文档化）**：只承诺 v0.3.13.post1；0.3.11 / 0.3.12 不兼容；0.3.14-rc1 未验证；0.3.14-pre+ 有 gate，不可用。**升级注意：`pin` 的 `exists` 自授 lease 会触发 gate 版本的 Case C。**

## 跨 PR

- A16：OPEN、上线前必修、不挡合入（#81 已定口径；影响面含 `check_promote` / `promote`）。
- A06 / A17 / 多 flow 分发：DEFER。
- R13（`pin(HARD)` 拒绝）在本 PR。

## 合入前删除清单

- [ ] 本 PR 的 OPEN / DEFER 已关闭或明确接受
- [ ] `git rm kvcm/design/pr73-soft-pin-tracker.md`
- [ ] 最后一个 commit 说明“删除 soft pin 临时跟踪文档”
- [ ] PR 描述注明“临时跟踪文档已删除”

## 维护日志

- **2026-11-04**：从 core/demote tracker 拆出；补“`pin` 的 `exists` 自授 lease 在 gate 版本触发 Case C”的升级耦合，写入 D2 §2 和代码注释。
- **2026-11-09**：#81 合入后改 cherry-pick 到 `main`；summary 保留 #81 闸/`run_after_exists` 口径，soft pin 标为有条件可用；A16 与 #81 对齐为 OPEN。
