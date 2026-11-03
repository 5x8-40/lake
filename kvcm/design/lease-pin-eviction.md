# Mooncake lease / soft pin / hard pin / 驱逐 × kvcm 对照

- 本文解决什么：把 Mooncake master 里三套保护机制和驱逐规则说清楚，再对照 **kvcm tip（`feat/kvcm-ops-mainline`）** 实际用了哪几条、没碰哪几条。
- 不替代 [D2-kv-operations.md](D2-kv-operations.md)：机制细节与方案取舍仍以 D2 事实基线（F1 / F8 / F9 / F10）与 §2 pin 为准；本文是导读对照。
- 参考实现：`docs/research/mooncake/kv-store.md`（`ObjectMetadata` / `GrantLease` / `IsSoftPinned`）；源码锚点 `master_service.cpp::BatchEvict`、`GrantLease`、`UpsertStart`（@0.3.11 / @main）。LMCache 有独立 `pin`/`unpin`（`StorageBackendInterface`），与「空写复用 Upsert」不同——关键差异见 D2 参考表。

## 1. Mooncake：三套机制 + 驱逐

三套都挂在 **key 元数据**上，护的是这把 key 的**全部副本**，不是某一个 seg。

| | lease（读租约） | soft pin | hard pin |
|--|--|--|--|
| 是什么 | 「最近有人在用」的短窗 | 「尽量别逐我」的限时标记 | 「绝对别逐」的永久标记 |
| 默认时长 | ~5s（0.3.11）/ ~10s（main）；测试床可配（如 11s） | ~30min；main 可单次覆盖，上限 24h | **无到期** |
| 怎么来 | 读/存在查询顺带授（`ExistKey` / `GetReplicaList` 等） | 写入或 upsert 的 `ReplicateConfig`（main：`SoftPinAction` 三态） | **只能创建 put 时设**；之后改不了 |
| 驱逐角色 | **硬过滤**：未过期 → 不进候选 | **软过滤**：先逐无 pin；不够且允许时才逐有 pin | **硬过滤**：永不进候选 |
| 排序 | **是**——两轮都按 `lease_timeout` 早→晚（≈多久没被读） | **否**——只决定进第几轮 | 不参与 |

### 驱逐（`BatchEvict`）在干什么

```text
对每个 key：
  hard pin？        → 跳过
  lease 未过期？    → 跳过
  副本 refcnt>0？   → 该副本跳过（copy/offload 等任务在途；不是读者锁）

候选：
  第 1 轮：无 soft pin，按 lease_timeout 从早到晚
  不够 且 allow_evict_soft_pinned_objects（默认 true）：
  第 2 轮：有 soft pin，同样按 lease_timeout
```

要点：

1. lease 未过期与 hard pin 同属「本轮不碰」。
2. soft pin 不是「不逐」，是「最后才逐」。
3. 同轮谁先死看 **lease 到期早晚**，不看 soft pin 还剩多久。
4. 新 put 的 key **出生无租约**——池紧时最容易先被逐。

### 续租怎么搅在一起

一次 `GrantLease`（如 `ExistKey` / `BatchExistKey`）：

- 总是续 `lease_timeout` → `now + default_kv_lease_ttl`
- 若 key **已有 soft pin**，再顺带把 `soft_pin_timeout` 推满（只延不缩）

没有「只续 soft pin、不续 lease」的独立 RPC；也没有「立刻撤销 lease」——只能停续、等过期。`ProbeKey`（main）不续租。

### soft pin 配置形态（版本差）

- 0.3.11：`with_soft_pin` 布尔；忘带 true 的 upsert 会清掉已有 pin。
- main：`SoftPinAction`——`PRESERVE`（默认不动）/ `ENABLE` / `DISABLE`。

## 2. kvcm tip：实际对上哪几条

```text
Mooncake            kvcm tip
────────            ────────
lease        ←───   KeepaliveLoop + exists（batch_is_exist）
                    pin(HARD) = 登记保活（不改 hard pin 位）
                    demote / remove(force=False) 先停保活再等满 lease TTL

soft pin     ←───   pin(SOFT) 空写 ENABLE/DISABLE（_empty_pin.so）

hard pin 位  ←───   （未用；事后改不了）
```

