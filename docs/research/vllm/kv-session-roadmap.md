# vLLM #48168 / #48501 — KV 与 Session 调度专文

> **上游**：  
> - [#48168](https://github.com/vllm-project/vllm/issues/48168) `[Roadmap] vLLM Roadmap Q3 2026`（open）  
> - [#48501](https://github.com/vllm-project/vllm/issues/48501) `[RFC]: Session-centric KV-cache orchestration over typed session identity`（open, RFC）  
> **调研快照**：2026-07-24 · submodule `3rdparty/vllm` @ `f3e9497e9`（文档旧注 `ab132ee98` 亦可对读）。  
> **相关基线**：[overview.md](overview.md) · [compute.md](compute.md) · [block-lifecycle.md](block-lifecycle.md) · [pain-points.md](pain-points.md)。  
> **范围**：只深挖 **KV / 缓存 / session 调度 / 控制面指令**；Flat Model、Omni、CI 等非本专文重点（#48168 清单里仅点名）。  
> **对照**：SGLang agentic 总路线见 [`../sglang/agentic-kv-roadmap.md`](../sglang/agentic-kv-roadmap.md)。

## 0. 一句话

#48168 把 Q3 押在 **生产级 agentic + 多层/分布式 KV**；#48501 给出引擎侧最小原语——**`session_id` + `continuation_id` 两个不透明坐标**：事件上可见、指令上可寻址，**策略全在控制面（llm-d / Dynamo）**。这与 lake「引擎=执行、池/控制面=权威」同向，但 vLLM 仍默认 **per-instance 账本 + 旁路 indexer**，未做池化 HBM。

---

## 1. #48168 — 与 KV/调度相关的 Q3 条目

### 1.1 Key takeaway（与本专文相关）

- Q2：核心性能 + disagg + KV offload 成熟度。  
- Q3：**生产 agentic**、高交互 premium tokens；生态（量化/投机/RL）与 OSS 维护另线。

### 1.2 SIG Core（引擎内 KV/调度地基）

| 条目 | 含义 | submodule |
|------|------|-----------|
| Scheduler refactoring | 调度抽象重做，为 agent/多层 KV 腾接口 | 进行中；无「完成」标记 |
| KV Cache Manager redesign | 承认 `KVCacheManager`/`BlockPool` 单实例账本撑不住大规模 | **partial**：已有 `KVCacheCoordinator` / hybrid 分型；**非**集群权威重设计 |
| Model Runner V2 / Flat Model | 计算面迁移（非本专文） | 见 #41286 / #42770 |
| Cold start #48193 | 启动时延 | 略 |

**锚点**：`v1/core/kv_cache_manager.py::KVCacheManager` · `kv_cache_coordinator.py::KVCacheCoordinator` · `v1/core/sched/scheduler.py`。

### 1.3 SIG Large Scale Serving（KV / agent / 路由）

| 条目 | 设计意图 | 状态（相对 HEAD） |
|------|----------|-------------------|
| SOTA AgentX + disagg + KV offload + PD recipes | 产线配方 | 工程推进中 |
| **Production-ready distributed + multi-tier KV offload** | 把已有 `kv_offload` 推到分布式/产线 | `kv_offload/` **present**；「分布式生产就绪」open |
| Long context / CP | 上下文并行 | 模型矩阵进行中 |
| **Agent-oriented Prefix Caching Policies** | 见下节 1.3.1 | RFC/未落地 |
| **KV Events in P2P caches (Mooncake)** | 对等 KV + 事件 | Events **present**；P2P tier **partial**（`tiering/p2p/`） |
| **KV Events for tiered offloading** | offload 层也发事件 | **partial**：`OffloadingEventsTracker` 等 |
| PD + KV offload recipes | 配方 | 进行中 |
| Elastic EP / async scale / fault recovery | 弹性 | 略 |
| AMD parity（RCCL/RDMA + Mooncake events） | 硬件 | 略 |

#### 1.3.1 Agent-oriented Prefix Caching（#48168 原文）

两句拆开：

1. **Agent hints**：`Session-ID` / `Correlation-ID`，让引擎理解多轮 agent 与 subagent。  
2. **Targeted customization**：调度与 KV 管理定制——**prefix cache prefetch、eviction、selective offloading**。

实质落地依赖：

| 依赖 | 角色 |
|------|------|
| [#48049](https://github.com/vllm-project/vllm/issues/48049) / [#48048](https://github.com/vllm-project/vllm/pull/48048) | 一等 `session_id` 进请求（Dynamo 路由可读；**不含**调度策略） |
| [#48501](https://github.com/vllm-project/vllm/issues/48501) | `continuation_id` + 事件回显 + 指令坐标化 |
| [#37003](https://github.com/vllm-project/vllm/issues/37003) | Retention API（优先级/TTL，**不硬 pin**）——指令族第一刀 |

**HEAD**：`session_id`/`continuation_id`/`agent hint` **absent**；已有 streaming session（`Request.resumable` / `_update_request_as_session`）≠ agent KV 坐标。

### 1.4 SIG Spec / Quant（KV 相关点到为止）

- Spec：sliding-window / sparse / hybrid drafting × **quantized KV**；高 accept length。  
- Quant：**KV-cache compression** 进产线（FP8/NVFP4/INT2/4…）× hybrid × disagg × **tiered offload**。

与 lake：压缩是池内可选编码；调度不因此拥有 KV。

### 1.5 旁路总栈（常与 #48168 一起读）

| Issue | 与本专文关系 |
|-------|----------------|
| [#45036](https://github.com/vllm-project/vllm/issues/45036) Mooncake connector 总栈 | 分层、KV Events→编排、recompute-on-failure、layer-wise |
| [#48203](https://github.com/vllm-project/vllm/issues/48203) Layerwise/Sparse offload | 长序列 / 稀疏 attention 的 offload API |

---

## 2. #48501 — Session-centric 编排（全文结构）

### 2.1 动机（问题定义）

Agent = 有状态程序；KV = 工作内存（工具定义、跨 turn 前缀、并行分支共享、暂停续跑）。舰队级问题是 **state orchestration**：保什么、放哪、何时搬、属于哪个 program。

分工：

| 层 | 职责 |
|----|------|
| **控制面**（llm-d / Dynamo） | indexer = 集群内存图；策略（retention/offload/move/prefetch） |
| **引擎** | 物化 KV、上报所作所为、执行**无意图**指令；不理解 session 语义 |

已有缺口：

1. `#48049` 的 `session_id` 让身份可读，但 **KV 事件无 session/lineage** → 控制面要对 hash 做镜像才能关联。  
2. 暂停会话保暖、offload、prefetch 等需要 **按 session 坐标寻址的指令**（Retention #37003 是第一刀）。  
3. 仅 `session_id` 不够：**fork / 共享前缀**需要内容派生的链位置 → `continuation_id`。

应用侧已有信号（引擎**不解析**）：Anthropic `cache_control`；OpenAI `prompt_cache_key` / `prompt_cache_retention` / `previous_response_id` 等。控制面映射为坐标。

### 2.2 两个坐标（正交、都承重）

| 坐标 | 性质 | 作用 |
|------|------|------|
| **`session_id`** | 会话 scope，跨 turn 恒定（#48049） | 归属哪条会话 lineage |
| **`continuation_id`** | **内容派生**链位置；同 model + `cache_salt` 下字节相同前缀 ⇒ 相等 | fork 安全、recompute-stable 的节点地址；共享块的公共名 |

引擎对二者皆 **opaque**（只校验非空/长度）。**永不进入 block hashing**（不影响 dedup/APC）。

### 2.3 可见性：引擎 → 控制面

#### V1a — `BlockStored` 回显标签

- 在现有 `BlockStored` 上追加 `session_id` / `continuation_id`。  
- 默认 `kv_cache_report_mode="incremental"`：每次 admission 带**当次请求**标签（同 hash 可重复）。  
- `"full"`：reuse 也带 touching 请求标签 → 消费者建 **hash→sessions 多映射**（跨 session 复用可见；indexer 热重启可从流量重建）。  
- V1a：`BlockRemoved` 仍只按 hash；消费者用已见 store 标签关联。  
- Connector/offload 层事件（CPU/FS/OBJ、LMCache/Mooncake）V1a **不打标签**（缺 Request 上下文）→ 命名 follow-up。  
- 风险：每个 serving 入口必须显式穿坐标，漏了则静默丢标签。

#### V1b — 移除带 admitting continuation

- `BlockPool` 维护 `hash_to_continuation`：**首次 admission** 的 `(continuation_id, num_tokens)`。  
- `BlockRemoved` 带该 continuation + 覆盖前缀长度；共享块不带任意 `session_id`。  
- 再进一步：可选 per-request report mode `"coordinates"`——线上**不带 hash/token_ids**，只报 session 坐标覆盖（省带宽；hash 消费者勿混流）。

#### 健壮性

- 建议 `EventBatch.publisher_epoch`：引擎重启 nonce，避免 indexer 幽灵覆盖。  
- 未落地前：ZMQ `seq` 回绕作 reset（晚重连可能漏）。

### 2.4 指令：控制面 → 引擎

| 指令 | 状态 | 语义 |
|------|------|------|
| **Retention** | [#37003](https://github.com/vllm-project/vllm/issues/37003) / PR #38514 | 驱逐优先级偏置 + 可选 TTL；**禁止硬 pin**（agent 暂停时 block 无 ref，LRU 会杀前缀） |
| Offload / Move / Discard / Prefetch | roadmap 后续 | 同一坐标寻址；**不在 #48501 必做范围** |

边界：引擎只做形状校验；**gateway 铸造坐标并剥离客户端伪造**（否则 retention 可被带偏）。坐标是 identity，不是 priority/lifecycle 语义——语义留在控制面 indexer。

### 2.5 API 草图（相对 `main`）

改动面（RFC 自述）：

1. `kv_events.py`：`BlockStored`/`BlockRemoved` 追加可选字段。  
2. `block_pool.py`：三处构造 `BlockStored` 时写入 `request.session_id` / `continuation_id`。  
3. V1b：`hash_to_continuation` side map + 移除归因。  
4. 请求链：镜像 #48048——body / `X-Continuation-ID` / `vllm_xargs` → `EngineCoreRequest` → `Request`（含 Rust frontend）。  
5. `extra_args["kv_cache_report_mode"]`：`incremental` | `full` | `coordinates`。

依赖：**#48048 未合**则 `Request.session_id` 仅在其分支。

### 2.6 配套愿景文档（控制面，非引擎行为）

RFC 链到 llm-d 系列（Google Docs）：Agentic Northstar、KV-Cache Orchestration、Session-Graph、Session-Centric Affinity——描述控制面如何用坐标做 retention/offload/move 与 session-block 图。

---

## 3. 关联 RFC/PR 速查（KV/调度）

| ID | 标题 | 与 #48501/#48168 |
|----|------|------------------|
| [#48049](https://github.com/vllm-project/vllm/issues/48049) | First-class session id | 身份原语；Dynamo + 未来内部调度 |
| [#48048](https://github.com/vllm-project/vllm/pull/48048) | session_id plumbing | PR#1：只接线，**无策略** |
| [#37003](https://github.com/vllm-project/vllm/issues/37003) | Context-Aware Retention | 暂停 agent 被 LRU 杀前缀；优先级/TTL |
| [#45036](https://github.com/vllm-project/vllm/issues/45036) | Mooncake connector 总栈 | 分布式 KV + Events→路由 |
| [#48203](https://github.com/vllm-project/vllm/issues/48203) | Layerwise/Sparse offload | 长序列/稀疏 offload API |

---

## 4. HEAD 落地对照（`f3e9497e9`）

| 能力 | 状态 | 锚点 |
|------|------|------|
| KV Events（hash/medium/group） | **present** | `kv_events.py::BlockStored` / `BlockRemoved` |
| `kv_offload` 多层 | **present** | `kv_offload/base.py::OffloadingManager` · CPU/FS/Obj · `LookupResult` |
| Selective / prompt-only offload | **present** | `OffloadPolicy.BLOCK_LEVEL` · `offload_prompt_only` |
| P2P secondary tier | **partial** | `kv_offload/tiering/p2p/` |
| Streaming session（暂停槽位） | **present** | `Request.resumable` · `Scheduler._update_request_as_session` |
| `session_id` / `continuation_id` | **absent** | — |
| 事件上回显 session 坐标 | **absent** | — |
| Retention / Prefetch by coordinate | **absent** | （仅有 SWA 类 `PREFIX_CACHE_RETENTION_INTERVAL` 等无关语义） |
| Agent hint 调度 | **absent** | — |
| KVCacheManager「集群级」重设计 | **absent** | 仅有 coordinator 分型 |

---

## 5. 与 SGLang #21846 / lake 对照

| 主题 | vLLM (#48168/#48501) | SGLang (#21846 / #27574) | lake |
|------|----------------------|---------------------------|------|
| 智能归属 | 控制面 indexer；引擎机制 | Router soft hint；引擎可拒 | Gateway 意图可选；**池权威放置**（方案 Z） |
| 会话坐标 | `session_id` + 内容链 `continuation_id` | session_id / agent_hints / KvHint | 链式 `block_hash` + 可选 session scope；续跑靠池位置 |
| 可见性 | KV Events + 未来标签 | kv_events + 实验 router index | **强一致位置视图**（非最终一致旁路） |
| 缓存分层 | `kv_offload` per-instance | HiCache L1/L2 私有 + L3 | **L0–L3 归池** |
| Prefetch/Evict | 指令族（retention 先） | PREFETCH/DEMOTE/PIN | 池主动预放置 + 调度单向消费 |
| 硬 pin | 明确拒绝（#37003） | soft protect（#29173） | `ref>0` 冻结 ≠ 无界 pin |

**最强同向信号**：#48501「引擎=无策略机制、控制面=集群内存图」几乎就是 lake 拆分叙事。  
**仍分叉**：vLLM 事件流 + 外部 indexer（最终一致）vs lake 控制面内存权威；vLLM HBM 仍引擎自分配 vs 方案 Z。

---

## 6. 对 lake 的可借鉴清单

1. **双坐标模型**：scope（session）× 内容链位置（continuation）——fork/共享前缀命名比单 session_id 干净。  
2. **标签永不进 hash**：编排元数据与内容寻址解耦（与 lake 内容寻址一致）。  
3. **Retention ≠ Pin**：优先级/TTL 偏置，避免无界占满——对齐 gateway/池配额边界。  
4. **事件模式**：`incremental` / `full` / 未来 `coordinates`——控制面重建覆盖的带宽/语义权衡。  
5. **`publisher_epoch`**：引擎重启清幽灵——lake 视图失效/世代号可对照。  
6. **KV Events schema 现状**：`BlockStored` 的 `medium`/`group_idx`/`ExternalBlockHash` 已是现成事件源形态（见 [compute.md](compute.md)）。

不必照搬：旁路 indexer 作权威；per-instance `kv_offload` 当集群池。

---

## 7. 代码索引

| 概念 | 文件:符号 |
|------|-----------|
| KV 事件 | `vllm/distributed/kv_events.py`::`BlockStored` / `BlockRemoved` / `KVEventAggregator` |
| Block 池发事件 | `v1/core/block_pool.py`::`_build_block_stored_event` / `emit_cached_block_events` |
| Offload 管理器 | `v1/kv_offload/base.py`::`OffloadingManager` / `LookupResult` / `OffloadKey` |
| Offload 调度壳 | `kv_connector/v1/offloading/scheduler.py::OffloadingConnectorScheduler` |
| KV 管理门面 | `v1/core/kv_cache_manager.py::KVCacheManager` |
| 协调器分型 | `v1/core/kv_cache_coordinator.py::KVCacheCoordinator` |
| Streaming session | `v1/core/sched/scheduler.py::_update_request_as_session` |
| 请求扩展位 | `v1/request.py::Request.kv_transfer_params` / `cache_salt` |

---

## 8. 跟踪清单

| 优先级 | 链接 | 看什么 |
|--------|------|--------|
| P0 | [#48501](https://github.com/vllm-project/vllm/issues/48501) | V1a 标签落地、V1b 移除归因 |
| P0 | [#48048](https://github.com/vllm-project/vllm/pull/48048) / [#48049](https://github.com/vllm-project/vllm/issues/48049) | `session_id` 合入 |
| P0 | [#37003](https://github.com/vllm-project/vllm/issues/37003) | Retention 指令 |
| P0 | [#48168](https://github.com/vllm-project/vllm/issues/48168) | KV Manager redesign / Scheduler refactor / agent prefix 勾选 |
| P1 | [#45036](https://github.com/vllm-project/vllm/issues/45036) | Mooncake Events→路由 |
| P1 | [#48203](https://github.com/vllm-project/vllm/issues/48203) | Layerwise/sparse offload |

---

*专文覆盖 #48168 中 KV/调度/agent 缓存相关勾选，以及 #48501 全文结构；非 KV 的 SIG（Omni/CI 等）从略。*
