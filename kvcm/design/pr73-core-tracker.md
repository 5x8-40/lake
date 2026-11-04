# KVCM core PR 临时评审跟踪（合入前必须删除）

> **性质：临时文档。** 只用于本 stacked PR 的评审；**合入前必须删除**，最后一个 commit `git rm` 本文件，PR 描述注明“临时跟踪文档已删除”。
>
> 代码状态以 git 为准；未决项在本文，已解决项只留结论和证据。完整历史见 `backup/pr73-pre-rebase-20261104`（`9661169`）和 `wip/kvcm-ops-rebased`。

## Stack

| PR | 分支 | base | 内容 |
|---|---|---|---|
| core | `feat/kvcm-ops-core` | `github/main` | move/remove/admin、实例目录、prefetch/TaskEngine/daemon、keepalive 拆名、A07 |
| demote/promote | `feat/kvcm-demote-promote` | core | demote/promote/check_promote、PromoteFlow、lease/promote 参数 |
| soft pin | `feat/kvcm-soft-pin` | demote/promote | native 空写、pin(SOFT)、NoF 收尾 |

## Scope

**本 PR 包含**

- L3：move / remove、keepalive 拆成 `register_keepalive` / `unregister_keepalive` / `renew_once`、exists/query/abandon；`demote` / `promote` / `pin` 仍是基类 unsupported。
- L4：StoreClient copy/move/remove/exists/query；admin batch query / 段容量准入；实例目录；`backend.move/remove`、keepalive、核验、A07 rebuild。
- L2/运行时：prefetch、TaskEngine、daemon、EventPublisher（预热 READY 补发）。
- 设计文档：D2 / code-architecture / summary 全量保留（设计先行；后续 PR 只补实现和测试）。

**本 PR 不包含**

- demote / promote / `check_promote` / `PromoteFlow` / `--promote-hits`。
- native 空写、`client.soft_pin`、`backend.pin`、soft pin capability。
- 上述功能的测试和 tracker；见 stacked PR 的 tracker。

## 版本基线

- mooncake 目标版本：**v0.3.13.post1**，master / client 同版本。
- 代码不写版本号、不按版本分叉；版本要求只在文档里约束。
- v0.3.13.post1：有 `SoftPinAction`；没有 `has_read_lease` gate；`/batch_query_keys` 有 `disk_values` / `local_disk_values`。
- main / v0.3.14 有 `has_read_lease` gate，**不在本 PR 目标内**。

## 评审摘要（core 部分）

### 评审基线（Step 1）

- 分层：L2 只调 L3；L3 不出现 seg / lease / client_id / PoolKey / rank。
- 八项操作：copy / move / keepalive / pin / demote / promote / retain / remove；READY 只给 copy / move / promote，retain 跟做法走。
- Ledger 四表；rebuild 恢复 copy / move；keepalive 注册表不可丢。
- E11：单事件循环 + 协程 + RPC executor + Ledger 单线程；E12：SubmitIntent / GetTask。
- E13 / E14 / E15 未做；ReleaseLoop / RetainLoop 骨架；限速器只留接口。

### 设计改动（Step 2，core 相关）

- **事实修正**：`/batch_query_keys` 主线零副作用；copy/move 核验改走 admin；0.3.11 admin 查询有续租副作用。
- **语义变更**：remove 总是先停保活；keepalive 拆成 register/unregister/renew_once；`--admin` 缺省从 master host 推 `:9003`；`--instance-segs` 做启动登记。
- **新约定**：intent 只覆盖预热；move/remove 事件推迟到 E14；Release/Retain 只留骨架。

### 设计判断（Step 3，core 相关）

