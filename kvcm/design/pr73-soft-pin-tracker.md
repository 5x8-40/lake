# KVCM soft pin PR 临时评审跟踪（合入前必须删除）

> **性质：临时文档。** 本文件只属于 `feat/kvcm-soft-pin` 这个 stacked PR；**合入前必须删除**，PR 描述注明“临时跟踪文档已删除”。
>
> 本 PR 基于已合入的 #81（`main` @ `ddfc4c4`）；只列 soft pin 增量。

## Stack

| PR | 分支 | base | 内容 |
|---|---|---|---|
| core | #80 已合入 | — | move/remove/admin、prefetch/TaskEngine/daemon、keepalive |
| demote/promote | #81 已合入 | — | demote / promote / PromoteFlow |
| soft pin | `feat/kvcm-soft-pin` | `main` | native 空写、pin(SOFT)、NoF、方案 1 收尾 |

## Scope

**能力范围（库能力，未接线）**

- 有 L3 `pin(SOFT)` / native / 单测；daemon / TaskEngine / intent **不会**自动调用。
- prefetch READY 后是否 pin：**上层接流另议**，不在本 PR 本提交范围。

**包含**

- `kvcm/kvcm/native/`：`_empty_pin.so`、`mooncake_pin_api.h`、`build_empty_pin.sh`（链 `libmooncake_store.so`）。
- `client.soft_pin` + `backend.pin`：空写 upsert、地址检查、方案 1 收尾。
- `capability.soft_pin_after` YES（main / 0.3.13.post1）；NoF 与 DRAM memory 分离。
- soft pin 单测、D2 版本依赖与收尾策略文档、本 tracker。

**不包含**

- core / demote/promote（#80 / #81）
- TaskEngine / intent / SessionEnd / prefetch READY 后自动 pin

## 设计判断（本 PR）

| 原判断 | 结论 | 证据 |
|---|---|---|
| soft pin 默认不可用 | 目标 v0.3.13.post1 **有条件可用**；Case B 前置 + 地址检查 + 方案 1 收尾 | D2 §2 / native / A14 |
| 空写可以跨版本通用 | 不通用：0.3.11/0.3.12 是 bool + `UpsertEnd(string)`；0.3.14-pre+ 有 `has_read_lease` gate | D2 §2「4. 版本依赖」 |
| `pin` 自己的 `exists` 只是防驱逐 | 目标版本无 gate，Case B；**升级到有 gate 的版本后，这个自授 lease 会单独触发 Case C** | D2 §2「升级注意」/ `backend.pin` |
| 地址检查能证明绝不丢数据 | 不能；不匹配 Revoke 在 Case B 上会 Erase；Case A 地址复用仍在 | F5 / A14 / A15 |
| Case B 上 Revoke 只是「撤写」 | **否**：sole PROCESSING → `EraseReplicas` → `Erase` 整 key | 源码 + A14 负向 |

## 收尾策略（方案 1，已选 · 现状）

权威叙述见 [D2-kv-operations.md](D2-kv-operations.md) §2「空写失败收尾」。

| 分流 | 行为 | 调用方看到什么 |
|---|---|---|
| 地址/大小**匹配** | 只 `UpsertEnd` | 成功 → soft pin 生效 |
| 匹配后 **End 失败** | **不** `UpsertRevoke`；`throw runtime_error`（经 pybind → Python 异常） | **本次 pin 失败**；进程不退出；副本可能仍 PROCESSING |
| 地址/大小**不匹配** | `UpsertRevoke`，不 End；同样抛异常 | pin 失败；若实为 Case B，**key 被 Erase**（方案 1 残余） |

辅助（已落地）：`pin` 空写前 `exists` → `renew_once` → 二次 `batch_query_keys`；不 `register_keepalive`。

**后续优化（D2 已登记，本提交不做）**

| # | 路径 | 状态 |
|---|---|---|
| **2** | 不匹配也不 Revoke，直接停（留 PROCESSING） | 已讨论；优先保数据；**未采纳为现状** |
| **3** | 池侧 `only_in_place` / pin-only | 未实现（需 fork mooncake） |
| **4** | keepalive-only（不空写） | 机制已有；不作 soft pin 替代路径 |

## 代码 vs 设计

| 检查项 | 结果 | 证据 |
|---|---|---|
| `pin(SOFT)` 走空写、不写数据 | ✅ | `backend.pin` / `client.soft_pin` |
| 匹配 → 只 End；End 失败不 Revoke | ✅ | `empty_soft_pin.cpp` |
| 不匹配 → Revoke | ✅ | `empty_soft_pin.cpp` |
| 失败以异常返回，不终止进程 | ✅ | `fail()` → `runtime_error` |
| `pin` 前 renew + 二次 query | ✅ | `backend.pin` |
| NoF 与 DRAM memory 分离；NoF-only 拒绝 | ✅ | `admin_main.py` / `backend.pin` |
| `pin(HARD)` 拒绝 | ✅ | `backend.pin` |
| `EndResult` ABI + 链 `libmooncake_store.so` | ✅ | `mooncake_pin_api.h` / `build_empty_pin.sh` / A14 |
| 版本依赖写清 | ✅ | D2 §2 |
| ABI / Case A/C 真机 | ✅ A14 方案 1 PASS | 见下实验记录 |

