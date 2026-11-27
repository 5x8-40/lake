# KVCM trace 回放仿真（纯 CPU）

用公开 agentic trace 回放 KV 缓存的访问序列，不跑模型，比较三类策略在 **DeepSeek-V4.1（即 V4.1-Flash，公开规格；KV 按 BF16 存，3,200 B/token）+ 昇腾 A3 224 NPU + HBM/DRAM 两层** 设定下的命中率（HBM 归推理引擎、纯 LRU、固定真实大小；KVCM 只管 DRAM 池；x 轴只扫池容量），再用 D8a §8.1/§8.2 的 TTFT 拟合换算成 TTFT 与等效 NPU 数。对应参考文章的数据类型 1.1（MRC）、1.2（lost KV reuse）、1.3（未命中 token）、1.4（TTFT）、1.6（等效卡数）、1.12（消融），见 Project 文档《参考文章的数据类型与获取方法》。

- 参数与每项来源/假设：[`params.md`](params.md)（V4.1 模型规格取自官方 config / 技术报告；**A3 容量与 prefill 算力换算仍是假设，等待校正**）。
- 结果与图：[`results/summary.md`](results/summary.md)（由脚本生成）。
- **6 节点保留收益主口径**（2026-11-27 起）：[`results/n6/summary.md`](results/n6/summary.md)，方法与结论见下文「6 节点保留收益主口径」；上面的 14 节点结果保留为基线。

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
- 仿真 1/k 集群：14 个实例不变，每实例 HBM、池容量与总负载同除以 k，NPU 数再乘回 k；主结果 k=32。对照（CC-256k，标称池，abs 阈值固定 11，在 FP4 890 B/token 口径下测）：per-instance LRU 命中 k=32 为 95.81%、k=8 为 95.66%；shared-pool k=8 为 96.18%。路由失衡阈值不随 k 缩放（按 32/√k 缩放时 k=32 的 per-instance LRU 掉到 91.2%，是切片伪影）。
- 10 h 窗口，前 3 h 预热不计。

### 拓扑与策略

14 个实例（每实例 1 节点 16 NPU，PD 混部）。**HBM 归推理引擎**：每实例 296 GB 前缀缓存（vLLM `gpu_memory_utilization=0.85`，激活从预算里扣；敏感性 360 GB）、纯 LRU，所有策略相同，不随 x 轴变化；**KVCM 只管 DRAM 池**（`../design/D2-kv-operations.md`「操作图解」：八项操作只作用于池内副本，HBM 不在 kvcm 操作面）。DRAM 两种组织：

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

realistic 的钉预算（10%）在 CC-256k、容量 1/8 标称上调过（更早一版模型，HBM 随 x 轴缩放）：预算越大越差（50% 时比 shared-pool 低 0.8 个点），因为按历史预测的停顿大多猜错，钉住的页挤掉了 LRU 本来留得住的内容。

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

0. **上一版（commit 17c954d）模型有误**：HBM 与 DRAM 同比随 x 轴缩放，且 KVCM 的保活/预热会钉 HBM，越过了“HBM 归引擎”的职责边界，“其中 HBM”在策略间不同也来自这里。本版已改为 HBM 固定、引擎 LRU、各策略相同。更早两版还按 V4-Pro 布局假设 9,848 B/token，且下一轮从不命中上一轮输出；现改为官方 V4.1-Flash 结构、decode KV 入缓存；commit 5d63330 按官方 FP4 890 B/token 计，本版按用户决定改为 BF16 3,200 B/token。
1. **A3 数值与算力换算是假设**：HBM 预算按 vLLM `gpu_memory_utilization=0.85` 计（296 GB/实例，激活不扣时 360 GB 为敏感性；commit 91a7710 及以前按 100% HBM 的 450 GB）；V4.1 结构取自官方（V4.1 只公开了 Flash），KV 按 BF16 存（3,200 B/token；官方 FP4 口径 890 B/token 为对照）；A3 HBM/DRAM 可用量、实例切分、950→A3 与 V4-Pro→V4.1 算力换算均未核实（见 `params.md`）。命中率曲线只依赖“容量 / 负载”比和 bytes/token；TTFT 和 NPU 数还依赖算力换算。
2. **无排队**：TTFT 是同等每卡并发下的单请求时延，不含排队；命中率下降带来的排队放大没有算进去（真实收益偏保守还是偏乐观取决于工作点）。要 goodput / P95 排队需要 DES（参考文章 1.5）。
3. **CC trace 的 block hash 只在会话内有效**，跨会话共享（同一 Claude Code system prompt、同一 subagent 模板）不可见，shared-pool 的去重收益被低估。
4. **Codex 时间线是合成的**（trace 无时间戳），停顿与 hook 结果只能看作结构性的。
5. **输出命中按“原样回传”计**：主结果假设下一轮输入原样携带上一轮全部输出（含思维链）；trace 不区分思维链与可见输出，这是上界口径，“剥掉思维链”下界见 summary 的对照表。
6. 页粒度 512 token、单次运行（seed=1）、无置信区间；切片 k=32（与 k=8 的对照见上）。
7. TTFT 拟合来自 950 单实例实测，不是 A3；跨节点传输按无竞争带宽计。
8. **池→HBM 预取不在范围内**：KVCM 向引擎发预取请求（`../design/kv-prefetch.md` 方案 A）未实现，未建模。