| 原判断 | 结论 | 证据 |
|---|---|---|
| remove 可以按 target 删 | mooncake 没有可用接口；remove 只做全局删；force 只跳 lease，不跳 replication task | D2 §4 / F6② |
| keepalive 按 target | lease 是 key 级，target 不是保活维度；保活按前缀窗口 | D2 F9 / R08 N/A |
| admin 短回/NoF 当不存在 | 短回、`ok=false`、坏描述符都转 `QueryError`；NoF 与 DRAM memory 分离 | D2 F11b / R07/R10/A11/R17 |
| `move` 可以不传源 | 必须显式指定 source；接口和实现都必填 | D2 F7 / A10 |
| retain 靠 retained client | `1fc27b6` 没有 `AddReplicaForRetainedClient`；按 PR74 四条路 | D2 §4 |
| capability 是运行时探测 | 是显式声明，不做版本探测；版本要求由文档约束 | R15 / capability.py |

### 代码地图（Step 4）

| 文件 | 职责 |
|---|---|
| `kvcm/ops/base.py` | L3 八操作接口 + query_task / exists / abandon；默认 Unsupported |
| `kvcm/backends/mooncake/backend.py` | L4 适配：copy/move/remove、keepalive、核验、Ledger、rebuild |
| `kvcm/backends/mooncake/client.py` | store 绑定封装：copy/move/remove/query/exists |
| `kvcm/backends/mooncake/admin_main.py` / `http_admin.py` | 主线只读 admin：batch_query_keys / tenant_quota；段容量准入 |
| `kvcm/flows/prefetch.py` | L2 预热编排 |
| `kvcm/loops.py` | KeepaliveLoop；Release/Retain 骨架 |
| `kvcm/task_engine.py` | intent 队列 + 传输闸 + READY 后保活/补发事件 |
| `kvcm/daemon.py` | 进程壳：装配、启动、优雅退出 |
| `kvcm/ledger.py` / `instances.py` | sqlite 状态；实例→seg 目录 |

### 代码 vs 设计（Step 5，core 相关）

| 检查项 | 结果 | 证据 |
|---|---|---|
| L2 只调 L3；L3 不出现 seg/lease/client_id | ✅ | `flows/prefetch.py`、`task_engine.py` |
| move READY + source 必填 | ✅（rebase 后 `_admit_new_bytes` 已接上） | `backend.move`、PR72 |
| keepalive 是周期续租 | ✅ 拆成 register/unregister + renew_once | `ops/base.py`、`loops.py`、`backend.py` |
| remove 全局删 | ✅ 不支持 target | `backend.remove`、D2 §4 |
| admin 零副作用核验 | ✅ 走 `/batch_query_keys`；R17/R18 边界保留 | `backend._verify_missing` |
| READY 只给 copy/move/promote | ✅ 空任务号抛 `ValueError` | `ops/base.py`、`backend.query_task` |
| Ledger rebuild 恢复 copy/move | ✅ A07 后 partial handle 跳过 | `backend.rebuild`、`test_rebuild_skips_partial_handle` |
| InstanceWatcher / EventPublisher / Release / Retain / limiter | ⚠️ 已知缺口 | 范围 / 已知缺口 |
| 多模型 rebuild 归属 | ⚠️ 未按 model_id 过滤 | 范围 / 已知缺口 |

### 代码问题与已解决（Step 6，core 相关）

| 项 | 结论 | 证据 |
|---|---|---|
| A01 move 依赖 PR72 | 已 rebase 到 main，A01 关闭 | `6a227c7` |
| A07 partial handle rebuild | rebuild 跳过 task 行数 != 全量 plan 的 handle | `test_rebuild_skips_partial_handle` |
| A08 StoreClient 并发 | 源码审计：`RpcClientPool::send_request` + `hot_cache` 锁；未加全局锁；真机压测归 A14 | 本期结论 |
| R07/R10/A09/A10/A11/A13/R17 | 已修；分别见 D2、backend、loops、capability 与测试 | git 历史 / 编码 |

## 剩余问题（core）

### 1. 重启 / 保活

- **A03 保活注册（已修）**
  - `TaskEngine._on_ready` 改 `await KeepaliveLoop.register_await`：对外
    `_finish(READY)` 之前先等名单写入结束。
  - 写入失败仍不抛、只计数——预热成功，副本可能提前变冷（语义保留）。
  - 消掉的是「READY 已可见、行还在路上」的空窗，不是写失败本身。
