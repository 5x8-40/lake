# PR73 最终审计（拆分合入，PR73 不再合并）

> **性质：临时文档，仅随 PR73 分支留档，不进入 main。**
> PR73 的功能已拆成 #80 / #81 / #82 合入 main；本文件只记录拆分后的最终状态，
> 便于关闭 PR73 时逐项核对。代码状态以 `github/main` 为准。

## 快照

| 项 | 值 |
|---|---|
| PR73 | [#73](https://github.com/5x8-40/lake/pull/73)，head `6e5786d`，base 旧 main `296f166` |
| PR73 结论 | **关闭，不再合并**；分支保留为拆分前历史 |
| 拆分 PR | #80 core / #81 demote-promote / #82 soft pin |
| 合入后的 main | `b0065a7`（#82 squash merge） |

## 代码 / 功能是否已合入

| 拆分 | 内容 | 状态 |
|---|---|---|
| #80 `5e2e120` | move / remove / admin、keepalive 拆分、A03、A07、R06 等 | ✅ 已合入 |
| #81 `ddfc4c4` | demote / promote、PromoteFlow、R05 | ✅ 已合入 |
| #82 `b0065a7` | soft pin L3、A15、A14 真机与文档 | ✅ 已合入 |

**结论：PR73 范围内的代码/功能已由 #80/#81/#82 覆盖，PR73 分支没有需要再合入的实现。**
剩余项是未实现、明确 DEFER 或已接受为文档边界的事项，不是 PR73 的待合代码。

## 已完成（代码已合入）

| 项 | 结果 | 证据 |
|---|---|---|
| A01 move 依赖 PR72 | 关闭；已 rebase 到 main | #80 基线 |
| A03 READY 前保活落账 | 关闭；READY 前 `register_await` | #80 |
| A07 partial handle rebuild | 关闭；跳过 task 行数 != 全量 plan 的 handle | #80、`test_rebuild_skips_partial_handle` |
| A08 StoreClient 并发审计 | 源码审计通过；真实压力归 A14 | #73 审计 |
| A02 / A10 / A13 remove 全局删与 move source | 关闭；remove 不支持 target，force 只跳 lease；move source 必填 | #80 |
| A15 地址复用误判 | 关闭；旧/新 replica id 判定，id 同只 End、id 异才 Revoke | #82 |
| R05 promote 落点前置校验 | 关闭；L3 promote 不带 target，target 路径用 copy + `check_promote` | #81 |
| R06 段名 vs transport endpoint | 关闭；启动核对 `--instance-segs` 与 `/get_segments_details` | #80 |
| R07 / R10 / R11 / R12 / R13 / A09–A13 / R17 | 已修 | #80/#81、backend/loops/capability/tests |

## 仅文档标记 / 已接受

| 项 | 状态 | 说明 |
|---|---|---|
| R01 `has_read_lease` gate | N/A（目标版本） | v0.3.13.post1 无该 gate；0.3.14-pre+ 才有 |
| R08 target 不是保活维度 | N/A | lease 是 key 级，保活按前缀窗口，所有副本一起冷 |
| R02 部分 key 失败不回滚 | 已接受 | 逐 key 循环，前面已钉上的保持；错误消息列出成功 key |
| R03 `put_step=1` | 已接受 / 真机覆盖 | 代码拒绝非单 slice；A14 用该路径完成 225 put → 45 soft_pin |
| R04 手写 ABI 镜像 | 已接受残余 | 无运行时 ABI 证明；升 wheel 必须重编 `_empty_pin.so` 并做行为验证 |
| A04 NoF | N/A | 当前无 NoF 场景；NoF put 本环境未配通 |
| R14 一进程一种 TP 组合 | 已接受边界 | daemon 只建一个 KeyComputer；多组合需多 backend |
| R15 capability 是声明不是探测 | 已接受约束 | 启动不探 master；版本由文档约束 |
| R16 保活请求量未算 | 已接受边界 | 每 5s 逐前缀全 key `batch_is_exist`；量级未测 |
| R18 8KB 查询分批未校准 | 已接受边界 | 只算请求行，不含 header；按目标版本实测再改 |
| Start/End 崩溃 | 已知边界 | 可能留 PROCESSING，无恢复 |
| 读者短暂不可读 | 已知边界 | `UpsertStart` → `UpsertEnd` 窗口 |
| `client_id` 改写 | 已知边界 | 空写归 kvcm；UUID 未持久化 |
| soft pin 续期 | 已知边界 | `GrantReadLease` 不续 soft pin；30min 后需重新空写 |
| L2 调用方 | 不在本 PR 范围 | prefetch READY 后是否/何时 pin 由后续策略决定 |
| 版本依赖 | 已文档化 | 只承诺 v0.3.13.post1；0.3.11/0.3.12 不兼容；0.3.14-pre+ 有 lease gate |

## 未做 / DEFER

| 项 | 状态 | 说明 |
|---|---|---|
| A06 move/remove 事件 | DEFER | 等 E14 发布端合入后接 |
| A16 同前缀串行化 | DEFER | 全局闸不是前缀互斥；prefetch/demote/remove/pin 可并发；R09/A05 归入此项 |
| A17 操作粒度 / 多 flow | DEFER | copy 核验、demote、move/remove 未统一成 flow；TaskEngine 多 flow 分发未做 |
| retain | 未实现 | `backend.retain` 直接 `NotImplementedError`；Release/RetainLoop 仅骨架 |
| InstanceWatcher | 未接 | 实例目录只做启动登记 |
| 限速器 E15 | 未接 | 只有接口，daemon 未接线 |
| 多模型 rebuild 归属 | 未做 | rebuild 未按 `model_id` 过滤；单实例单模型无问题 |
| A14 并发压测 | 未做 | A14 已覆盖 ABI/Case A/B/NoF N/A/段名/partial rebuild；并发 RPC 与 End 失败注入仍未做 |
| A08 真机并发压测 | 未做 | 源码审计通过；真实并发压力归 A14，当前未跑 |

## A14 真机验证

- 已覆盖：`_empty_pin.so` 的 `EndResult`/sizeof、`buffer_address_`/`size_`、Case B、删 key 后新 id、NoF N/A、段名/端点核对、partial rebuild。
- 未覆盖：End 失败注入、并发 RPC 压测、多 key 部分失败自动化、地址复用注入（id 判据下不再必要）。
- `probe_upsert_memory_ids` 为临时脚本，未入仓；native 测试在 CI 无 `_empty_pin.so` 时 skip。

## PR73 处置

1. PR73 不再合并；保留分支作为拆分前历史。
2. 不把本 tracker 合入 main；它只随 PR73 分支留档。
3. 关闭 PR73 时注明：已拆入 #80/#81/#82，tracker 仅临时文档。
4. 后续工作按 A06 / A16 / A17、retain、InstanceWatcher、E15、多模型 rebuild 分别开项，不在 PR73 继续。

## 维护日志

- **2026-11-10**：按 #80/#81/#82 合入后的 main 逐项复核；A15/R05/R06/A03/A07 关闭；A14 更新为“部分验证”；A06/A16/A17 仍 DEFER；PR73 标记为关闭。