## 6 节点保留收益主口径

只看保留收益（池保活 + 零引用优先），不涉及冷热复制、局部性与 PD。14 节点结果（`results/summary.md`）保留为基线，不覆盖。参数见 [`params.md`](params.md) §9，逐点表格见 [`results/n6/summary.md`](results/n6/summary.md)，图在 [`results/figures/n6/`](results/figures/n6/)。

```bash
cd kvcm/sim
R="env KVCM_NODES=6 PYTHONPATH=. python3 -m kvcm_sim.run --util 0.7 --anchor lru --policies shared,kvcm-real,kvcm-oracle"
$R --trace cc256k --k 32 --out results/n6 --pool-tb 0.18,0.44,0.89,1.78,3.55,6 --seeds 1,2,3 --jobs 2   # 主口径（Codex / Qwen 同）
$R --trace cc256k --k 32 --out results/n6 --pool-tb 9,12 --seeds 1,2,3 --jobs 2                       # 大池敏感性（超标称）
$R --trace cc256k --k 16 --rate-k 32 --out results/n6 --pool-tb 0.44,1.78,6 --jobs 2                  # 切片对照
env KVCM_NODES=6 PYTHONPATH=. python3 -m kvcm_sim.run --trace cc256k --util 0.6 --anchor lru \
    --policies shared,kvcm-real,kvcm-oracle --k 32 --out results/n6/u60 --pool-tb 0.18,0.44,0.89,1.78,3.55,6  # 60% 敏感性
PYTHONPATH=. python3 -m kvcm_sim.report_n6                                                       # summary.md、closure.json、figures/n6/*.png
```

**设定**：DeepSeek-V4.1-Flash，BF16 KV 3,200 B/token；A3 6 节点 × 16 die = 96 NPU；每实例 HBM 296 GB（共 1.78 TB），引擎纯 LRU；KVCM 只管 DRAM 池。三种策略：共享池 LRU、KVCM 现实 hook、KVCM 理想 hook。池容量直接用 TB：0.18、0.44、0.89、1.78、3.55、6 TB（6 TB = 6 节点 × 1 TB 标称）。

**负载锚点**：每条 trace 分别标定到达率，使 6 TB 共享池 LRU 的实际 prefill NPU 占用为 70%（seed 1；seed 1/2/3 均值 67–70%）。结果是 Claude Code 5,123 会话/h、Codex 35,336、Qwen coder 928,967，分别为 14 节点主线的 0.471、0.596、0.571 倍。最初试过的“理想命中下占 70%”锚点会让 Claude Code 在 1.78 TB 需要 204% NPU，已弃用，粗扫留在 `results/n6/ideal_anchor_coarse/` 作记录。

### 结论

**三条 trace 都呈现“小池三者相同、中池理想 hook 拉开、大池收敛”。理想 hook 的收益集中在池 ≈ 0.5–2 倍集群 HBM（0.89–3.55 TB）。现实 hook 基本拿不到这部分收益：在 3.55 TB 及以上反而比共享池 LRU 低 0.1–1.4 pt。**

