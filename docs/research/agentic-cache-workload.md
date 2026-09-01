# Agentic cache workload：用量、留存与容量输入

> 状态快照：**2026-08-31**。本文记录 Agentic workload 证据，不描述 NVIDIA CMX 架构，也不把 API provider 的 prompt cache 当作物理 KV Pool。

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

仿真默认使用 trace 中占 token 最多且已有 KV 布局模型的 Kimi K3。其长期聚合为 164 个 events、`255.158901M` Cache Read、`22.481047M` uncached input，token-weighted hit 为 `91.902805%`。Kimi 峰值小时是 `2026-08-05 15:00`（UTC+8）：5 个 events、`1.083487M` uncached input、`20.131868M` Cache Read、`116.710K` output、`21.332065M` total token。5 个 events 仍不能解释为 5 个模型请求。

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

这份 trace 可提供单用户 token/h 峰值、token-weighted cache hit 和模型 mix。request arrival rate、单请求 context、KV bytes、GPU time、prefix identity/reuse distance 和存储成本仍需底层 request trace（§2/§3 的公开数据集补上了一部分）。

## 2. 公开 request 级 trace：Codex × SWE-bench Pro

**[公开数据集 + 官方分析]** vLLM 团队与 Inferact 采集了 Codex / GPT-5.4 在 SWE-bench Pro 上的真实 agentic trace 并开源（[`Inferact/codex_swebenchpro_traces`](https://huggingface.co/datasets/Inferact/codex_swebenchpro_traces)）；分析方法与结论见 [vLLM blog 2026-05-06](https://vllm.ai/blog/2026-05-06-mooncake-store)。与 §1 的 event 级账单 trace 不同，这是 **request 级**数据：每次 LLM 调用的 input/cached/uncached/output token、turn 序号与调用间隔都可直接统计。

数据集自报统计（610 个成功 trial、20,230 次 LLM 调用）：

| 维度 | 数值 |
|---|---|
| 每 trial LLM 调用数 | 均值 33（P50 30 / P90 57 / P99 90） |
| 全量 cache hit | **94.2%**（1,301.5M / 1,382.3M input token） |
| input : output | 131 : 1 |
| cache : 新增 input | 16.1 : 1 |
| 单次调用 input | 均值 68,329（P50 63,917 / P90 114,888 / P99 166,322） |
| 单次调用 uncached | 均值 3,991（P50 758 / P90 8,736 / P99 53,323） |
| 单次调用 output | 均值 520（P50 246） |
| 上下文增长 | 首轮 ~12.4K → 末轮均值 84.5K（P99 180.9K）；均值 2,242 token/turn（P50 880） |
| trial 时长 | 均值 336.8 s（P50 273.7 s）；调用间隔均值 10.5 s（P50 5.2 s / P99 81.4 s） |

对仿真有直接参考价值的三个结构事实：

- **turn 级命中率单调上升**：turn 1 平均 87.4%，turn 7 起稳定 ≥94%，turn 40 达 98.1%。会话越深，新算占比越小。
- **跨 trial 共享前缀**：93.8% 的 trial 首轮调用即命中（命中的是其他并发 trial 写下的 system prompt 前缀；主共享组 11,520 token、覆盖 568/610 个 trial），命中者首轮平均命中 93.2%。
- **uncached 计算重尾**：top 1% 调用占 20.5% 的 uncached prefill 计算量，top 10% 占 64.2%。按均值估计计算需求会低估尾部长 prefill。

blog 同时给出本地 offload 的两个结构性限制（即分布式 KV 池的动机）：单实例容量/驱逐（100K token 上下文约 GB 级，如 Kimi-2.5 FP8 约 3.8 GB）与跨实例 miss（负载均衡把会话后续 turn 调度到没有该前缀的实例）。其合成扩展负载（20K 公共 token + 10K 首轮输入 + 2,048 token/turn × 30 turn、900 output、output/input ≈ 1.3%）可作为仿真输入形状复用。

限制：单一 agent 框架（Codex）与单一任务族（SWE-bench Pro）；`cached_tokens` 是 provider 账单口径，不证明物理 KV 的留存位置或 TTL。

## 3. AgentX v1.0（SemiAnalysis InferenceX）

**[公开基准 + 方法论]** [AgentX](https://inferencex.semianalysis.com/zh/agentx) 是 SemiAnalysis 的 agentic 负载回放基准（Apache 2.0）。与 §2 的原始 request 级 trace 不同，AgentX 是可回放基准：匿名化会话结构 + 确定性重建 + 统一测量协议，用于横向比较 serving 系统。v1.0 于 2026-06-21 构建，含 393 个自愿采集的 Claude Code 会话、135,282 个请求，匿名化方法与 Qwen-Bailian 类似；发布数月内合作伙伴以其为衡量标准向上游引擎提交了 50+ 优化 PR。资料入口：[测试方法](https://inferencex.semianalysis.com/agentx/methodology)、[InferenceXv3 发布文](https://newsletter.semianalysis.com/p/agentx-inferencexv3-does-cuda-moat)、Hugging Face 数据集（[full](https://huggingface.co/datasets/semianalysisai/cc-traces-weka-062126) / [256k](https://huggingface.co/datasets/semianalysisai/cc-traces-weka-062126-256k)，WEKA 格式，AIPerf 可读）。

### 3.1 数据集概况

筛选规则：会话 ≥20 个请求、Claude Code ≥2.1.139、同时运行 subagent ≤10；剔除完全重复请求、安全监控/标题生成的短 classifier 调用、重建后 input 超 990K token 的请求。

| 维度 | full 变体 | 256k 变体 |
|---|---|---|
| 会话数 | 393（175 个含 subagent，44%） | 同左 |
| 请求数 | 135,282（其中 main+subagent turn 98,827） | 68,266（移除超限请求，保留相对时间与 subagent 重叠） |
| 请求/会话 | 中位 86，均值 251.5 | 中位 70，均值 173.7 |
| turn/会话 | p50 65 / p90 330 / p95 553 / max ~3.0k | p50 48 / p90 117 / p95 178 / max ~1.6k |
| cached input（token 口径） | 98% | 95% |
| 总 token | 21.7B | 6.9B |
| 上下文上限 | 1M token | 256K token |
| 模型组合（按 turn） | claude-opus-4-8 62.1k、fable-5 16.2k、haiku-4-5 8.0k、opus-4-7 6.4k、opus-4-6 5.5k | 同左（opus-4-8 35.9k） |
| subagent group | 1,697 个；时长中位 2.27 min（p95 18.5 min）；每会话 group 数中位 4 | 同左 |

重建保真度：全部 135,282 个请求中，重建 token 数 / 服务商 token 数之比中位数 1.004（p25–p75 围绕 1.0，官方注明不代表逐请求固定误差上限），长度维度无系统偏差。

### 3.2 负载分布

口径说明：**会话** = 一次完整 Claude Code 任务；**请求** = 单次 LLM 调用，分 main agent（主链）、subagent（支链，带 join gate）、辅助（一次性侧支调用，不带 join edge，主链不等它；语义 trace 未标注，安全监控/标题生成类已剔除）三类；分布表的 **turn** = main + subagent 请求，不含辅助请求。cached/uncached 按 64-token block hash 的会话内前缀复用计算，是**逻辑复用率**（cache 无限保留时的上限），不是物理命中；物理命中见 §3.3。各分位是同一分布的不同统计量，不同行的 max 不属同一请求。

逐 turn 分布（full n=98,827，256k n=68,266；p50 / p75 / p90 / p95 / max）：

| 维度 | full 变体 | 256k 变体 |
|---|---|---|
| input/turn | 142.0k / 310.5k / 549.5k / 682.9k / 989.8k | 88.8k / 148.8k / 204.3k / 228.2k / 255.8k |
| output/turn | 444 / 1.1k / 2.7k / 4.3k / 59.9k | 376 / 849 / 1.8k / 3.3k / 59.9k |
| uncached input/请求 | 1.7k / 2.9k / 6.8k / 14.7k / 949.4k | 1.6k / 3.1k / 8.7k / 21.2k / 244.2k |
| cached fraction/turn | p50 99%，p75 起 100% | p50 98%，p75 99% |
| subagent 请求 ISL | 64.0k / 119.7k / 196.3k / 259.5k / 918.5k（n=42,029） | 60.6k / 105.1k / 161.7k / 196.5k / 255.8k（n=39,822） |
| subagent 请求 OSL | 328 / 640 / 1.3k / 2.0k / 59.9k | 330 / 637 / 1.2k / 1.9k / 59.9k |

时间维度：轮间延迟（主要为 tool 执行时间）中位 3.84 s，约 10% 超过 1 min（多为等待人工介入）；ISL/OSL 与轮间延迟近似 log-normal。发布文另给出全部 DeepSeek V4 回放 run 的请求分布（ISL p50 88k / p90 272k / p95 404k / p99 675k，OSL p50 413 / p90 2.2k / p95 3.7k / p99 8.6k）；closed-loop 回放从会话 25%–75% 处起播、按系统速度推进，run 级分布与语料全量口径不同。

两点解读：

- per-turn cached fraction 中位 99%（full），与 §2 Codex trace 的 turn 级命中率单调上升同向；uncached input 中位仅 1.7k token/请求，新算量集中在长尾（p95 14.7k、max 949.4k）。
- 分布近似 log-normal，可用对数正态拟合做仿真采样；但分布受 harness 注入上下文量影响（Claude Code 偏多，Pi 等偏少），换 harness 需重新标定。

### 3.3 实测命中率

InferenceXv3 各配置 run 的实测值。理论命中率由负载决定（§3.2 逻辑复用率），实测与理论的差距来自系统实现：

| 配置 | 并发客户端 | HBM hit | DRAM hit | 说明 |
|---|---|---|---|---|
| B300 vLLM DEP8 + 3TB DRAM offload | 384 | 91% | 1.36% | HBM KV 工作集 ≈43M token，负载刚好不超出 |
| B200（其余配置相同） | 196 | 73% | ≈20% | HBM 工作集 ≈22M token，约为 B300 一半 |
| GB200 TP4/EP4/DP-attention | 32 | 28.8%（理论 96%） | — | 机制见下 |
| ATOM 稀疏 checkpoint 保留修复前 → 后 | 48 | 5.6% → 96.45% | — | 机制见下 |

两处大差距的机制，发布文有明确归因：

- **GB200 DPA：放置分片 + 路由无亲和**。DP-attention 下每个 DP rank 只持有池的 1/4 私有分片，路由不按 cache 位置选 rank；会话后续请求落到不含前缀的 rank 即全量重算（原文："Each DP rank owns a private quarter of the pool; a 300k-token session re-landing on the wrong rank recomputes everything"）。KV 在集群内存在，但对本次请求不可达。对应修复是 SGLang 的 DP cache affinity（会话粘到持有其 cache 的 rank）。
- **ATOM：保留/驱逐策略**。prefix 匹配实际找到了，但混合注意力模型恢复前缀需要 sliding-window 尾部 checkpoint，保留策略不留它，匹配被丢弃（原文："the cache was not missing but being overruled"；window-gate 丢弃率 91.35% → 0.16%）。修复是选择性保留 window 尾部 checkpoint。

容量规划的实测输入：HBM KV 工作集 B300 DEP8 ≈43M token、B200 ≈22M token；DRAM offload 为 write-through，官方经验法则 DRAM 容量需为 HBM 的 1.5–3×。

### 3.4 回放方法

- **匿名化保 prefix 结构、不保内容**：input 按 64-token block 转为会话内串联 hash（重复 block ID = 共享 prefix），AIPerf 回放前用确定性合成 coding/tool-use token 填充。客户端不可观测的字段（服务端 chat template、专有 tokenizer、加密 reasoning、图片/文档展开后的 token 数）用确定性 placeholder + 按模型 padding 处理。
- **subagent 归属依赖 Claude Code 两个新 header**：`x-claude-code-agent-id` / `x-claude-code-parent-agent-id`（[anthropics/claude-code#49207](https://github.com/anthropics/claude-code/issues/49207)，Workflow 工具 fan-out 由 [#66761](https://github.com/anthropics/claude-code/issues/66761) 扩展）。没有这两个 header，N 个并发 subagent 在代理侧与 N 个无关会话不可区分，spawn/join 结构无法恢复。
- **DAG 重建语义**：主 agent 请求成线性链；subagent 链在符合条件的父请求完成后 spawn，在下一个依赖它的主请求前经 join gate 汇合；一次性辅助请求无 join edge 独立运行。trace 只记请求时间戳与分支 ID，不记触发分支的 tool 级事件——回放保留请求顺序、分支重叠与轮间延迟，不推断服务端内部因果。
- **closed-loop 并发语义**：concurrency 指并发 agent 客户端数，不是固定 request batch；subagent fan-out 使服务端瞬时请求数可高于客户端数。更快的系统在同一小时内推进到会话更后位置，实际请求组合随系统速度略变（低并发时最明显）——跨系统比较的是同一场景定义下的曲线，不是同一批请求。

测量协议（P7 校准可逐项对照）：固定 seed 在每段会话记录时长的 25%–75% 区间均匀选起点 → `max_tokens=1` primer 物化主 agent 与 subagent 的活跃前缀 → 每条回放 lane 再完成 10 个 warmup 请求 → 测量 barrier 开启；对外指标只统计随后 1 小时 profiling 窗口；每次循环使用唯一 cache-bust 标记，避免无关回放轮次间形成共享 prefix。报告指标为 output throughput/chip、p90 interactivity、TTFT、ITL、cache 行为与 serving 成本，要求吞吐与延迟同时给出。两个配套规则：合成 token 的 draft 接受率与自然输出不同，acceptance length 按（模型 × speculator × draft length × thinking mode）取 SPEED-Bench coding 类实测值，记录在带版本 golden 文件，引擎侧强制 acceptance 控制已合入 SGLang/TRT-LLM/vLLM/ATOM；DRAM offload 对非标准配置服务器上限 3 TB，标准系统按实际装机，且每种配置只能按 GPU 占比使用对应 host DRAM。

### 3.5 对仿真的价值与限制

价值：会话拓扑（subagent DAG + join gate）与轮间延迟的公开重建方法；WEKA trace 公开可下载，上表之外可自行统计任意维度；64-token block 串联 hash 与 LMCache `ChunkedTokenDatabase`、本系统 radix block 哈希同构；primer + warmup + cache-bust + closed-loop 是 P7 性能校准可直接借鉴的协议；256k 变体适合受限 context 对照。subagent fan-out 的 KV 压力形态需单独验证：单个 turn 可同时拉起多个短生命周期 subagent，各自分配 KV、快速结束，cache 压力呈尖峰而非稳态（1,697 个 group、时长中位 2.27 min 是定量尺度），按均匀请求流调优的调度与驱逐策略在该模式下行为不同。

限制：合成 payload 不能评估模型质量；block ID 仅会话内有效，跨会话 prefix identity 不可得（与 §2 Codex trace 的跨 trial 共享前缀互补）；trace 不含服务端内部转换，重建长度有按模型 padding 的近似成分。

## 4. Provider cache 留存：合同与实测

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

## 5. ChatGPT Pro 100 GB 是 File Library，不是 KV quota

OpenAI Help Center 当前写明 ChatGPT Pro 用户拥有 **100 GB File Library storage**。该额度承载上传文件和 ChatGPT 生成文件；它不是 prompt cache、KV cache、GPU-local cache、API context retention 或 CMX quota。

`100 GB/user` 不进入 KV sizing。若业务要设置每用户 Pool quota，至少还需定义：

- 保留多少会话/前缀；
- 每份 session 的模型、layout、token 长度和共享比例；
- TTL/热度、去重、增量快照、冗余与 usable/raw；
- quota 是 soft、hard 还是可借用。

LMCache `QuotaManager` 提供 `cache_salt → byte limit` 的动态配额接口，但没有默认 100 GB，也不能证明终端产品额度适合 KV Pool。

## 6. 仿真可用输入与缺口

仿真把可核实的 trace 数据与场景假设分开：

- **Trace preset**：Kimi K3、平均 hit `91.902805%`、峰值 `21.215355M prompt token/h`；
- **模型假设**：默认 1 Mi-token context、Kimi FP8 MLA + vLLM KDA state；
- **部署假设**：GPU 数、unique token/s/GPU、active prefix、Pool 副本和 usable/raw。

request 级公开 trace（§2/§3）补齐的输入与仍缺的项：

- **已可得**：单请求 input/cached/uncached/output 分布、turn 级命中率曲线、inter-call delay 分布（≈ KV 需存活的空闲间隔）、跨会话共享前缀比例（Codex trace）；逐 turn cached fraction 与 uncached 分布、会话拓扑（subagent DAG + join gate）、轮间/tool 执行延迟、closed-loop 回放协议与实测 HBM 工作集（AgentX，WEKA trace 公开可下载）。
- **仍缺**：request arrival 过程（Codex trial 内调用是串行的，trace 不含多会话并发到达）；prefix identity 与 reuse distance（AgentX block hash 仅会话内串联，跨会话被 cache-bust 隔离）；KV bytes 仍需模型 layout 换算（见 cmx-sim）；provider 物理 TTL 仍无观测。

KV 加载页可以直接用峰值 prompt token/s 和平均 hit 计算 token-proportional growing-KV load：

```text
cache_read_token/s = peak_prompt_token/s × average_hit
growing_KV_load = cache_read_token/s × growing_bytes/token
```

该口径不需要 request 数，但也不能计算 KDA fixed-state load。固定状态每次 resume/load 计一次，必须有底层 request/resume rate；因此默认完整 KV load 和 observed req/s 显示 `N/A`。

90%/95% 页面使用以下场景输入：

- `active_sessions`：同时保留一份热前缀的 session 数；
- `hit`：每个请求可复用的 token 前缀比例，按 block 向下取整；
- `effective_unique_tok/s/GPU × GPUs`：GPU-saturated Prefill 计算预算；
- 模型 representation、Pool 副本和 usable/raw。

页面不从 `91.902805%` 或整体 `92.2939%` token 账单命中率推导工作集。当前 CSV 没有 prefix ID 和 reuse distance，无法计算“达到 90%/95% 命中所需的最小容量”。页面默认采用 per-prefix 工作集：

```text
hot_prefix = active_sessions × representation(matched_tokens)
retained_writes = 0
raw_storage = copies × (hot_prefix + retained_writes) / usable_fraction
```

默认 `active_sessions=1` 只表示 per-prefix 归一化，不是 trace 实测并发。用户可选择计入 retention 窗口内的新写入；启用后才假设窗口内版本互不去重。该模型用于比较容量、P 侧 KV load 和 compute-constrained req/s，不代表真实 cache-size/hit-rate 曲线。

## 7. 参考实现与边界

- Dynamo `3rdparty/dynamo/lib/llm/src/kv_router/indexer/mod.rs::KvIndexer::new`（`PruneConfig.ttl`）：可参考 approximate metadata 的 TTL 清理形态；不能当物理 KV/provider TTL。
- LMCache `3rdparty/lmcache/lmcache/v1/distributed/quota_manager.py::QuotaManager`：可参考按 `cache_salt` 动态设置 byte limit；quota 数值仍须由 workload 推导。
- vLLM `3rdparty/vllm/vllm/models/deepseek_v4/compressor.py::CompressorStateCache`：会话续算需要固定 residual state，容量不能只算 growing KV。

关键差异：provider prompt cache、Router metadata 和自管 Pool 是三个独立生命周期。前两者的 TTL/命中观测只能作为 workload 样本，不能替代 Pool 的位置、引用和 GC 权威。

## 8. 来源

- 用户提供的 Cursor team usage CSV（2026-08-14 导出；只入仓匿名聚合）
- [Inferact/codex_swebenchpro_traces（Hugging Face 数据集）](https://huggingface.co/datasets/Inferact/codex_swebenchpro_traces)
- [vLLM blog：Serving Agentic Workloads at Scale with vLLM × Mooncake（2026-05-06）](https://vllm.ai/blog/2026-05-06-mooncake-store)
- [AgentX 测试方法与数据集（SemiAnalysis InferenceX）](https://inferencex.semianalysis.com/zh/agentx)
- [AgentX 完整测试方法（methodology）](https://inferencex.semianalysis.com/agentx/methodology)
- [A Brief Overview of Agentic Workloads（InferenceX blog）](https://inferencex.semianalysis.com/blog/brief-overview-of-agentic-workloads)
- [AgentX – InferenceXv3 发布文（SemiAnalysis Newsletter）](https://newsletter.semianalysis.com/p/agentx-inferencexv3-does-cuda-moat)
- [AgentX v1.0 数据集：cc-traces-weka-062126（full）](https://huggingface.co/datasets/semianalysisai/cc-traces-weka-062126) 与 [256k 变体](https://huggingface.co/datasets/semianalysisai/cc-traces-weka-062126-256k)
- [Mempko：Your Agentic Workflow's Cache Keepalive Costs 8x Too Much（v2）](https://blog.mempko.com/your-agentic-workflows-cache-keepalive-costs-8x-too-much-v2-the-interval-frontier/)
- [Keeping the Cache Warm Pays（arXiv:2607.19214）](https://arxiv.org/abs/2607.19214)
- [Anthropic Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache)
- [Google Gemini Context Caching](https://ai.google.dev/gemini-api/docs/generate-content/caching)
- [OpenAI Help：ChatGPT File Library storage limits](https://help.openai.com/en/articles/20001052-file-storage-and-library-in-chatgpt/)