### 组件对照

| Mooncake | kvcm 组件 | tip 行为 |
|----------|-----------|----------|
| lease 续租 | `exists` → `batch_is_exist` | 读即续 lease；已 soft pin 时顺带续 soft TTL |
| lease 保活登记 | `keepalive` / `stop_keepalive` + Ledger | 只加/删保活行；真正续租靠 `KeepaliveLoop` |
| lease 周期续 | `KeepaliveLoop` | 默认约 5s 一轮调 `exists`；须显著小于 master lease TTL |
| lease 等待过期 | `demote`、`remove(force=False)` | 先 `stop_keepalive`，再睡满 `--lease-ttl-s`（默认 10，须与 master 一致） |
| soft pin 事后加/解 | `pin(SOFT, enable=…)` | admin 取唯一 `size_` → 有盘拒空写 → `UpsertStart`/`UpsertEnd`，不写数据 |
| hard pin 位 | — | **不碰**；`hard_pin_after` 通道 NO |
| 「硬钉」产品语义 | `pin(HARD)` | 仅 `keepalive()`；保护强度 = lease 窗，不是 hard pin 位 |
| 能力声明 | `mooncake_main` / `mooncake_v0311` | tip 默认 main；`--capability v0311` 启动失败。soft 通道仅 main YES |

### soft pin 空写前置（有盘拒空写）

- **谁有盘**：admin `/batch_query_keys` 里该 **key** 的 `disk_replicas > 0` 或 `local_disk_endpoints` 非空。
- **谁拒绝**：`MooncakeBackend.pin(SOFT)` 在进 `_empty_pin.so` **之前**整次失败。
- **为什么**：Upsert 会把已完成副本标 PROCESSING；`UpsertEnd(ALL)` 收尾盖不回 LOCAL_DISK / DISK。

### 和预热怎么串

1. 预热 READY → `TaskEngine` 登记保活 → Loop 续 lease → 短期内不被逐。
2. 需要「排到第二轮、更长」→ 另调 `pin(SOFT)`（可与保活并存）。
3. API `pin(HARD)` → 只加强/确保保活登记，池里仍是 lease。
4. 降层/温和删除 → 必须停保活并等 lease 过期，否则硬过滤一直跳过。

## 3. 常见混淆

| 说法 | 实情 |
|------|------|
| `pin(HARD)` = Mooncake hard pin | **否**。tip = 登记保活续 lease。 |
| soft pin = 绝对不逐 | **否**。第二轮仍可逐（默认允许）。 |
| 保活 = soft pin | **否**。保活走 lease；soft pin 要空写。 |
| 停保活立刻可删/可降层 | **否**。须再等满一个 lease TTL（无撤销 RPC）。 |
| `exists` 与 `ProbeKey` 等价 | **否**。前者续租；后者（main）不续。 |

## 4. 仍未解决的 tip 拍板（与本对照相关）

完整列表见 PR #73 评论「待拍板」。与本文直接相关的：

1. 已有读租约且副本全是内存时，空写仍可能走 Upsert Case C（重分配、中间不写数据）。
2. 一批 key soft pin 半途失败，已成功的不回滚。
3. `pin(HARD)` 窗口约一个 lease TTL；重复预热不刷新已有行的 `registered_at`。
4. 保活表只有前缀、无实例维度；同前缀多目标时停保活会互相踩。

## 5. 代码索引（kvcm tip）

| 概念 | 位置 |
|------|------|
| `pin` / `keepalive` / `demote` / `exists` | `kvcm/backends/mooncake/backend.py` |
| 空写扩展 | `kvcm/native/empty_soft_pin.cpp`、`mooncake_pin_api.h` |
| 保活循环 | `kvcm/loops.py::KeepaliveLoop` |
| 能力集 | `kvcm/backends/mooncake/capability.py` |
| 操作语义 | `kvcm/ops/base.py`；方案正文 [D2 §2](D2-kv-operations.md) |