1. **Claude Code**：池 ≤ 0.44 TB 时三者都是 90.2%。理想 hook 在 1.78 TB 拉开最多：命中 92.71% 对 90.86%（+1.86 pt），prefill NPU 98.0 对 121.5（−19.3%），TTFT P95 1.71 s 对 2.84 s（−40%）；3.55 TB 时 +0.78 pt、NPU −13.6%；6 TB 时 +0.55 pt、NPU −11.1%。6 TB 仍有差距，所以补了大池敏感性（**超过 6 节点 × 1 TB 标称，只用于验证收敛，没有降低到达率**）：9 TB 时 +0.36 pt，12 TB 时 +0.24 pt，三者逐步收敛。现实 hook 在 6 TB 比 LRU 低 0.35 pt（NPU +6.9%）。
2. **Codex**：池 ≤ 0.89 TB 时三者差 < 0.1 pt。理想 hook 只在 1.78 TB 拉开：96.51% 对 96.02%（+0.49 pt，NPU −9.9%，P95 −1.5%）。3.55 TB 起三者完全相同（96.56%，已到理想上限）。
3. **Qwen coder**：池 ≤ 0.44 TB 时三者都是 60.0%。理想 hook 在 0.89–1.78 TB 拉开最多：+5.0–5.1 pt，NPU −9.3%，P95 −6.3%；3.55 TB 时 +2.09 pt；6 TB 时 +0.85 pt（NPU −1.8%）。大池敏感性：9 TB 时 +0.27 pt，12 TB 时 +0.10 pt，收敛。现实 hook 在 6 TB 比 LRU 低 1.35 pt。
4. **过载（如实标记）**：Claude Code 在池 ≤ 1.78 TB 时共享池 LRU 需要 127–136% NPU（每次 miss 的长上下文 prefill 很贵，实际命中 90.1–90.9% 远低于理想 96.3%）；理想 hook 在 1.78 TB 把它降到 102%。Codex 在 0.18 TB 为 102%。Qwen coder 全程 ≤ 83%。TTFT 为分析式、不含排队，过载格的 P95 偏乐观；命中率与 NPU 差不受影响。
5. **60% 锚点敏感性**（seed 1）：形态不变。Claude Code 1.78 TB 理想 hook +1.36 pt、NPU −17.1%，过载只剩池 ≤ 1.78 TB 的 102–111%；Qwen coder 0.89–1.78 TB +4.3–4.8 pt；Codex 负载更轻后只剩 1.78 TB +0.13 pt（NPU −2.9%），全程不过载。
6. **切片对照**（k=16 对 k=32，seed 1，池 0.44 / 1.78 / 6 TB）：Claude Code 绝对命中率差 ≤ 0.2 pt。Qwen coder 的绝对命中率在 k=16 下高 1.1–2.7 pt（HBM 层切片偏差），但策略之间的差距基本不变（1.78 TB 理想 hook − LRU：k=32 +5.02 pt，k=16 +4.93 pt）。所以 Qwen coder 的绝对命中率要带约 2–3 pt 的切片不确定度，增益结论不受影响。Codex 类似：0.44 TB 绝对命中率在 k=16 下高 1.1 pt，1.78 TB 理想 hook − LRU 为 k=32 +0.54 pt、k=16 +0.45 pt，6 TB 两者相同。
7. **seed 波动**：命中率 seed 极差一半 ≤ 0.34 pt（Claude Code），≤ 0.18 pt（Codex），≤ 0.09 pt（Qwen coder），小于中池的理想 hook 增益。
8. **距理论上限的缺口与闭合率**（图 [`results/figures/n6/n6_gap_to_ceiling.png`](results/figures/n6/n6_gap_to_ceiling.png)，三张主图的命中率面板也画了上限线）：理论上限 = 无限容量、全局共享下只剩首轮必然 miss，从回放记录计算，Claude Code 96.10%、Codex 96.56%、Qwen coder 70.98%（seed 均值）。闭合率 =（hook − LRU）/（上限 − LRU），按 seed 配对。理想 hook 闭合率：Claude Code 1.78 / 3.55 / 6 TB 为 35% / 52% / 68%；Codex 1.78 TB 为 91%；Qwen coder 0.89–1.78 TB 为 46–48%，6 TB 为 40%。现实 hook 中小池 0–5%，大池为负（Claude Code 6 TB −43%，Qwen coder 6 TB −63%）。LRU 缺口 < 0.2 pt 视为触顶、不给比值（Codex ≥ 3.55 TB 缺口恰为 0）；0.2–0.5 pt 只作参考（Claude Code 9 / 12 TB、Qwen coder 12 TB），因为分母趋零时比值被 seed 噪声放大（Claude Code 12 TB 逐 seed 71–85%）。闭合率要和绝对缺口一起读：Qwen coder 12 TB 的 35% 只对应 0.10 pt。逐点数据在 `results/n6/closure.json`。

## PD 转换（P/D/混部角色切换）：E1 流体模型 + E4 切换代价

回答“PD 转换值不值得做、KVCM 池在切换里值多少”。代码在 [`pd/`](pd/)，结果在 [`results/pd/summary.md`](results/pd/summary.md)，参数见 [`params.md`](params.md)「PD 转换」节。对应可行性评估：PR #89（vLLM-Ascend sleep mode 角色切换）+ `../design/D8-joint-scheduling.md` §5.3。

```bash
cd kvcm/sim
PYTHONPATH=. python3 -m pd.run            # 约 10 分钟；读 /tmp/traces 下的 cc256k / qwen_coder 原始 trace
PYTHONPATH=. python3 -m pytest tests -q   # 含 tests/test_pd.py
```

### 方法

