# lease / soft pin / hard pin / 驱逐——易混淆点澄清

- 本文只澄清 Mooncake master 里三套保护和驱逐怎么分，以及 kvcm 侧常见说法容易和它们对错号的地方。
- 机制细节与方案取舍仍以 [D2-kv-operations.md](D2-kv-operations.md) 事实基线（F1 / F8 / F9 / F10）与 §2 为准。
- 参考：`docs/research/mooncake/kv-store.md`（`ObjectMetadata` / `GrantLease`）；源码锚点 `master_service.cpp::BatchEvict`、`GrantLease`。

## Mooncake 里是三套不同的东西

都挂在 **key 元数据**上，护的是这把 key 的**全部副本**，不是某一个 seg。

| | lease（读租约） | soft pin | hard pin |
|--|--|--|--|
| 一句话 | 「最近有人在用」的短窗 | 「尽量别逐我」的限时标记 | 「绝对别逐」的永久标记 |
| 默认量级 | 数秒～十余秒（版本/配置可改） | 约 30 分钟级（可配） | **无到期** |
| 怎么来 | 读/存在查询顺带授 | 写入或 upsert 时带配置 | **只能创建 put 时设**；之后改不了 |
| 驱逐里 | **硬过滤**：未过期 → 不进候选 | **软过滤**：先逐没 pin 的；不够才逐有 pin 的 | **硬过滤**：永不进候选 |
| 同轮排序 | **按 `lease_timeout` 早→晚**（≈多久没被读） | 不参与排序，只决定进第几轮 | 不参与 |

### 驱逐在干什么（`BatchEvict`）

```text
hard pin？ / lease 未过期？ / 副本任务 refcnt>0？  → 跳过

第 1 轮：无 soft pin，按 lease 到期早晚挑
不够 且允许逐 soft pin（默认允许）：
第 2 轮：有 soft pin，同样按 lease 到期早晚挑
```

容易混的三点：

1. **soft pin ≠ 不逐**，只是「最后才逐」。
2. 同轮谁先死看的是 **lease 多久没被读**，不是 soft pin 还剩多久。
3. 新 put 出来的 key **出生无租约**——池紧时反而容易先被逐。

### 续租为什么容易搅在一起

一次 `GrantLease`（例如走 `ExistKey` / `BatchExistKey`）：

- 总是续 lease；
- 若 key **已经有 soft pin**，再顺带把 soft pin TTL 也推满（只延不缩）。

因此：**没有**「只续 soft、不续 lease」的独立 RPC；也**没有**「立刻撤销 lease」——只能停续、等过期。main 上的 `ProbeKey` 不续租，和会续租的存在查询不是一回事。

## kvcm 侧容易对错号的地方

下面说的是**语义分工**，不绑定某一版实现细节。

| 容易当成 | 实际 |
|----------|------|
| 保活 / 周期 `exists` = soft pin | **否**。那是在续 **lease**。 |
| API 名叫 `pin(HARD)` = 设了 Mooncake hard pin 位 | **否**（若走的是登记保活这条路径）。hard pin 位创建固化、事后改不了；保活只给 lease 短窗。 |
| soft pin = 绝对防驱逐 | **否**。第二轮仍可逐。 |
| 停保活立刻就能删 / 降层 | **否**。lease 不会被立刻撤销，通常还要等满一个 lease TTL。 |
| 保活和 soft pin 互斥 | **否**。可以并存：lease 管短窗硬过滤，soft pin 管「排到第二轮」。 |

一句话收束：

```text
lease     ←  kvcm 保活 / 读即续租 碰的是这个
soft pin  ←  要改池里的 soft pin 标记，得走 upsert 类路径（事后空写是其中一种）
hard pin  ←  创建固化；事后「硬钉」若没做读删写重建，就不是在设这个位
```

## 和 D2 的关系

- 为什么硬钉选「登记保活」而不是改 hard pin 位、soft pin 为何空写：见 [D2 §2](D2-kv-operations.md)。
- 驱逐过滤与续租同续两个时钟：见 D2 F8 / F9。
