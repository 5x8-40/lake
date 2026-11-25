# KVCM trace 回放仿真（纯 CPU）

用公开 agentic trace 回放 KV 缓存的访问序列，不跑模型，比较三类策略在 **DeepSeek-V4.1-Flash（公开规格，890 B/token）+ 昇腾 A3 224 NPU + HBM/DRAM 两层** 设定下的命中率（HBM 归推理引擎、纯 LRU、固定真实大小；KVCM 只管 DRAM 池；x 轴只扫池容量），再用 D8a §8.1/§8.2 的 TTFT 拟合换算成 TTFT 与等效 NPU 数。对应参考文章的数据类型 1.1（MRC）、1.2（lost KV reuse）、1.3（未命中 token）、1.4（TTFT）、1.6（等效卡数）、1.12（消融），见 Project 文档《参考文章的数据类型与获取方法》。

- 参数与每项来源/假设：[`params.md`](params.md)（V4.1 模型规格取自官方 config / 技术报告；**A3 容量与 prefill 算力换算仍是假设，等待校正**）。
- 结果与图：[`results/summary.md`](results/summary.md)（由脚本生成）。

## 跑法

```bash
# 依赖：numpy、matplotlib；Codex 需要 tiktoken
pip install numpy matplotlib tiktoken
# trace 下载到 /tmp/traces（路径可用环境变量 CC256K / CODEX / QWEN_CODER 改）
curl -L -o /tmp/traces/cc256k.jsonl https://huggingface.co/datasets/semianalysisai/cc-traces-weka-062126-256k/resolve/main/traces.jsonl
curl -L -o /tmp/traces/codex.json https://huggingface.co/datasets/Inferact/codex_swebenchpro_traces/resolve/main/codex_swebenchpro.json
curl -L -o /tmp/traces/qwen_coder.jsonl https://media.githubusercontent.com/media/alibaba-edu/qwen-bailian-usagetraces-anon/main/qwen_coder_blksz_16.jsonl

cd kvcm/sim
PYTHONPATH=. python3 -m kvcm_sim.run --trace cc256k --jobs 2      # 校准 + MRC + 消融，结果追加到 results/cc256k.json
PYTHONPATH=. python3 -m kvcm_sim.run --trace codex --jobs 2
PYTHONPATH=. python3 -m kvcm_sim.run --trace qwen_coder --jobs 2
PYTHONPATH=. python3 -m kvcm_sim.report                          # 生成 results/figures/*.png 与 results/summary.md
PYTHONPATH=. python3 -m pytest tests -q                           # 回放器语义小测试
```

单个 run 在 4 核 VM 上 30–170 s（1/32 切片）；三条 trace 全部跑完约 1 小时（4–5 进程并行）。

## 方法

### trace → 页级前缀键

| trace | 规模 | 时间线 | 哈希作用域 | 处理 |
|---|---|---|---|---|
| AgentX CC-256k（`semianalysisai/cc-traces-weka-062126-256k`） | 393 会话 / 68,266 请求 | trace 真实时间戳，服务时长用 `api_time` | 会话内 | main 链一条流、每个 subagent group 一条流、main 层一次性辅助请求各一条流 |
| Codex × SWE-bench Pro（`Inferact/codex_swebenchpro_traces`） | 610 trial / 20,230 调用 | **合成**：间隔 lognormal（P50 5.2 s / P90 23 s，按数据集 README） | 全局（内容） | tiktoken `o200k_base` 逐消息编码；第 k 次调用输入 = 第 k 条 gpt 消息前的全部消息 |
| Qwen-Bailian coder（`alibaba-edu/qwen-bailian-usagetraces-anon`） | 26,406 会话 / 43,011 请求 | trace 真实时间戳（2 h） | 全局 | `parent_chat_id` 链成会话 |

页 = 512 token；页键链式哈希 `key_i = hash(key_{i-1}, 页内 block ids)`，同一前缀在不同请求得到同一键。

