# Agentic cache workload：用量、留存与容量输入

> 状态快照：**2026-08-14**。本文记录 Agentic workload 证据，不描述 NVIDIA CMX 架构，也不把 API provider 的 prompt cache 当作物理 KV Pool。

交互式 90%/95% 场景见 [`../../tools/cmx-sim/economics.html`](../../tools/cmx-sim/economics.html)，公式和使用说明见 [`../../tools/cmx-sim/README.md`](../../tools/cmx-sim/README.md)。

## 1. 匿名 Cursor usage trace

**[用户提供数据 + 推导]** 数据窗口为 `2026-07-20 16:03` 至 `2026-08-14 10:46`（UTC+8），来自单用户 Cursor team usage CSV：

- 553 个 usage events、8 个模型、19 个活跃日、96 个活跃小时；
- `731.0422M` total token，其中 prompt `726.4252M`、output `4.6170M`；
- prompt 中 `670.4458M` 是 Cache Read，`6.4455M` 是 Input with Cache Write，`49.5338M` 是 Input without Cache Write；
- 每行 `Total Tokens = Cache Write + uncached input + Cache Read + Output`，553 行全部一致；
- 296 行有数值 Cost，合计 `$502.65`；243 行为 `-`、14 行为 `Free`，该金额不是全部事件的经济价值；
- Cloud Agent ID / Automation ID 均为空，原始用户标识和 CSV 不入仓。

Cursor event 不是单次模型 API 请求。单个 event 最高包含 `22.0526M` prompt token，说明 Cursor 会聚合多次底层调用。input/output 分位数只能描述 event 聚合量，不能反推单请求 KV 容量、并发数、TTFT 或 provider TTL。

整体 token-weighted cache hit 定义为：

```text
h = Cache Read / (Cache Write + uncached input + Cache Read)
  = 670.4458M / 726.4252M
  = 92.2939%
```

活跃小时 total token 的 `p50/p95/max` 分别为 `4.1143M / 20.9452M / 60.3019M token/h`。最大小时发生在 `2026-08-13 17:00`（UTC+8）：5 个 events、`60.0477M` prompt、`254.2K` output、`56.6650M` Cache Read、数值 Cost `$66.58`。

完整匿名分模型汇总：

| 模型 | events | total token | cache hit | input/event p50 | input/event p95 | output/event p50 | output/event p95 | peak token/h | 数值 Cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Composer 2.5 Fast | 4 | 9.223M | 93.91% | 1.335M | 5.256M | 11.4K | 16.2K | 5.949M | — |
| Cursor Grok 4.5 High Fast | 253 | 209.077M | 95.02% | 445.9K | 2.624M | 1.9K | 18.6K | 18.877M | $73.65 |
| Cursor Grok 4.6 XHigh | 18 | 25.177M | 93.69% | 1.393M | 2.903M | 11.4K | 38.5K | 13.756M | — |
| GPT-5.5 Medium | 85 | 93.311M | 87.34% | 585.5K | 3.688M | 2.6K | 22.7K | 26.474M | $67.28 |
| GPT-5.6 Sol Max | 14 | 104.705M | 92.71% | 6.194M | 19.034M | 38.2K | 67.2K | 60.302M | $124.20 |
| GPT-5.6 Sol Medium | 11 | 6.205M | 80.94% | 84.9K | 1.829M | 1.9K | 7.6K | 2.307M | $10.76 |
| GPT-5.6 Terra Medium | 4 | 3.752M | 86.41% | 803.8K | 1.390M | 6.8K | 7.7K | 1.620M | — |
| Kimi K3 Max | 164 | 279.593M | 91.90% | 584.8K | 7.818M | 5.6K | 50.2K | 21.332M | $226.76 |

这份 trace 可提供单用户 token/h 峰值、token-weighted cache hit 和模型 mix。request arrival rate、单请求 context、KV bytes、GPU time、prefix identity/reuse distance 和存储成本仍需底层 request trace。

## 2. Provider cache 留存：合同与实测

Mempko / arXiv:2607.19214 使用 100K prefix 测量 Anthropic Sonnet 4.5、OpenAI GPT-5.1、DeepSeek V3.2、Gemini 2.5 Pro 的空闲存活与 keepalive。它是特定时点和路径的测量样本，不是 provider 的统一 TTL 合同。