## 实验记录 A14（真机）

### 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-11-09 |
| 主机 | `7.242.104.45`（本机）；对照机 `7.242.110.225` 已升同版本，本轮冒烟未跨机 |
| 容器 | `vllm-ascend-lake-test` |
| mooncake | **0.3.13.post1**（master + client；自 0.3.11 升级） |
| master | `:50088`；admin metrics `:9003`；`default_kv_lease_ttl=11000` |
| 工作树 | 容器内 `/tmp/lake-a14/kvcm`（从本分支拷贝源码后本地编 `_empty_pin.so`） |
| 冒烟入口 | 容器 `/tmp/a14_soft_pin_smoke.py`（未入仓；走 `StoreClient.soft_pin`） |

### 编译 / ABI 校正（先失败后修）

| 问题 | 现象 | 修复 |
|---|---|---|
| 只链 `store.so` | `Upsert*` 未定义 | `build_empty_pin.sh` 同时链 `libmooncake_store.so` + rpath |
| `EndResult` 布局错 | End 成功被读成失败 → 误 Revoke | `mooncake_pin_api.h` 按 `tl::expected<void, ErrorCode>`（union + `ok`） |

### 用例与结果（方案 1 复测）

| # | 步骤 | 期望 | 结果 |
|---|---|---|---|
| 1 | put 1MiB → admin 查 size/addr → `soft_pin(ENABLE)` | Case B 匹配 → End；`master_soft_pin_key_count` 0→1 | **PASS** |
| 2 | 再查 admin；buffer 地址/size 不变 | 原位 | **PASS** |
| 3 | `soft_pin(DISABLE)`；`get` 比对 payload | 数据完整 | **PASS** |
| 4 | 新 key put → 故意错 `buffer_address` 再 pin | 抛异常；不匹配 → Revoke → Erase（`got_len=0`） | **PASS**（确认 F5 Case B 陷阱 = 方案 1 残余） |

**未覆盖（本轮）**：`backend.pin` 的 renew/二次 query（单测覆盖）；NoF；End 失败留 PROCESSING 注入；225 跨机；并发；段名/端点不一致。

**单测**：本机 `pytest tests/test_backend.py -k pin_soft` → **11 passed**。

### 结论

- 正向空写 soft pin 在 0.3.13.post1 + 校正 ABI 下可用。
- 方案 1 行为与文档一致；错地址删 key 是**已知接受残余**，消解看后续路径 2/3/4。
- 上层接流（prefetch READY → `pin(SOFT)`）尚未做。

## 剩余问题

- **A14 残余**：NoF 收尾、跨机、并发、段名/端点、End 失败真机注入。
- **R02** 部分失败不回滚（已知）。
- **R03** `put_step=1` ↔ 单 slice 真机布局。
- **R04** ABI 无 `static_assert`/`offsetof`（手写头 + 真机冒烟）。
- **A04** NoF：`UpsertEnd(ALL)` 是否收干净。
- **A15** Case A 地址复用（Revoke 后下一跳可复用同 addr → 误判 B）；与不匹配 Revoke 叠加更糟。
- **Start/End 崩溃 / End 失败**：留 PROCESSING；无恢复（路径 2 相关）。
- **读者短暂不可读**、**`client_id` 改写**、**soft pin TTL 不随 lease 续**、**版本 / gate 升级注意**：见 D2 §2。
- **上层接流**：OPEN，下一步另议。

## 跨 PR

- A16：OPEN、上线前必修、不挡合入（#81 口径；`check_promote` / `promote`）。
- A06 / A17 / 多 flow 分发：DEFER。
- R13（`pin(HARD)` 拒绝）在本 PR。

## 合入前删除清单

- [ ] 本 PR 的 OPEN / DEFER 已关闭或明确接受
- [ ] `git rm kvcm/design/pr73-soft-pin-tracker.md`
- [ ] 最后一个 commit 说明“删除 soft pin 临时跟踪文档”
- [ ] PR 描述注明“临时跟踪文档已删除”

## 维护日志

- **2026-11-04**：从 core/demote tracker 拆出；补 gate 版本自授 lease→Case C 升级耦合。
- **2026-11-09**：#81 合入后 cherry-pick 到 `main`；soft pin 有条件可用；A16 OPEN。
- **2026-11-09（方案 1 + A14）**：收尾策略落地；D2 登记路径 2/3/4；EndResult/链接修复；45 机复测 PASS；本文件补全实验记录；**未 push**。
