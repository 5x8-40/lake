# vLLM × SGLang — Agent 场景下的 KV Cache

> **素材**：[知乎专栏《Sglang 和 vllm 深度比较：从 kv cache 展开到推理框架》](https://zhuanlan.zhihu.com/p/2082926853241696898)第 6 节（四问题框架与对比叙事）；本文论断以代码快照核实为准。  
> **代码快照**：2026-09-28 · `3rdparty/vllm` @ `027b6f3a2` · `3rdparty/sglang` @ `55cc90b533`。  
> **上游 issue**：vLLM [#37003](https://github.com/vllm-project/vllm/issues/37003)（Retention API）· [#51428](https://github.com/vllm-project/vllm/issues/51428)（KvHint）· SGLang [#27574](https://github.com/sgl-project/sglang/issues/27574)（Programmatic KV）· [#36224](https://github.com/sgl-project/sglang/issues/36224)（KvHint 信封）。  
> **相关**：[../vllm/kv-session-roadmap.md](../vllm/kv-session-roadmap.md)（vLLM 侧落地细节）· [../sglang/agentic-kv-roadmap.md](../sglang/agentic-kv-roadmap.md)（SGLang 侧落地细节）· [../model-routing.md](../model-routing.md)（实例级路由）。

## 0. 一句话

Agent 场景下 KV 从「请求结束即释放的临时资源」变成跨 turn / 跨时间 / 跨 Worker 的会话状态。两家引擎都能发块级 KV 事件；真正的分歧是路由视图从哪来——SGLang 的 router 用请求历史自建近似树选点，vLLM 生态的 router 消费事件流在外部重建前缀索引。前缀管理的 radix 拓扑两家都绕不开，区别只是它住引擎内还是引擎外。

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

先分清两棵树（两个进程、两个用途）：

- **引擎树**：引擎内的 radix 树只做一件事——KV cache 命中匹配，是权威拓扑。
- **router 树**：路由进程里另有一棵树，只为选点服务，是近似视图。两家都有这棵树，分歧在**它的数据从哪来**。

- **SGLang**：router 树来自请求历史。
  - 引擎内：`UnifiedRadixCache` 是真实 KV 拓扑，也是 radix 开启时的默认实现（`default_radix_cache_factory` 的 fall-through；例外只有 ChunkCache（radix 关闭）、纯 SWA、LMCache/FlexKV 变体）。
  - Router 侧（`sgl-model-gateway`）：`cache_aware` 策略按「自己路由过的请求历史」为每个 Worker 维护一棵近似 radix 树（`policies/tree.rs::Tree`），据此预测各 Worker 的前缀命中率选点；负载严重不均时退回最短队列。
  - 引擎**也有** KV 事件流：与 vLLM 同款三件套 `BlockStored` / `BlockRemoved` / `AllBlocksCleared`，从 mem_cache 层产生（`mem_cache/events.py`），经 ZMQ PUB 发布（`SchedulerKvEventsPublisher`），`/server_info` 可发现。但自家 gateway **不消费**它——事件是给外部 indexer（如 #31458 KV Indexer）准备的。
- **vLLM**：引擎只暴露事实，路由视图在外部重建。
  - 引擎承诺的只有 block 级事实流：KV Events（`BlockStored` / `BlockRemoved` + `parent_block_hash`）。
  - prefix/session 视图由外部系统重建：Dynamo 的 KV Router 消费 KV events，其 KvIndexer 内部就是一棵 radix 树；llm-d 是 KVEvents → KV-Block Index → Prefix Index 两层架构。

读数：**radix 拓扑对 prefix 管理不可避免**——vLLM 生态的控制面在外部重造了它。两家引擎都能发 block 事件，真正的分歧是 router 的视图从哪来：SGLang 自家 router 用请求历史做近似——零事件依赖、实现简单，但视图是「我路由过什么」，不是「引擎里真实有什么」（引擎内因驱逐/命中产生的变化它看不见）；vLLM 生态用事件流重建——视图贴近真实缓存状态，代价是最终一致性与重建带宽。

### 问题②：session 停顿

- **SGLang（已落地）**：`--enable-session-radix-cache`，session 对前缀持 **Session Reference**——介于「请求锁（正在用，不可回收）」与「无引用（最先驱逐）」之间的**软保护**。
  - 打标：`UnifiedSessionRefTracker` 按 `session_id` 给树节点挂引用；会话关闭后仍在跑的请求结束时可能把引用重新挂回（保护泄漏），引擎用 generation 编号 + 关闭黑名单挡住（机制细节见 [../sglang/agentic-kv-roadmap.md](../sglang/agentic-kv-roadmap.md) §2.1）。
  - 驱逐：`session_ref>0` 的节点经 `_session_lru_predicate` 延后驱逐；内存极端不足时仍可驱逐——soft protection ≠ pin。
  - 入口：`/close_session` HTTP 端点释放会话引用。
  - 叠加 HiCache 后形成完整链条：session → 引用 → 驱逐优先级 → 驻留层（GPU/Host/L3）。
- **vLLM（未落地）**：#37003 Retention API。
  - 指令形态：token 区间 `RetentionDirective{start, end, priority 0-100, duration}`，请求可携带多条。
  - 实现形态：两结构 evictor——现有 LRU 队列不动，带优先级的块进 min-heap，TTL 惰性过期。
  - 多租户：`retention_scope`——任意 scope 可升优先级，仅属主 scope 可降/清。
  - 状态：issue 自称有工作实现，截至快照未合 main。

### 问题③：branch

两边都胜任，机制同源：SGLang radix 树的分叉是原生操作（共享前缀 = 共享路径，branch = 子树，delta = 后代节点，无需额外元数据）；vLLM 的链式 block hash（`parent_block_hash`）同样表达分支拓扑。差异仍在「拓扑住哪」，不在表达能力。

### 问题④：可编程 cache（passive → programmable）

hint 动词表（两社区共用一套 taxonomy——#51428 开篇声明「aligned with SGLang #27574」）：

| 动词 | 语义 |
|------|------|
| Pin | **有界保证**：TTL 内保证不被驱逐（超配额的 pin 引擎可裁剪/拒绝）；但**不要求驻留 HBM**——可只在冷层（如 L3）留副本，GPU/Host 副本照常可驱逐 |
| Retain | **优先级偏置**：只改驱逐顺序，压力来了比低优先级块后驱逐；无任何保证 |
| Prefetch | 访问到来前从 L2/L3 异步拉回热层 |
| Demote | 不销毁，主动下沉一层（HBM→Host→SSD） |
| Release / Deref | 允许回收销毁该前缀（SGLang 初始 action 名 `kv.deref`） |

Pin 与 Retain 常被混为一谈，区别在「保证 vs 偏置」。命名上有个坑：SGLang 的 Pin POC 在 API 里叫 "retention"（沿用 OpenAI `prompt_cache_retention` / Anthropic `cache_control` 的 provider 术语），机制上却是 L3 TTL 租约——即 taxonomy 里的 Pin；#27574 自己点明了这一点。

核心原则（#27574 原文）：「Engine keeps ownership of scheduling and memory and is free to clip, defer, or reject any hint」——**hint ≠ command**。

**落地状态（2026-09-28 核实）**：

- **vLLM**：
  - 信封 transport 已合 main：`v1/kv_hints/protocol.py`（`KvHintsEnvelope` / `KvHintAction`，#53421 系）。
  - 入口：只走 Python 引擎 API（`LLMEngine` / `AsyncLLM.generate` kwargs）；OpenAI HTTP 未接。
  - 消费方：引擎自身不消费任何 action；唯一消费者是 KVCR tier（`on_new_request` → `submit_hint`）。
  - 配套坐标：`session_id`（#48048）、`BlockStored.session_id` 回显、`kv_cache_report_mode` 已落地；Retention（#37003）未合。
- **SGLang**：
  - 信封 transport 已合 main：`managers/kv_hints.py`（#36224 定稿的同构格式 + 入口校验）。
  - 入口：`GenerateReqInput.kv_hints` 携带到 scheduler `Req`；OpenAI 端点未接。
  - 消费方：`kv.deref` / `kv.demote` / `kv.prefetch` 三个 action handler 均未实现。
  - 配套：session radix（问题②）已落地；Mooncake L3 retain（#30796 / Mooncake#2835）未合。

## 3. 对 lake 的读数

- 四问题框架与 lake 拆分同向：① = 池发布位置视图、调度读视图选点；② = 引用数冻结 + 分层归池；③ = 链式 `block_hash` 原生分支；④ = 控制面意图、池执行。
- 关键差异不变：两家引擎的 session / retention 语义都绑在**引擎实例**上（树节点引用或块优先级）；lake 的引用与位置权威在池，引擎可销毁。
- 「radix 住引擎内还是引擎外」对 lake 是第三个答案：radix 归存储控制面（强一致位置视图），引擎与 router 都是读者。

## 4. 代码索引

| 概念 | 文件:符号 |
|------|-----------|
| SGLang 默认树选择 | `mem_cache/registry.py::default_radix_cache_factory` |
| SGLang session 软保护 | `mem_cache/unified_cache/session_ref_tracker.py::UnifiedSessionRefTracker` · `unified_tree_core.py::_session_lru_predicate` |
| SGLang close 端点 | `entrypoints/http_server.py::close_session` |
| SGLang router 近似树 | `sgl-model-gateway/src/policies/tree.rs::Tree` · `cache_aware.rs` |
| SGLang KV 事件 | `disaggregation/kv_events.py::BlockStored` / `BlockRemoved` · `scheduler_components/kv_events_publisher.py::SchedulerKvEventsPublisher` |
| SGLang KvHint 信封 | `managers/kv_hints.py::KvHintsEnvelope` / `decode_kv_hints_envelope` |
| vLLM KV 事件 | `distributed/kv_events.py::BlockStored`（含 `session_id` 回显） |
| vLLM KvHint 信封 | `v1/kv_hints/protocol.py::KvHintsEnvelope` / `KvHintAction` |
| vLLM hint 消费（KVCR tier） | `v1/kv_offload/tiering/kvcr/manager.py::on_new_request` |