### 负载与规模

- 会话按 Poisson 到达，每次到达从 trace 均匀抽一个会话、按原时间线整段回放；副本之间私有页加盐隔离，跨会话共享页（Codex 的公共 system prompt、Qwen 的公共前缀）保留，副本不会制造 trace 里不存在的复用。
- 到达率按“理想命中下 prefill 占集群 NPU 时间 50%”校准（`results/calibration.json`）。
- 仿真 1/k 集群：14 个实例不变，每实例 HBM、池容量与总负载同除以 k，NPU 数再乘回 k；主结果 k=32。对照（CC-256k，标称池，abs 阈值固定 11）：per-instance LRU 命中 k=32 为 95.81%、k=8 为 95.66%；shared-pool k=8 为 96.18%。路由失衡阈值不随 k 缩放（按 32/√k 缩放时 k=32 的 per-instance LRU 掉到 91.2%，是切片伪影）。
- 10 h 窗口，前 3 h 预热不计。

### 拓扑与策略

14 个实例（每实例 1 节点 16 NPU，PD 混部）。**HBM 归推理引擎**：每实例 450 GB 前缀缓存、纯 LRU，所有策略相同，不随 x 轴变化；**KVCM 只管 DRAM 池**（`../design/D2-kv-operations.md`「操作图解」：八项操作只作用于池内副本，HBM 不在 kvcm 操作面）。DRAM 两种组织：

| 策略 | HBM（L0） | DRAM（L1） | 路由 |
|---|---|---|---|
| per-instance LRU | 引擎 LRU | 实例私有，write-through，LRU（总量同池） | cache-aware（看各实例 HBM+私有 DRAM 命中） |
| shared-pool LRU | 引擎 LRU | 全集群一个共享池（去重、任意实例可取），LRU | cache-aware（看各实例 HBM 命中；池对所有实例等价） |
| KVCM（主线） | 引擎 LRU（不碰） | 共享池 + 停顿期池保活 + 零引用优先 | 同上 |

- 路由：SGLang cache-aware 规则——失衡（max−min > abs 且 max > 1.5×min）走最短队列，否则走最长前缀命中。负载 = 在途请求数。
- 每个请求的整条前缀都被加载进服务实例 HBM，并写入/刷新 DRAM 层（读即续租，同 Mooncake lease）；触达按“叶先根后”，祖先页不比子孙页旧，各层驻留集合沿前缀闭合，命中长度用二分查找。
- 驱逐三类：`pinned`（保活，不可驱逐）> `lru1`（有活会话引用）> `lru0`（零引用，先驱逐）。基线所有页都在 `lru0`，即纯 LRU。
- **引用归会话**：CC trace 里约 40% 的已结束 subagent 页会被同会话后续流（其他 subagent）复用，按流计引用会把它们错杀；在途请求同样持有引用（同 vLLM `ref_cnt`）。
- 池保活只钉 ETA ≤ 2 × 池 LRU 驻留时间（在线估计：容量 / 新页写入速率）的停顿，钉预算池 50%（realistic 10%，见下）。

### hook 三档

| 档 | ETA | 会话结束 | 说明 |
|---|---|---|---|
| none | 无 | 无 | 池保活 / 零引用都不生效，KVCM 退化为 shared-pool + 全局视图 |
| realistic | 该流最近 5 次间隔的几何均值（上界 exp(μ+σ)）；冷启动用全局历史中位数 | 活跃窗 max(4×ETA上界, 300 s) 内没回来视为结束 | 只用历史，不用未来信息 |
| oracle | 真实间隔 | 知道是否还有下一轮 | 上界 |

realistic 的钉预算（10%）在 CC-256k、容量 1/8 标称上调过（上一版模型，HBM 随 x 轴缩放）：预算越大越差（50% 时比 shared-pool 低 0.8 个点），因为按历史预测的停顿大多猜错，钉住的页挤掉了 LRU 本来留得住的内容。