- **需求序列（E1）**：会话 Poisson 到达，会话内按 trace 原时间回放（不改数据集时间）；每个 5 分钟窗口算 prefill 需求（`Params.npu_seconds`，命中取 h*）与 decode KV 占用（BF16 3,200 B/token × (n_in + n_out/2)，时长 = n_out × 每步 25 ms）。
- **节点模型**：14 个整数节点，P 节点 prefill 利用率 ≤ 80%；D 节点 KV ≤ 296 GB × 90%（decode 在这组参数下受 HBM 容量约束）；混部节点 prefill 占比 ≤ min(80%, 1 − t_step/TPOT_SLO) = 50%，decode 每步拉长 1/(1−f)。
- **策略**：静态全混部、静态最优 P:D（事后最优）、静态最优 P/D/混部组合、动态 P:D（oracle 前瞻 / 滞后一窗控制器 / 统计判据控制器）、动态三态（oracle）。统计判据按 Evolving Infra（arXiv 2611.12304）§5.2 / 算法 E.3：每个规划窗口（5 或 10 分钟）切成 60 s 切片，候选配比的逐片吞吐收益均值超过 2 倍标准误才切，切换未完成或开始不足 120 s 时跳过。goodput = 过载窗口 ≤ 1% 的最大负载倍数 α。
- **场景**：S0 平稳（未注入）；S1 只变总量；S2 / S2m / S4 只变配比（CC ↔ Qwen coder 组合按正弦变化，强 / 弱 / 2 小时快变）；S3 总量 + 配比；S5 配比阶跃（CC 到达率在 5 分钟内阶跃，P 占比目标 0.5↔0.8，24h 内 3 次、每次持续 4h；Qwen 到达率按 CC 实际需求补齐使总需求≈恒定；因 CC 会话长，P 占比实际约 45–60 分钟完成大部分变化），另报每次阶跃后的过载窗口数与连续过载分钟。**S1–S5 的到达率包络或 trace 组合为人为注入，表和图里都标【注入】**；CC 会话很长，注入的“总量恒定”在实现上会被拖尾抹平，实际总量 CV 见结果表。
- **切换代价分档**：同布局逻辑切换（权重不动，代价≈0）与 PR #89 sleep/wake + host H2D（+30 s，host 510 GB/节点副本从池扣，命中下降按 PR #90 MRC 计）各自 × {KVCM 池丢弃后重读, 无池点对点迁移, 无池排空}；另加“逻辑切换 + 无池快切·丢 KV 重算”（2 s 切换，被切节点 KV 全丢，E4 杀掉重算的 NPU·s 按切换时刻 KV 占用比例摊进之后 5 分钟的 prefill，并统计丢失复用 = 重算 / 命中省下的 NPU·s，对照论文图 3a 的 Arrow 55.5% 与 Evolving Infra 0.3%）。
- **E4（轻量版，非 DES）**：对满载 D 节点的在途 decode 做快照抽样（按 decode 时长加权），算一次 D→P 切换的排空时间、点对点迁移时间、杀掉重算的 NPU·s、池刷尾页与接收方重读量；结果作为 E1 分档的切换耗时。

### 参考实现

- **Dynamo Planner** `components/src/dynamo/planner/core/load_scaling.py::LoadScalingMixin` / `throughput_scaling.py::ThroughputScalingMixin`（见 [`docs/research/dynamo/overview.md`](../../docs/research/dynamo/overview.md)「Planner」）。借鉴：`min_gpu_budget == max_gpu_budget` 时总量固定、只在 P/D 间重新分配，就是这里的“动态 P:D”；滞后控制器对应它的快环（按实时指标调整）。差异：Planner 扩缩以 pod 为单位、KV 随实例走；这里 KV 权威在池，D→P 切换可以丢弃 HBM KV 由他处重读（E4）。
- **Splitwise** mixed pool（机器在 prompt/token 角色间移动）与 **DistServe** goodput 定义：本实验的“动态三态”与 goodput 指标照搬其口径。差异：它们是 GPU 真机 + 自建 DES，这里只有窗口级流体模型。
- **Evolving Infra**（arXiv 2611.12304，无公开代码）§3.1 图 3a 与 §5.2 / 附录 E.3 算法 E.3：统计判据控制器与丢失复用口径照此实现。差异：论文用在线拟合的迭代时间模型算 Θ，这里用流体容量；论文节点切换时在途请求原地跑完，这里各档按 E4 分别计。
- **vLLM-Ascend** `vllm_ascend/worker/worker.py::Worker.sleep` / `wake_up`（PR #89 核实）：sleep/wake 档的切换流程与 host 权重副本来自这里。差异：PR #89 的 8 卡示例形态（D DP8 TP1）放不下 V4.1-Flash，这里按单节点形态计。

### 局限

见 [`results/pd/summary.md`](results/pd/summary.md)「简化与局限」。最关键的三条：不排队、不出 TTFT/TPOT 分位（混部的 TPOT 尾部干扰只按均值 1/(1−f) 计，会高估混部）；decode 代价与切换耗时在 A3 上无实测；配比漂移场景是注入的。

## 池局部性扩展：近远端、冷热与网卡争用

回答“共享池里块放在哪个节点、要不要做热点复制，KVCM 能不能因此比扁平共享池更快”。对应 Project 文档《KVCM 池化的近远端、冷热与争用》§4 的三项最小扩展与五组对照。代码 [`kvcm_sim/locality.py`](kvcm_sim/locality.py)（`LocSim`，继承主线 `Sim`，命中口径不变）、[`kvcm_sim/locality_run.py`](kvcm_sim/locality_run.py)；全部表格见 [`results/locality.md`](results/locality.md)，图在 [`results/figures/locality/`](results/figures/locality/)，参数见 [`params.md`](params.md) §6。基线全部是 BF16 3,200 B/token、HBM 296 GB/实例。

```bash
cd kvcm/sim
PYTHONPATH=. python3 -m kvcm_sim.locality_run --trace cc256k --jobs 2   # codex / qwen_coder 同理
PYTHONPATH=. python3 -m kvcm_sim.locality_run --trace zipf --jobs 2     # 合成倾斜度扫描
PYTHONPATH=. python3 -m kvcm_sim.locality_report                         # 图 + results/locality.md
```

单 trace 在 4 核机器上约 1–2 小时；`--only E1,E2` 只跑部分实验，`--alphas` 只跑部分 α，已有结果自动跳过。

