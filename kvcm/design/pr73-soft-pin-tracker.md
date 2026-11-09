# KVCM soft pin PR 临时跟踪（合入前删除）

> 临时文档，只服务 `feat/kvcm-soft-pin`。合入前 `git rm`，PR 描述写明已删。
> base：已合入的 #81（`main` @ `ddfc4c4`）。本 PR **只做 L3 库能力**，**不接 L2**（TaskEngine / PrefetchFlow / intent 自动 pin）。

## Stack

| PR | 分支 | 内容 |
|---|---|---|
| #80 | 已合入 | move/remove/admin、prefetch、keepalive |
| #81 | 已合入 | demote/promote、PromoteFlow |
| soft pin | `feat/kvcm-soft-pin` → `main` | native 空写、`pin(SOFT)`、方案 1 收尾 |

## 范围

**做**

- native：`_empty_pin.so`、`mooncake_pin_api.h`、`build_empty_pin.sh`（链 `store.so` + `libmooncake_store.so`）
- `client.soft_pin` / `backend.pin`、capability、单测
- D2 §2 收尾策略与版本依赖

**不做**

- L2 接线（prefetch READY 后自动 `pin(SOFT)` 等）
- 后续路径 2/3/4（见下）
- fork mooncake

## 收尾（方案 1）

详见 [D2-kv-operations.md](D2-kv-operations.md) §2。

| 条件 | 行为 | 调用方 |
|---|---|---|
| 地址+size 匹配 | 只 `UpsertEnd` | 成功则钉上 |
| 匹配后 End 失败 | 不 Revoke；抛 `runtime_error` → Python 异常 | 本次 pin 失败；可能留 PROCESSING |
| 不匹配 | `UpsertRevoke`；抛异常 | 按 A/C；若实为 Case B 会 **Erase key**（接受） |

`pin` 路径：`exists` → `renew_once` → 再 `batch_query_keys` → 空写。不 `register_keepalive`。

备选（未做）：② 不匹配也不 Revoke；③ 池侧 only_in_place；④ 只用 keepalive。

## 对照表

| 项 | 状态 |
|---|---|
| 空写 Case B + 方案 1 | 已实现 |
| `pin(HARD)` 拒绝 | 已实现 |
| NoF 与 memory 分离；NoF-only 拒绝 | 已实现 |
| EndResult ABI + `libmooncake_store` 链接 | A14 已校正 |
| 真机正向 Case B | A14 PASS |
| L2 接线 | 本 PR 不做 |

## A14 实验（2026-11-09）

| 项 | 值 |
|---|---|
| 机 | `7.242.104.45`，容器 `vllm-ascend-lake-test` |
| mooncake | 0.3.13.post1（master+client） |
| master | `:50088`，metrics `:9003`，`default_kv_lease_ttl=11000` |
| 代码 | 容器 `/tmp/lake-a14/kvcm`（分支拷贝后本地编 so） |
| 冒烟 | `/tmp/a14_soft_pin_smoke.py`（未入仓；`StoreClient.soft_pin`） |

| 问题 | 修复 |
|---|---|
| 只链 `store.so` → `Upsert*` 未定义 | 同链 `libmooncake_store.so` |
| `EndResult` 布局错 → 误 Revoke | 按 `tl::expected<void,ErrorCode>`（ok+union） |

| 用例 | 结果 |
|---|---|
| put → pin ENABLE → `master_soft_pin_key_count` 0→1；地址不变 | PASS |
| DISABLE + get 比对 payload | PASS |
| 错地址 → 异常 + Revoke→Erase（`got_len=0`） | PASS（方案 1 残余） |

未测：NoF、跨机、并发、End 失败注入、`backend.pin` 真机（单测有）。  
单测：`pytest -k pin_soft` → 11 passed。

## 仍开着

- A15 地址复用；不匹配 Revoke 删 key
- NoF 真机、`put_step=1` 布局、ABI 无 static_assert
- End/崩溃留 PROCESSING，无恢复
- A16（#81，不挡合入）；A06/A17 DEFER

## 合入前

- [ ] OPEN/DEFER 关闭或书面接受
- [ ] `git rm` 本文件；末 commit 写明删除；PR 描述注明

## 日志

- 2026-11-04：从 demote tracker 拆出；gate 版自授 lease→Case C
- 2026-11-09：rebase #81；方案 1 + A14；文档与现实对齐（L2 明确不做）