### decode 写出的 KV

vLLM（生成 token 写满的块入 APC）与 SGLang（请求结束时整段插入 radix）都缓存 decode 写出的 KV。本仿真在请求结束时把“输入尾页 + 输出写满的页”写入服务实例 HBM 并写穿到 DRAM 层，h* 同样包含这些页。它们能否被下一轮命中，取决于下一轮输入是否原样携带本轮输出：

| trace | trace 暴露了什么 | 统计（同流相邻请求对） | 口径 |
|---|---|---|---|
| AgentX CC-256k | 输入 block hash + 输出 token 数 `out`（Anthropic 计数含 thinking），不分思维链/可见 | 80% 以本轮输入为前缀；其中 85% 输入增长 ≥ `out`；输出只占下一轮输入的 0.7% | 原样回传（影响很小） |
| Codex × SWE-bench Pro | 只有可见消息文本，reasoning 不在 trace 里；下一轮输入按构造原样包含上一条 gpt 消息 | 输出 = 上一条 gpt 消息的 token | 原样回传（可见部分精确） |
| Qwen-Bailian coder | 输入 block hash + `input_length` / `output_length`（`parent_chat_id` 链） | 99.6% 以本轮输入为前缀；99% 输入增长 ≥ `output_length`（中位增长/输出 = 1.12）；输出占下一轮输入的 13.4% | 原样回传（数据支持输出被完整带回） |

输出页的键取自同流下一请求输入里覆盖 `[本轮输入页数, (n_in + n_out) // 512)` 的页（要求下一请求以本轮全部输入页为前缀）。若下一轮实际剥掉了思维链，这些位置其实是可见回答 + 新内容，原样回传口径会高估——所以它是上界；“剥掉思维链”（输出全部不命中，即上一版口径）作为下界单独跑对照。

### 指标与换算

- 命中率 = 命中 token / 输入 token（token 加权，页粒度向下取整）；`h*` = 无限容量、全局共享时的命中率；lost reuse = 1 − h/h*。
- per-instance LRU 额外记录“集群任意层里有、但本实例看不见”的命中，用于把损失拆成“跨实例不可见”与“已被驱逐”。
- 按前一停顿长度（<10 s / 10–60 s / 1–10 min / 10–60 min / >1 h / 首轮）分组统计损失。
- TTFT（A3）= 2 × D8a §8.1 V4-Pro 拟合 TTFT（950，64 卡 · gbs=64；与 token 数相关的项按 V4.1 prefill 激活参数比 8/49 缩放）+ 未掩盖的 KV 加载时间（共享池按跨节点 78 GB/s，敏感性 ROCE 23.8 GB/s；私有 DRAM 按本机 86 GB/s）。
- 等效 NPU = Σ(基线 NPU·s − 策略 NPU·s) / 墙钟秒（D8a §8.2），单请求 NPU·s = 2 × 拟合 TTFT（64 卡同时跑 64 个请求，单请求平均占 1 张 950 卡）。

## 参考实现

