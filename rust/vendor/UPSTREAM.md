# Vendored upstream pin

本目录是 [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo) 的 **in-tree vendor**（非 git submodule），供 lake P4 KV Pool 复用（见 [#20](https://github.com/chengda-wu/lake/issues/20)、`docs/research/3rdparty-reference.md`「代码级复用策略」B）。

| crate | 上游路径 | 许可 |
|-------|----------|------|
| `kvbm-logical` | `lib/kvbm-logical/` | Apache-2.0（见各 crate `LICENSE`） |
| `dynamo-tokens` | `lib/tokens/` | Apache-2.0（见各 crate `LICENSE`） |

## Pin

| 字段 | 值 |
|------|-----|
| Upstream | https://github.com/ai-dynamo/dynamo |
| Commit | `f5b1c1cceaee8374e3e6134f43f8aa1a0a225f9c` |
| 对应本地 submodule | `3rdparty/dynamo` @ 同上 SHA（vendor 时一致） |
| Vendor 日期 | 2026-07-21 |

## 工具链 / MSRV

- vendor crate 使用 **`edition = "2024"`**（上游 let chains）。
- **钉死工具链**：`rust/rust-toolchain.toml` → `1.96.1`（与 `3rdparty/dynamo/rust-toolchain.toml` 对齐）；CI 用 `dtolnay/rust-toolchain@1.96.1`，**不用**滚动 `@stable`。
- 本地若仍是旧 toolchain（无 2024 edition），`cargo build -p kvbm-logical` 会直接失败——`rustup toolchain install 1.96.1`（或按该文件自动安装），勿降 edition 回写上游语义。
- lake 业务 crate 仍可为 `edition = "2021"`；仅 vendor 成员要求 2024。

## CI / clippy

- **业务 crate**：`cargo clippy --workspace --all-targets --exclude kvbm-logical --exclude dynamo-tokens -- -D warnings`。
- **vendor 排除 `-D warnings`**：本树约定业务源码不改；滚动 lint / 新 stable 若 deny 进 vendor，会逼改 fork 或堆 `allow`。排除后 lint 洁净度与「我们的 CI」解耦；vendor 正确性靠下方单测门禁。
- **vendor 单测**：`cargo test -p dynamo-tokens -p kvbm-logical`（约 500，含 proptest）在 P4.1 起作为**每 PR** rust job 门禁，锁「业务源码未改 + 构建接入仍绿」。P4.2 链业务依赖后若嫌慢，可改 label / `schedule` 触发，勿静默删门禁。

## 本树相对上游的改动

### P4.1（构建接入）

`Cargo.toml`：去 workspace 继承、填实版本、改 path；`edition = "2024"`；`dynamo-tokens` → `path = "../dynamo-tokens"`。见下节偏差表。

### P4.2（最小源码改造，#20）

| 项 | 上游 | vendor | 说明 |
|----|------|--------|------|
| `InactiveIndex` + `MultiLruBackend` + `LineageBackend` | `pub(crate)` | `pub` + crate root re-export | **仅** lake controlplane 所需（见下「为何不 pub Lru/Fifo」） |
| `LeafPolicy::Frequency` + `LineageBackend::with_frequency` | 注释中的 planned third arm | **已实现** | TinyLFU 分档驱叶子；Authority 主路径 |
| `on_node_inserted(idx, seq_hash)` | 仅 `idx` | 增 `SequenceHash` | Frequency 需要；Fifo/Tick 忽略 |
| `mark_present` / `mark_absent` | `pub(crate)` | `pub` | 无 BlockStore 时由 CP 标 presence |
| `LogicalLayoutHandle` | `G1..G4` | `L0..L3` | lake 统一编址；vendor 内原无引用 |
| `EventsManager` | 可选挂 registry | **仍保留模块**；lake **不接线** | 物理删除字段留后续小 PR |
| `check_presence` 注释 | store-shadow | 注明 lake 同锁线性 | 实现未改 |

其余 `src/` 业务逻辑与上游 pin 一致（除上表）。

#### Authority 驱逐主路径（组合，非互斥二选一）

上游 `InactiveBackendConfig` 里 MultiLru **与** Lineage 互斥（单槽 `Box<dyn InactiveIndex>`）。lake 要 `ref>0` 冻结 + LFU-Aging + 前缀亲和，故走上游预留扩展点：

- **主路径**：`LineageBackend::with_frequency` = 只驱叶子（≈前缀亲和强形式）+ `LeafPolicy::Frequency`（TinyLFU 冷叶优先 ≈ LFU-Aging）
- **`MultiLruBackend`**：仍 **pub**，作对照/单测；Authority **不再单独挂**它
- 加权软亲和（非叶子也可驱但权重极高）defer；P4.2 用结构约束近似
- **inactive 上界（对齐 Dynamo 路径拆分）**：上游 `release_primary` → `inactive.insert` 与 `allocate_atomic` → `inactive.allocate` **分离**；固定槽使 insert 不会顶破 MultiLru cap。lake 无 `BlockStore` → Authority 人工 `INACTIVE_CAP`：`report_ref` 满容 **只 skip insert、不摘视图**；压力驱逐只走显式 `evict_n`（及后续生产 allocate）。`FrequencyPolicy::on_leaf_added` 仍对齐 MultiLru 超容 `debug_assert`（禁静默踢 leaf → 视图僵尸）
- **TinyLFU 分档时机**：与上游 MultiLru 一致——leaf 进入 inactive 时按当时 count 入档，**存续期间不随 Lookup touch 重分桶**；升温只影响「尚未入 inactive / 再次 0→正→0 再入」的路径

#### 为何不 pub `LruBackend` / `FifoReusePolicy` / `HashMapBackend`

lake 冷热策略已定（见 `docs/architecture/kv-cache-pool.md`）：`ref>0` 冻结 + **LFU-Aging** + **前缀亲和**。

| vendor backend | 语义 | lake P4.2 |
|----------------|------|-----------|
| `LineageBackend` + `Frequency` | 叶子约束 + TinyLFU 分档 | **主路径**（`with_frequency`） |
| `MultiLruBackend` | TinyLFU 分档 + 档内 LRU（无树约束） | **pub**，对照；非 Authority 默认 |
| `LineageBackend` + Tick/Fifo | 只驱叶子 + 简单序 | BlockManager 默认仍 Tick |
| `LruBackend` | 纯 LRU | 保持 `pub(crate)` |
| `FifoReusePolicy` / `HashMapBackend` | FIFO + HashMap 槽位复用 | 保持 `pub(crate)`——绑 `BlockStore`；lake 薄驱动不用 |

不是 LRU/FIFO「没用」，而是当前策略不需要它们当一等公民；crate 内仍供 Dynamo `BlockManager` 使用。以后若另开策略再按需提 `pub`。连带少改动，也避开 `InactiveBlock` 等经 public API 泄漏触发的 `private_interfaces`（CI `-D warnings`）。

## Cargo.toml 相对上游的版本/feature 偏差

填实依赖时对齐 **lake workspace 风格** 与上游 crate 意图，而非逐字复制 dynamo workspace 根：

| 依赖 | 上游 | vendor | 说明 |
|------|------|--------|------|
| `tokio` | `=1.48.0` + `full` | `"1"` + `rt-multi-thread/macros/net/signal/sync/time` | 与 lake 根 `tokio` 同风格；显式含 `sync`/`time`，不靠 `tokio-stream` 间接补齐。dev-deps 仍 `features = ["full"]`。 |
| `bytes` | crate 局部 `"1.10"`（覆盖根 `"1.9"`） | `"1.10"` | 对齐上游 crate 局部约束（曾误填根值 1.9，已改）。semver 解析结果通常与 lock 一致。 |
| `tracing-subscriber` | workspace `"0.3"`（dev） | **省略** | 上游/vendor 源码均无引用，属死依赖；曾误填 `"0.1"`（像抄了 `tracing` 版本号），已删除而非改成 0.3。 |

其余填实版本（`dashmap`/`parking_lot`/`prometheus`/…）与上游 workspace 根定义对齐。re-vendor 时保留上表策略，勿盲目改回 `=1.48.0`+`full`。

## Re-vendor 约定

1. 更新 `3rdparty/dynamo` 到目标 commit（或从上游检出同等树）。
2. 同步拷贝 `lib/kvbm-logical` → `rust/vendor/kvbm-logical`、`lib/tokens` → `rust/vendor/dynamo-tokens`（保留 lake 侧 `Cargo.toml` 填实版本与 path 依赖；冲突时以「能编过 + 单测绿」为准手工合并）。
3. 刷新本文件的 Commit / Vendor 日期；`LICENSE` 若上游变更一并更新。
4. `cd rust && cargo test -p dynamo-tokens -p kvbm-logical` + `cargo fmt --check` + 业务 crate clippy（`--exclude` vendor，见上节）全绿后再提交。


## LICENSE 说明

各 crate 与 `rust/vendor/LICENSE` 使用**纯 Apache-2.0 全文**（附 NVIDIA 版权说明）。
已去掉上游根 `LICENSE` 开头针对 `lib/llm/tests/data/deepseek-v3.2` 的 NOTICE——vendor
树不含该测试数据，避免误导。

上游 `lib/kvbm-logical/AGENTS.md`（指向 Claude Code 规则的 symlink）不纳入 vendor，
与 lake「忽略 submodule 自带 `.claude`/agent 规则」约定一致。