### 方法

- **三项扩展**：①池块有所在节点（`hash` 按页键条带到 14 节点 = 扁平池；`local` 写到写入请求所在节点）；②计入 TTFT 的池读时间是 `max(0, τ − W)`，W 为 prefill 排队（不重叠时读在拿到算力后才开始，W 不掩盖）；③每节点带宽按 `B/(1+在途流数)` 共享：跨机读占源节点出口与读者节点入口（一个请求从多个源并行读，入口只算一条流、总量受入口带宽约束），本机读占本机 H2D 拷贝引擎。副本作为独立条目占池容量、参与池 LRU。
- **排队**：在主线回放上加离散事件 prefill 队列，每实例 16 槽，槽占用 = A3 计算时长 + 未掩盖读时间。TTFT 分位来自这个队列，与主线 summary 的无排队 TTFT 不可直接比较。
- **两种 A3 传输**：FabricMem（本机 86、跨机 78 GB/s）与 RoCE（本机 47、跨机 23.8 GB/s），D8a §9.1。
- **五组对照**：E1 成本模型（扁平 / 拓扑 / 拓扑+争用 × 读是否与排队重叠）；E2 放置与副本（写本机、Mooncake 式被动副本、TokenLake 式热点复制、KVCM 热页定向副本 + 预测 TTFT 路由）；E3 合成 Zipf 共享长文档负载的倾斜度扫描；E4 路由（主线规则 / 最短队列 / 预测 TTFT）；E5 跨机代价对 bytes/token 的敏感度（BF16 3,200 vs FP4 890）。

### 结论

**一句话：池内的近远端放置和热点复制，在三条真实 trace 和合成倾斜负载上都没有可测的 TTFT 收益；KVCM 相对扁平共享池的收益来自命中率（主线 MRC 的工作区间）和排队感知路由，不来自局部性。**

1. **池读本身太小，近远差异被淹没（E1、E5）**。标称 14 TB 池下，读池的请求占 1%（Codex）、4%（Claude Code）、10%（Qwen coder），大部分前缀在 HBM 命中，池只补最后一段。未掩盖的池读时间均值 ≤ 0.6 ms（Claude Code + RoCE，最差），占 TTFT ≤ 0.2%；扁平 / 拓扑 / 拓扑 + 争用三种成本模型下，读池请求 TTFT P95 都是 2.17 s（Claude Code）、0.60 s（Codex）、0.39 s（Qwen coder），差异 < 1%。最忙节点出口利用率 ≤ 1.5%。按输入长度分桶，跨机比本机多出的时间在 RoCE + BF16、128K–256K 最长的桶里也只有均值 0.6 ms、P95 7.5 ms；换成 FP4 再小一个量级。
2. **写本机放置能提高本机字节占比，但不改变 TTFT（E2）**。Claude Code 本机字节 7% → 12%，Qwen coder 7% → 17%，TTFT 均值与 P95 变化 0.0%。cache-aware 路由下续轮请求多数回到 HBM 命中的实例，真正需要池补的那段并不集中在写入节点。
3. **被动副本（Mooncake 式“拉取留存”）有害（E2）**。副本挤占池容量：Claude Code 14 TB 池里副本占 28%，命中率 95.52% → 95.18%，读池请求 TTFT P95 +18%，均值 +27%，prefill 占用 137.9 → 147.6 NPU；Qwen coder 副本占 11%，命中率 −0.24 pt；Codex 副本占 18%，无收益。
4. **热点复制（TokenLake 式 + D8a 目标副本数）从未触发（E2、E3）**。按 `r = ceil(λ_p · τ_req / ρ)` 算，池读时长是毫秒级，即便合成 Zipf α = 0.6–1.4 的共享长文档负载，`λ·τ` 也远小于 ρ = 0.5，所以副本数始终为 1。E3 里扁平池与 KVCM + 局部性的读池请求 TTFT P95：FabricMem 下差 ≤ 0.5%，RoCE 下 0.131 → 0.125 s（−4.5%，约 6 ms），最忙出口利用率 ≤ 3.8%。
5. **“KVCM + 局部性”组合的收益来自预测 TTFT 路由（E2、E4）**。Claude Code 14 TB 池下，这个组合把 TTFT 均值从 0.510 s 降到 0.378 s（−26%）；但 E4 里扁平共享池只换成预测 TTFT 路由就得到全部请求 P95 2.005 → 1.096 s、均值 0.510 → 0.360 s，最短队列路由也有 2.005 → 1.558 s。也就是说，起作用的是“路由看排队”，与池块放在哪个节点无关。Qwen coder 与 Codex 本来不排队（排队 P95 = 0），路由换了也没变化；Codex 的“读池请求 P95”在预测 TTFT 路由下变差 74%，是选择效应（路由不再偏向 HBM 命中实例，读池的人群换成了更长的请求），全部请求 P95 不变（0.675 s）。
6. **Claude Code 3.5 TB 池（过载）下 KVCM + 预测 TTFT 路由不稳定**：现实 hook 版本命中率 91.56% → 89.79%，prefill 占用 251.9 → 303.2 NPU，TTFT 均值翻倍；理想 hook 版本命中率 91.97%、占用 241.3 NPU，仍比扁平池好。过载区路由不能只看预测 TTFT，还要保住前缀亲和。