- **R16 保活量未算（OPEN）**
  - `KeepaliveLoop.sweep_once` 每 5s 逐前缀 `renew_once`（全 key `batch_is_exist`）。
  - 请求量/延迟未测量；长前缀可能拖慢 sweep。
  - 方向：测量、限流、分批或按前缀错峰。

### 2. admin / 部署

- **R06 段名 vs transport endpoint（已修）**
  - 启动时 `GET /get_segments_detail`，要求每个 `--instance-segs` 值在
    master 上 `segment_name == te_endpoint == 配置值`；对不上直接启失败。
  - `_verify_missing` 仍用配置名比副本 `transport_endpoint`；契约由启动门禁保证。
- **R18 8KB 分批依据（OPEN）**
  - `_MAX_BATCH_QUERY_REQUEST_BYTES = 8192` 只算请求行，不含 header；上游实际限制未校准。
  - 方向：按 master 实际限制校准，或改 POST。
- **R14 一进程一种 TP 组合（OPEN，文档边界）**
  - `daemon.py` 只建一个 `KeyComputer`；`--tp-size` / `--peer-tp-size` / `--put-step` 是进程级。
  - 多组合需要多 backend，文档写清即可。
- **R15 capability 是声明不是探测（OPEN，文档约束）**
  - `--capability` 按 flag 选能力集，启动不探 master。
  - 版本由文档约束，不做运行时探测/分叉；是否给通用启动项加警告另议。

### 3. 范围 / 已知缺口

- `retain` 未实现：`backend.retain` 直接 `NotImplementedError`（D2 §4 已登记四条路）；`ReleaseLoop` / `RetainLoop` 只有骨架。
- intent 只覆盖预热；其他操作没有 intent（E13 / E14 / E15 未做）。
- `InstanceWatcher` 未接；实例目录只做启动登记。
- `EventPublisher` 已接通：prefetch READY 后按目标实例补发 `BlockStored`；move/remove 事件等 E14（A06）。
- 限速器只有接口，daemon 未接线（E15 接入点）。
- `rebuild` 不区分任务归属：多模型/多后端共用一个 Ledger 时会按本实例 KeyComputer 错算；单实例单模型无此问题，多模型前必须加 `model_id` 归属判定。
- 提交并发默认 4 是进程级；单机窗口实验 #16 的结论，跨机 #17 仍半挂，未纳入 PR 验收。

### 4. DEFER

- **A06 move/remove 成功后不发事件（DEFER）**：发布端在 E14。
- **A16 同前缀操作串行化（DEFER）**：TaskEngine 全局闸不是前缀互斥；R09/A05 归入此项；外部 offload 竞态也不能完全挡。
- **A17 操作粒度未统一（DEFER）**：copy 查询+核验仍在 `query_task`；move 无 MoveFlow；demote/remove 在 L3 内 sleep+核验。

## 合入门禁

1. **core 基线**：自测全绿；`move` / `remove` / keepalive / prefetch / admin 核验可用。
2. **admin**：R06 已修（启动核对）；R18 写进文档或后续修。
3. **保活**：A03 已修（await 落账）；R16 写进文档或后续修。
4. **跨 PR**：A06 / A16 / A17 继续 DEFER；PR2/PR3 的 tracker 各自记录。

## 合入前删除清单

- [ ] 本 PR 的 OPEN / DEFER 已关闭或明确接受
- [ ] `git rm kvcm/design/pr73-core-tracker.md`
- [ ] 最后一个 commit 说明“删除 core 临时跟踪文档”
- [ ] PR 描述注明“临时跟踪文档已删除”

## 维护日志

- **2026-11-04**：从全量 PR73 tracker 拆出 core 部分；A01/A07/A08 已关闭；demote/promote 与 soft pin 移到后续 stacked PR。
- **2026-11-04**：A03 关闭：`register_await` + `_on_ready` await；写失败仍不改判预热失败。
- **2026-11-04**：R06 关闭：启动拉 `/get_segments_detail`，要求配置 seg = segment_name = te_endpoint。
