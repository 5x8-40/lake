# vLLM × SGLang — Agent 场景下的 KV Cache

> **素材**：[知乎专栏《Sglang 和 vllm 深度比较：从 kv cache 展开到推理框架》](https://zhuanlan.zhihu.com/p/2082926853241696898)第 6 节（四问题框架与对比叙事）；论断已按代码快照逐条核实，过时标注见 §3 纠正。  
> **代码快照**：2026-09-28 · `3rdparty/vllm` @ `027b6f3a2` · `3rdparty/sglang` @ `55cc90b533`。  
> **上游 issue**：vLLM [#37003](https://github.com/vllm-project/vllm/issues/37003)（Retention API）· [#51428](https://github.com/vllm-project/vllm/issues/51428)（KvHint）· SGLang [#27574](https://github.com/sgl-project/sglang/issues/27574)（Programmatic KV）· [#36224](https://github.com/sgl-project/sglang/issues/36224)（KvHint 信封）。  
> **相关**：[../vllm/kv-session-roadmap.md](../vllm/kv-session-roadmap.md)（vLLM 侧落地细节）· [../sglang/agentic-kv-roadmap.md](../sglang/agentic-kv-roadmap.md)（SGLang 侧落地细节）· [../model-routing.md](../model-routing.md)（实例级路由）。

## 0. 一句话

Agent 场景下 KV 从「请求结束即释放的临时资源」变成跨 turn / 跨时间 / 跨 Worker 的会话状态。SGLang 把会话语义做进**引擎内**的 radix 树（locality 直接是树拓扑），vLLM 把块级事实流抛给**外部**控制面重建索引（block state → event → external index）——分歧不在要不要 radix 拓扑，而在它住引擎内还是引擎外。

## 1. 场景与失效模式

Agent session = 多 turn + 工具调用停顿（秒到分钟级，不产生新 KV）+ 分支（共享前缀 + 独立 delta）+ 暂停/恢复 + 跨 Worker 迁移 + 分层驻留（GPU/Host/远端 KV Store）。

失效模式的量化（[#37003](https://github.com/vllm-project/vllm/issues/37003) 原文）：

- 典型 agent turn 超 **90%** token 是上一 turn 前缀的逐字复用（prefix 命中）；
- **40–60%** 会话墙钟时间处于工具调用暂停态，此时 KV 无引用，并发负载下被其他 agent 的 LRU 驱逐；
- resume 时全量重算可增长到 **70K–200K** token 的上下文。

「LRU 不足」的旁证（#37003 引用）：Alibaba 生产 trace——10% 的 KV 块占 77% 复用，workload 感知驱逐降 41.9% 平均时延（arXiv:2506.02634）；Continuum——TTL retention 降 1.12–3.66× 延迟（arXiv:2511.02230）；KVFlow——workflow 感知打分 2.19× 加速（arXiv:2507.07400）；MARCONI——成本感知驱逐 34.4× 命中率、71.1% 更低 TTFT（arXiv:2411.19379）。

## 2. 四问题框架

| 问题 | 语义 | 对 cache 系统的要求 |
|------|------|---------------------|
| ① KV 应在哪个 Worker | session 亲和 + prefix 感知路由；路由错 = 全量重 prefill | 状态可被 router 感知 |
| ② session 停顿怎么办 | 跨 turn 保留 + 分层卸载 | 生命周期支持 session 级引用 |
| ③ branch 怎么办 | 共享前缀 + 独立 delta 的树形复用 | 拓扑支持分支 |
| ④ 能否主动说「保留这个」 | passive cache → programmable cache | 暴露控制接口 |

### 问题①：KV 在哪个 Worker

- **SGLang**：引擎内 `UnifiedRadixCache` 是真实 KV 拓扑（`default_radix_cache_factory` 的 fall-through 默认路径）；router（`sgl-model-gateway`）的 `cache_aware` 策略按请求历史为每个 Worker 维护**近似 radix 树**，预测各 Worker 前缀命中率选点，负载严重不均时退回最短队列（`policies/cache_aware.rs`）。近似树 ≠ 真实树——prefix locality 被显式建模为路由策略的输入，而不是 router 直读真实树。
- **vLLM**：引擎只承诺暴露 block 级事实流（KV Events：`BlockStored` / `BlockRemoved` + `parent_block_hash`）；prefix/session 视图由外部系统重建——Dynamo 的 KV Router 消费 KV events，其 KvIndexer 内部就是一棵 radix 树；llm-d 是 KVEvents → KV-Block Index → Prefix Index 两层架构。

读数：**radix 拓扑对 prefix 管理不可避免**——vLLM 生态的控制面在外部重造了它。SGLang 更直接（locality → routing），vLLM 更分层（block state → event → index → routing），代价是最终一致性与重建带宽。

### 问题②：session 停顿

- **SGLang（已落地）**：`--enable-session-radix-cache`。session 对前缀持 **Session Reference**——介于「请求锁（正在用，不可回收）」与「无引用（最先驱逐）」之间的**软保护**。实现：`UnifiedSessionRefTracker`（`register_session_ref` / `release_radix_session`，generation + 关闭 tombstone 防陈旧引用）+ 驱逐侧 session 分区（`session_ref>0` 节点经 `_session_lru_predicate` 延后驱逐）+ `/close_session` HTTP 端点。叠加 HiCache 形成 session → 引用 → 驱逐优先级 → 驻留层（GPU/Host/L3）的完整链条。**soft protection ≠ pin**：内存极端不足时 referenced 仍后被驱逐。
- **vLLM（未落地）**：#37003 Retention API——token 区间指令 `RetentionDirective{start, end, priority 0-100, duration}` + 两结构 evictor（现有 LRU 队列不动，带优先级的块进 min-heap，lazy TTL 过期）+ `retention_scope`（任意 scope 可升优先级，仅属主可降/清）。issue 自称有工作实现，截至快照未合 main。

### 问题③：branch

两边都胜任，机制同源：SGLang radix 树的分叉是原生操作（共享前缀 = 共享路径，branch = 子树，delta = 后代节点，无需额外元数据）；vLLM 的链式 block hash（`parent_block_hash`）同样表达分支拓扑。差异仍在「拓扑住哪」，不在表达能力。

### 问题④：可编程 cache（passive → programmable）

hint 动词表（两社区共用一套 taxonomy——#51428 开篇声明「aligned with SGLang #27574」）：

| 动词 | 语义 |
|------|------|
| Pin | 有界 TTL 防普通驱逐；**不要求驻留 HBM**，可落在冷层实现 |
| Retain | 驱逐优先级偏置（refcount 归零后不立即进候选），非租约保护 |
| Prefetch | 访问到来前从 L2/L3 异步拉回热层 |
| Demote | 不销毁，主动下沉一层（HBM→Host→SSD） |
| Release / Deref | 允许回收销毁该前缀（SGLang 初始 action 名 `kv.deref`） |

核心原则（#27574 原文）：「Engine keeps ownership of scheduling and memory and is free to clip, defer, or reject any hint」——**hint ≠ command**。

**落地状态（2026-09-28 核实）**：

| 面 | vLLM | SGLang |
|----|------|--------|
| 信封 transport | **已合 main**：`v1/kv_hints/protocol.py`（`KvHintsEnvelope` / `KvHintAction`，#53421 系）；经 Python 引擎 API（`LLMEngine` / `AsyncLLM.generate` kwargs）携带，OpenAI HTTP 未接 | **已合 main**：`managers/kv_hints.py`（#36224 定稿的同构格式 + 入口校验）；`GenerateReqInput.kv_hints` 携带到 scheduler `Req`，OpenAI 端点未接 |
| action 执行器 | 无；唯一消费者是 KVCR tier（`on_new_request` → `submit_hint`） | 无；`kv.deref` / `kv.demote` / `kv.prefetch` 均未实现 |
| 配套坐标 | `session_id`（#48048）+ `BlockStored.session_id` 回显 + `kv_cache_report_mode` 已落地；Retention（#37003）未合 | session radix（问题②）已落地；Mooncake L3 retain（#30796 / Mooncake#2835）未合 |

## 3. 与文章的差异（时效性纠正）

1. 文章标 #27574 / #51428「未落地 / RFC 阶段」——**信封 transport 2026-09 已双双合入 main**；未落地的是 action 执行语义与结果回报。
2. 文章「vLLM 的 session 停顿方案还在演进」——结论不变（#37003 仍 open），但坐标与可见性已先行落地（`session_id` + 事件回显），缺的只剩 Retention 执行器。
3. 文章称 UnifiedRadixCache「v0.5.19 起全模型默认（#35081）」——版本号未核实；代码事实是它是 `default_radix_cache_factory` 的默认 fall-through（radix 开启时），例外仅 ChunkCache（radix 关闭）/ PureSWA / LMCache / FlexKV 变体。

## 4. 对 lake 的读数

- 四问题框架与 lake 拆分同向：① = 池发布位置视图、调度读视图选点；② = 引用数冻结 + 分层归池；③ = 链式 `block_hash` 原生分支；④ = 控制面意图、池执行。
- 关键差异不变：两家引擎的 session / retention 语义都绑在**引擎实例**上（树节点引用或块优先级）；lake 的引用与位置权威在池，引擎可销毁。
- 「radix 住引擎内还是引擎外」对 lake 是第三个答案：radix 归存储控制面（强一致位置视图），引擎与 router 都是读者。

## 5. 代码索引

| 概念 | 文件:符号 |
|------|-----------|
| SGLang 默认树选择 | `mem_cache/registry.py::default_radix_cache_factory` |
| SGLang session 软保护 | `mem_cache/unified_cache/session_ref_tracker.py::UnifiedSessionRefTracker` · `unified_tree_core.py::_session_lru_predicate` |
| SGLang close 端点 | `entrypoints/http_server.py::close_session` |
| SGLang router 近似树 | `sgl-model-gateway/src/policies/cache_aware.rs` |
| SGLang KvHint 信封 | `managers/kv_hints.py::KvHintsEnvelope` / `decode_kv_hints_envelope` |
| vLLM KV 事件 | `distributed/kv_events.py::BlockStored`（含 `session_id` 回显） |
| vLLM KvHint 信封 | `v1/kv_hints/protocol.py::KvHintsEnvelope` / `KvHintAction` |
| vLLM hint 消费（KVCR tier） | `v1/kv_offload/tiering/kvcr/manager.py::on_new_request` |