**对 KVCM 设计的含义**：在这组参数（BF16、HBM 296 GB/实例、A3 两种传输）下，池可以按哈希条带化、单副本、不做局部性放置；把复杂度放在会话感知的保留 / 驱逐（命中率）和路由侧的排队感知上。只有当池读量大幅上升（HBM 远小于工作集、或长文档被大量并发首读）时才需要重新评估，届时出口利用率是先行指标。

### 参考实现

- **Evolving Infra**（arXiv 2611.12304）附录 E.3：远端读的 TTFT 代价是 `max(0, τ(x) − W)`，排队时间能掩盖传输。直接用作计入 TTFT 的读时间；差异：它只给公式，这里用 DES 排队真实算出 W，并对比“读不与排队重叠”（SGLang HiCache / vLLM connector 的默认加载时机）。
- **NetKV**（arXiv 2606.03910）：`B_eff = B·(1−c)/(1+n_inflight)`。借鉴为每节点出口 / 入口 / H2D 的争用模型（c 取 0）；差异：NetKV 是 GPU 集群，这里套 A3 FabricMem / RoCE 带宽。
- **Mooncake Store** `mooncake-store/include/replica.h::ReplicateConfig`（`prefer_alloc_in_same_node`、`replica_num`），见 [`docs/research/mooncake/kv-store.md`](../../docs/research/mooncake/kv-store.md)。`local` 放置对标 `prefer_alloc_in_same_node`，“被动副本”对标论文里持有者过载时拉取留存；预测 TTFT 路由对标 Mooncake Conductor（见同文「Conductor」节）。差异：Mooncake Store 是对象级、无前缀语义，副本数是写入时静态配置；KVCM 按会话与前缀热度动态决定。
- **TokenLake**：热段复制 + power-of-two 选副本。`hash+hot` 变体与读副本两选一直接照搬；差异：TokenLake 面向高并发共享前缀，副本主动推送，这里只在热度满足 D8a 目标副本数时才触发。
- **D8a §6.5** `r_target = ceil(λ_p / (μ_p ρ))`（`../design/D8a-scheduling-algorithms.md`）：热点复制的副本数按 Little 定律算。本仿真按这个公式触发复制，结果是真实 trace 上几乎从不触发（读池时长是毫秒级，`λ·τ` 远小于 ρ）。

### 局限

1. **时间膨胀切片是近似**：1/32 切片下计算时长 ×32、每节点带宽 ÷32，让在途流数与槽占用按 Little 定律与全规模一致；但会话内请求间隔没有放大，突发更密，排队偏保守。
2. **Claude Code 3.5 TB 池过载**：prefill 需求 > 224 NPU，DES 队列不收敛（P95 数百秒），该行只看命中率与相对排序，不看 TTFT 绝对值；KVCM + 预测 TTFT 路由在这一档不稳定（命中率反而下降）。
3. **带宽按每节点一条路径**：没有把 16 NPU 的多链路聚合进来（会进一步降低争用）；FabricMem 取跨机下限 78 GB/s。
4. **池→HBM 预取、PD 间 KV 传输不在模型内**：只建模“请求到达时从池读前缀”。
5. **合成 Zipf 负载是假设**：文档总量 ≈ 2× 集群 HBM、0.6× 池，用来造出“热文档落池被并发读”的最坏情形；真实 trace 没有这种共享长文档。

## 压力敏感性与 KVCM 增益区间【注入】

回答两件事：①局部性、热点复制、被动副本从哪一点开始有收益；②KVCM 相对共享池 LRU 在哪个区间有增益、增益来自保留还是局部性 / 路由。全部场景为人为注入，trace 数据集不改；参数见 [`params.md`](params.md) §7，表格见 [`results/locality.md`](results/locality.md)「KVCM 增益区间」「压力敏感性」两节，图在 [`results/figures/locality/stress/`](results/figures/locality/stress/)。

```bash
cd kvcm/sim
LOC_H=21600,7200 PYTHONPATH=. python3 -m kvcm_sim.locality_run --grid --trace cc256k --jobs 2     # 增益网格
LOC_H=21600,7200 PYTHONPATH=. python3 -m kvcm_sim.locality_run --stress --trace cc256k --jobs 2   # 压力点
PYTHONPATH=. python3 -m kvcm_sim.locality_report
```

**口径与裁剪（截止时间约束）**：回放窗口 6 h（前 2 h 预热，主线 10 h / 3 h），切片 1/32；只跑 RoCE；链路改用守容量的 FCFS 排队模型（§6 的 `contend` 在饱和时会超容量）；负载放大 = 提高会话到达率（等价于复制会话，会话内复用不变，没有压缩 trace 时间）；Zipf 冷热重扫只保留 α = 0.6 的 2 个压力点；最坏组合只做 1 个；单因素去掉背景 30% 与 KV ×4。

### 增益区间结论

