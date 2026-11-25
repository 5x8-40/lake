# KVCM trace 回放仿真（纯 CPU）

用公开 agentic trace 回放 KV 缓存的访问序列，不跑模型，比较三类策略在 **DeepSeek-V4.1（即 V4.1-Flash，公开规格；KV 按 BF16 存，3,200 B/token）+ 昇腾 A3 224 NPU + HBM/DRAM 两层** 设定下的命中率（HBM 归推理引擎、纯 LRU、固定真实大小；KVCM 只管 DRAM 池；x 轴只扫池容量），再用 D8a §8.1/§8.2 的 TTFT 拟合换算成 TTFT 与等效 NPU 数。对应参考文章的数据类型 1.1（MRC）、1.2（lost KV reuse）、1.3（未命中 token）、1.4（TTFT）、1.6（等效卡数）、1.12（消融），见 Project 文档《参考文章的数据类型与获取方法》。

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