- **SGLang** `python/sglang/srt/mem_cache/radix_cache.py::evict`（按优先级弹叶子）与 `hiradix_cache.py::_evict_write_through`（L1→L2 write-through、降层不丢）——见 [`docs/research/sglang/block-lifecycle.md`](../../docs/research/sglang/block-lifecycle.md)。借鉴：叶先驱逐 + 前缀闭合（本回放用“叶先根后触达”的 LRU 等价实现），DRAM 层 inclusive write-through。差异：SGLang 的 L2 是实例私有，本仿真的 shared-pool / KVCM 把 DRAM 做成全集群一个池（lake 原则 4）。
- **vLLM** `vllm/v1/core/block_pool.py` 的 `ref_cnt` / `free_block_queue`（归 0 只进可驱逐候选，按逆序入队）——见 [`docs/research/vllm/block-lifecycle.md`](../../docs/research/vllm/block-lifecycle.md)。借鉴：在途请求持有引用；逆序释放保证尾块先被驱逐。差异：vLLM 引用只在单实例内、请求级；KVCM 的引用是会话级、跨实例的，覆盖停顿期。
- **Mooncake** `master_service.cpp::BatchEvict`（lease 硬过滤 → 无 soft pin 先逐 → soft pin 后逐）——见 [`../design/lease-pin-eviction.md`](../design/lease-pin-eviction.md)。借鉴：三类驱逐顺序（pinned > 有引用 > 零引用）就是 hard/lease、soft pin、普通 key 的分轮驱逐；读即续租对应本仿真“每次使用刷新 DRAM 层 LRU”。
- **SGLang cache-aware 路由**（失衡走最短队列、平衡走最长前缀）——见 [`docs/research/sglang/model-runner.md`](../../docs/research/sglang/model-runner.md)。直接沿用为所有策略共同的路由，区别只在路由能看到的缓存。
- **Tair KVCM Optimizer**（tair-kvcache `kv_cache_manager/optimizer`）：radix 前缀匹配 + 可插拔驱逐策略的纯 CPU 回放、`HitRate = HitTokens / InputTokens` 口径，本仿真沿用其口径与“无限容量理论命中”上限。差异：Optimizer 是单池、无路由、无会话停顿语义；本仿真多实例 + 路由 + 停顿期池保活，是为了量化 KVCM 的会话感知机制。
- **Evolving Infra**（arXiv 2611.12304）：lost KV reuse = 1 − h/h* 的定义与“LRU 驱逐仍在等工具调用的 KV”的损失归因思路，本仿真按停顿长度分组复现这类归因。

## 局限（读结果前必看）

0. **上一版（commit 17c954d）模型有误**：HBM 与 DRAM 同比随 x 轴缩放，且 KVCM 的保活/预热会钉 HBM，越过了“HBM 归引擎”的职责边界，“其中 HBM”在策略间不同也来自这里。本版已改为 HBM 固定、引擎 LRU、各策略相同。更早两版还按 V4-Pro 布局假设 9,848 B/token，且下一轮从不命中上一轮输出；现改为官方 V4.1-Flash 890 B/token、decode KV 入缓存。
1. **A3 数值与算力换算是假设**：V4.1 结构与 890 B/token 取自官方（V4.1 只公开了 Flash）；A3 HBM/DRAM 可用量、实例切分、950→A3 与 V4-Pro→V4.1 算力换算均未核实（见 `params.md`）。A3 若不能存 FP4 KV，BF16 口径为 3,200 B/token。命中率曲线只依赖“容量 / 负载”比和 bytes/token；TTFT 和 NPU 数还依赖算力换算。
2. **无排队**：TTFT 是同等每卡并发下的单请求时延，不含排队；命中率下降带来的排队放大没有算进去（真实收益偏保守还是偏乐观取决于工作点）。要 goodput / P95 排队需要 DES（参考文章 1.5）。
3. **CC trace 的 block hash 只在会话内有效**，跨会话共享（同一 Claude Code system prompt、同一 subagent 模板）不可见，shared-pool 的去重收益被低估。
4. **Codex 时间线是合成的**（trace 无时间戳），停顿与 hook 结果只能看作结构性的。
5. **输出命中按“原样回传”计**：主结果假设下一轮输入原样携带上一轮全部输出（含思维链）；trace 不区分思维链与可见输出，这是上界口径，“剥掉思维链”下界见 summary 的对照表。
6. 页粒度 512 token、单次运行（seed=1）、无置信区间；切片 k=32（与 k=8 的对照见上）。
7. TTFT 拟合来自 950 单实例实测，不是 A3；跨节点传输按无竞争带宽计。
8. **池→HBM 预取不在范围内**：KVCM 向引擎发预取请求（`../design/kv-prefetch.md` 方案 A）未实现，未建模。