**增益区间存在，但几乎全部来自“保留”且只有理想 hook 拿得到；局部性本身不贡献增益。**

1. **理想 hook（保留 / 驱逐按真实未来复用）**：Claude Code 在所有格子 prefill NPU 都省 ≥ 5%：标称池 1× 负载省 8.1%（命中 +0.37 pt），负载 1.5–2× 省 13–14%，池 7 TB 省 14–20%（7 TB × 2× 最大，−19.9%），池 3.5 TB 省 10–16%。Qwen coder 只在池偏小时达标：池 3.5 TB 省 7.4–13.7%（命中 +3.7 ~ +7.4 pt），池 7 TB 且负载 ≥ 1.5× 省 5.9–7.6%；标称 14 TB 池省 < 4%。**增益随“池相对工作集变小”与负载上升而变大**，与 HBM 大小无关：HBM 降到 1/2、1/4 时命中率与 NPU 完全不变（池把 HBM 漏掉的全部接住），变的只是读池时间。
2. **现实 hook（只靠会话停顿期保活 + 零引用优先）拿不到这部分增益**：所有格子命中率比共享池 LRU 低 0.0–0.9 pt（Qwen 3.5 TB 池例外，+0.3 ~ +0.45 pt、NPU 省 < 1%），NPU 多用 0–8.5%。现实 hook 与理想 hook 的差距就是“预测未来复用”的价值。
3. **TTFT P95 的大幅下降来自路由，不来自 KVCM 或局部性**：负载 ≥ 1.5× 时系统排队（共享池 P95 50–560 s），预测 TTFT 路由把 P95 降 60–99%；但“共享池 + 同样的路由”对照拿到几乎相同的收益（例如 CC 标称池 1.5×：KVCM + 局部性 −87%，共享池 + 路由 −97%）。负载 1× 时，局部性 / 路由增量 P95 在 Qwen 为 0–1%，CC 为 +10% ~ +35%，同样被“共享池 + 路由”覆盖（标称池 +18% vs +21%）。
4. **过载区（共享池 prefill 需求 > 224 NPU：CC 池 3.5 TB ≥ 1.5×、7 TB ≥ 1.5×、14 TB 2×；Qwen 2×）**：TTFT 不收敛，只看 NPU；理想 hook 在这里省 10–20%，足以把部分格子拉回容量以内（CC 7 TB 1.5×：244 → 208 NPU）。现实 hook + 预测 TTFT 路由在小池下会破坏前缀亲和、命中率反降 1.4–2.0 pt（CC 3.5 TB），路由在过载区要保住亲和。

### 压力敏感性结论

**任何单因素压力下，局部性、热点复制和被动副本都没有收益。只有当跨机读池需求超过网卡剩余容量时，局部性才开始有用；本次扫描里只有最坏组合达到这个条件。即便如此，起作用的也是“路由知道数据在哪”，不是放置或副本本身。**

1. **单因素（跨机带宽 1/4、1/10，背景流量 50%、70%，KV 传输 ×8）**：在前缀亲和路由下，读池请求只占 3.3%（Claude Code）和 10.5%（Qwen coder）。未掩盖读时间分别 ≤ 10 ms 和 ≤ 0.6 ms，池读额外占用的出口 ≤ 11%。所有变体的 TTFT P95 变化都 < 2%。Claude Code 上 KVCM + 局部性的 P95 是 1.15 s，扁平池是 1.41 s，但这个差距在基线就已存在，来自路由的负载均衡，与压力无关。
2. **起效点（最坏组合 = HBM 1/4 + KV ×8 + 跨机带宽 1/10 + 背景 70%）**：读池请求升到 28–32%，最忙出口负载（需求/容量）达到 158–758%。扁平池、写本机、被动副本、热点复制和 KVCM（哈希放置）全部失稳：Claude Code 的 P95 在 4.2×10⁴ 到 1.4×10⁵ s，Qwen coder 在 5.2×10³ 到 2.0×10⁴ s。KVCM + 局部性仍稳定（P95 分别 1.56 s 和 0.57 s，未掩盖读 37 ms 和 0.7 ms），因为路由把请求送到池块所在节点，读走本机 H2D，不占网卡。单因素点与最坏组合之间没有二分出精确拐点（截止时间裁剪）。
3. **消融：TTFT 路由不计数据位置**：放置、副本和命中率都与 KVCM + 局部性相同，但请求不跟着数据走，读池请求升到 81%（Claude Code）和 53%（Qwen coder）。Claude Code 在跨机带宽 1/4 时 P95 就升到 15.7 s；在带宽 1/10、背景 70%、KV ×8 下分别是 508 s、322 s、319 s。Qwen coder 只在最坏组合失稳（2.31×10³ s）。基线下两者 P95 相同（1.16 s 对 1.15 s），所以无压力时路由收益来自负载均衡加命中，有压力时来自局部性。“写本机”放置如果不配位置感知路由，反而制造跨机读。
4. **热点复制（TokenLake 式）**：单因素下从不触发（0 GB/s）。只在最坏组合触发（Claude Code 190 GB/s，Qwen coder 20 GB/s，Zipf 500 GB/s），而且让结果更差：P95 是扁平池的 2–3 倍，因为复制流量挤在同一条已饱和的网卡上。副本占池 28–65%，命中率降 0.8–1.4 pt。
5. **被动副本（Mooncake 式）**：单因素下始终是净成本。Claude Code 上副本占池 24.9%，命中率 −0.27 pt，NPU 从 116.8 升到 123.6，P95 +16%。最坏组合下 P95 比扁平池低约 40%，但仍然失稳。
6. **合成 Zipf α = 0.6 在压力下**：跨机带宽 1/10 时，扁平池 P95 0.222 s，KVCM + 局部性 0.125 s（−44%），热点复制仍不触发。最坏组合下扁平池 P95 1.9×10⁵ s，KVCM + 局部性 0.247 s。
7. **口径提醒**：本机读走 H2D、不占网卡，这是模型假设。Claude Code 最坏组合的消融格没跑完（截止时间），该格在表中留空。