| Provider | 官方口径（2026-08-14 快照） | 该实验观测 | 采用方式 |
|---|---|---|---|
| Anthropic | 默认 5 分钟；可选 1 小时 | 5–6 分钟断崖 | 5 分钟可作为合同；4 分钟 keepalive 是实验策略 |
| OpenAI | 旧模型 in-memory 通常 5–10 分钟、最长 1 小时；另有 extended 24h / 新模型策略 | GPT-5.1 在 20 分钟约一半存活，30 分钟全冷 | 只记为 GPT-5.1/该时点实测 |
| DeepSeek | best-effort；官方称闲置后通常数小时至数天清理 | V3.2 在 10 分钟全冷 | 与官方范围冲突，只能保留为路径实测 |
| Google Gemini | implicit cache 无 TTL 保证；explicit cache 默认 1 小时 | implicit 命中率在 33%–83% 波动 | 不能外推 explicit cache |

文章给出的 client-side keepalive break-even：

```text
I_max ≈ τ × (w / r − 1)
```

`τ` 是 ping 间隔，`w` 是冷 re-prefill/cache-write 相对成本，`r` 是一次 cache-read ping 相对成本。该公式用于 API 客户端是否续租，不是物理 KV block TTL。对 Pool sizing 有用的是实验产生的 pause/resume 分布和 retention-policy 输入。

Dynamo `router_ttl_secs` 也不是物理 KV TTL。它只在不消费 KV events 的 approximate indexer 中清理 Router 预测条目，不能证明 worker/storage 上的数据仍存在或已删除。

## 3. ChatGPT Pro 100 GB 是 File Library，不是 KV quota

OpenAI Help Center 当前写明 ChatGPT Pro 用户拥有 **100 GB File Library storage**。该额度承载上传文件和 ChatGPT 生成文件；它不是 prompt cache、KV cache、GPU-local cache、API context retention 或 CMX quota。

`100 GB/user` 不进入 KV sizing。若业务要设置每用户 Pool quota，至少还需定义：

- 保留多少会话/前缀；
- 每份 session 的模型、layout、token 长度和共享比例；
- TTL/热度、去重、增量快照、冗余与 usable/raw；
- quota 是 soft、hard 还是可借用。

LMCache `QuotaManager` 提供 `cache_salt → byte limit` 的动态配额接口，但没有默认 100 GB，也不能证明终端产品额度适合 KV Pool。

## 4. 仿真可用输入与缺口

90%/95% 页面使用以下 workload 输入：

- `active_sessions`：同时保留一份热前缀的 session 数；
- `retention`：新写入不可变版本的保留时间；
- `hit`：每个请求可复用的 token 前缀比例，按 block 向下取整；
- `effective_unique_tok/s/GPU × GPUs`：GPU-saturated Prefill 计算预算；
- 模型 representation、Pool 副本和 usable/raw。

页面不从 `92.2939%` token 账单命中率推导工作集。当前 CSV 没有 prefix ID 和 reuse distance，无法计算“达到 90%/95% 命中所需的最小容量”。页面采用显式工作集假设：

```text
hot_prefix = active_sessions × representation(matched_tokens)
retained_writes = compute_req/s × retention × write_bytes/request
raw_storage = copies × (hot_prefix + retained_writes) / usable_fraction
```

该公式假设每个活跃 session 保留一份命中前缀，新写入后缀在 retention 窗口内互不去重。它用于比较容量、读写 offered load 和 compute-constrained req/s，不代表真实 cache-size/hit-rate 曲线。

## 5. 参考实现与边界

- Dynamo `3rdparty/dynamo/lib/llm/src/kv_router/indexer/mod.rs::KvIndexer::new`（`PruneConfig.ttl`）：可参考 approximate metadata 的 TTL 清理形态；不能当物理 KV/provider TTL。
- LMCache `3rdparty/lmcache/lmcache/v1/distributed/quota_manager.py::QuotaManager`：可参考按 `cache_salt` 动态设置 byte limit；quota 数值仍须由 workload 推导。
- vLLM `3rdparty/vllm/vllm/models/deepseek_v4/compressor.py::CompressorStateCache`：会话续算需要固定 residual state，容量不能只算 growing KV。

关键差异：provider prompt cache、Router metadata 和自管 Pool 是三个独立生命周期。前两者的 TTL/命中观测只能作为 workload 样本，不能替代 Pool 的位置、引用和 GC 权威。

## 6. 来源

- 用户提供的 Cursor team usage CSV（2026-08-14 导出；只入仓匿名聚合）
- [Mempko：Your Agentic Workflow's Cache Keepalive Costs 8x Too Much（v2）](https://blog.mempko.com/your-agentic-workflows-cache-keepalive-costs-8x-too-much-v2-the-interval-frontier/)
- [Keeping the Cache Warm Pays（arXiv:2607.19214）](https://arxiv.org/abs/2607.19214)
- [Anthropic Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache)
- [Google Gemini Context Caching](https://ai.google.dev/gemini-api/docs/generate-content/caching)
- [OpenAI Help：ChatGPT File Library storage limits](https://help.openai.com/en/articles/20001052-file-storage-and-library-in-chatgpt/)