## 热点复制增益场景【注入】

目的：构造一个热点复制应当有收益的场景。少数节点很热，其他节点网卡有余量，同一份长文档被很多请求反复读。然后找复制从“净成本”转为“净收益”的拐点。合成负载，不改原有 trace；参数见 [`params.md`](params.md) §8，表格见 [`results/locality.md`](results/locality.md)「热点复制增益场景」，图在 [`results/figures/locality/hotspot/`](results/figures/locality/hotspot/)。

```bash
cd kvcm/sim
LOC_H=10800,3600 PYTHONPATH=. python3 -m kvcm_sim.hotspot --jobs 4 --sets 2          # 主扫描：持有热文档节点数 1–14
LOC_H=10800,3600 PYTHONPATH=. python3 -m kvcm_sim.hotspot --jobs 4 --sets 0.5 --hot 1  # 对照：热集合 HBM 放得下
PYTHONPATH=. python3 -m kvcm_sim.locality_report
```

**设计**：
- 共享 128K token 长文档，热集合为集群 HBM 的 2×，热文档在 HBM 里互相挤出，约一半请求读池。
- 写本机放置下，热文档主副本只落在 n_hot 个节点。
- 到达率使全网池读需求约为全网出口容量的 20%。n_hot = 1、2、3 时热节点出口需求分别约为容量的 271%、136%、92%，其余节点有余量。
- 裁剪：回放窗口 3 h（前 1 h 预热）；热集合只扫 2× 一档主扫描，0.5× 只做 1 个热节点对照。

### 结论

**只有当数据被强制集中在少数节点、热节点出口需求接近或超过容量（本场景约 ≥ 90%）时，热点复制才是净收益。但扁平哈希池把热文档条带化到所有节点，热点从一开始就不存在，P95 始终 0.16 s；没有任何复制变体比它好 10% 以上。KVCM 热页定向副本只在“单节点持有全部热文档”这一档比“只路由”有收益，2 个及以上热节点时路由已经足够。**

1. **TokenLake 式热点复制对写本机**：热节点 1、2、3 个时，P95 分别从 559 s、135 s、12.5 s 降到 184 s、18.9 s、0.26 s（−67%、−86%、−98%），是净收益；4 个及以上时（热节点出口 ≤ 66%）变化 ≤ 3%，基本中性。代价集中在最热的一档：热节点 1 个时复制流量 19.4 GB/s，副本占池 37.5%，命中率 −1.1 pt，prefill NPU 从 68.5 升到 124；而且仍没有收敛（P95 184 s），因为复制流量从已经饱和的热节点出口出发。
2. **KVCM（热页定向副本 + TTFT 路由）对只路由**：热节点 1 个时，只路由把请求挤到唯一的热节点或排它的出口，P95 57 s；KVCM 在读者处留副本（读者刚拉到的字节就地留存，无额外复制流量），副本占池 22.6%，P95 0.174 s，命中率只降 0.03 pt。热节点 ≥ 2 个时，只路由已经是 0.15–0.16 s，KVCM 副本占池 ≤ 7%，不再有增益。
3. **被动副本（Mooncake 式）是净成本**：与集中度无关，副本占池约 48%，挤掉热文档，命中率 −5.3 pt，prefill NPU 从 68.5 升到约 333，P95 约 366 s。只有单热节点那一档它比写本机的 P95 低 34%，但两者都已失稳（367 s 对 559 s）。
4. **保留策略在这里不起作用**：只路由的两种（共享池 LRU 与 KVCM 保留）结果完全相同。
5. **对照：热集合 0.5× 集群 HBM（只做 1 个热节点）**：HBM 吸收了热文档，读池请求只占 7%（TTFT 路由下 0%），即使只有 1 个热节点，它的出口负载也只有 22%。所有变体的 P95 都是 0.134 s，复制没有用武之地，与之前的 Zipf 实验一致。所以热集合必须大到 HBM 放不下，才会出现上述拐点。
6. **拐点**：以热节点出口负载（需求/容量）为横轴，复制从中性转为净收益的位置在约 90%（3 个热节点）与 66%（4 个热节点）之间；单热节点（约 270%）时只有“读者就地留副本 + 位置感知路由”能收敛。
