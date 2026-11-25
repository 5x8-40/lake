# KVCM trace 回放仿真：参数表

每个参数标注来源；没有可核实来源的一律标 **假设**，等待用户校正。代码里的对应值在 [`kvcm_sim/params.py`](kvcm_sim/params.py)（硬件/模型/算力）与 [`kvcm_sim/replay.py`](kvcm_sim/replay.py) `Config`（策略/路由）。两处改动要同步本表。

## 1. 模型：DeepSeek-V4.1-Flash（公开规格）

DeepSeek V4.1 目前公开的只有 **DeepSeek-V4.1-Flash**（Hugging Face `deepseek-ai/DeepSeek-V4.1-Flash`，2026-09-10 发布，含 `config.json`、`inference/model.py` 与 `DeepSeek_V41_Tech_Report.pdf`）；没有 V4.1 Pro。下表按它取值。

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| 规模 | 552B backbone（另有 Engram 196B）；prefill 激活 8B、decode 激活 16B；40 层（20 层 causal encoder + 20 层 decoder，CED）+ 3 层 MTP | HF README「Introduction」+ 技术报告摘要 |
| 全局 KV 结构 | CSA2：只有 `kv_source_layer_ids = [2, 8, 14, 20]` 4 层存 main KV 与 indexer K，其余层 Reindex/Reuse 共享；这 4 层 `compress_ratios` = 2, 2, 2, 1 | `config.json` `text_config`；`inference/model.py` `Compressor` / `Indexer`（`owns_k = layer_id in kv_source_layers`） |
| 每 entry 字节（**主口径 BF16**） | main KV 512 维 × 2 B = 1,024 B；indexer K 128 维 × 2 B = 256 B；合计 1,280 B | 用户决定（2026-11-25）按 BF16 存 KV；维度取自 `config.json` `text_config`（`head_dim` 512、`index_head_dim` 128）；结构同下行 FP4，只换存储精度 |
| 每 entry 字节（官方 FP4，对照） | main KV 512 维 FP4（E2M1）+ 每 16 维一个 E4M3 scale = 288 B；indexer K 128 维 FP4 + 每 32 维一个 scale = 68 B；合计 356 B | `inference/model.py`（main latent `fp4_act_quant(latent, 16, …, float8_e4m3fn)`，indexer `fp4_act_quant(k, fp4_block_size=32)`）；README「FP4 main KV caching (E2M1, one E4M3 scale per 16 channels)」 |
| **全局 KV bytes/token（主口径）** | **3,200 B**（BF16：3 × 1,280 / 2 + 1,280） | 由上面几行推导；**主结果按此计** |
| 同上（官方 FP4，对照） | 890 B（= 3 × 356 / 2 + 356） | 与 README / 技术报告给出的 “890 bytes per token” 一致；commit 5d63330 的结果按此口径，未进本版主结果 |
| SWA KV | 每层 128 token 窗口，**不入持久 KV**，放在每机 10% DRAM 的短 TTL 池；缺失时 Encoder SWA Bounded Replay 只重算最后 `n_win` 个 token | 技术报告 §3.2.1–§3.2.2；**仿真忽略**（与前缀命中无关，重算 128 token 的开销在回放精度以内） |
| 权重 | 510 GB | HF safetensors 48 个分片合计（FP8 主体 + FP4 专家 + Engram 表；按全部放 HBM 计，偏保守） |

## 2. 硬件：华为昇腾 A3 集群

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| 规模 | 14 节点 × 16 NPU = 224 NPU | 用户给定（2026-11-24） |
| HBM / NPU | 64 GB | **假设**：910C 每芯片 128 GB、2 die，按 die 计 NPU（16 NPU/节点 = 8 芯片） |
| 实例切分 | 1 节点 / 实例（16 NPU），共 14 个实例；PD 混部 | **假设**：510 GB 权重单节点（1 TB HBM）放得下，按最小可放切分 |
| 激活 / workspace 预留 | 4 GB / NPU | **假设** |
| 引擎显存预算 | vLLM `gpu_memory_utilization = 0.85`：引擎只用每 die 64 × 0.85 = 54.4 GB，权重与激活/workspace 都从这 54.4 GB 里出 | 用户批准（2026-11-25）；vLLM 默认 0.9，取 0.85 留余量 |
| HBM KV / 实例（**主结果**） | **296 GB**：每 die 54.4 − 31.9 权重 − 4 激活 = 18.5 GB，× 16 = 296 GB；每实例 ≈ 9,250 万 token，全集群 4.1 TB ≈ 13 亿 token（BF16） | 由上面几项推导 |
| HBM KV / 实例（敏感性） | 360 GB：激活不从 0.85 预算里扣（每 die 54.4 − 31.9 = 22.5 GB） | 推导；summary「HBM 预算敏感性」表 |
| （上一版） | 450 GB = 16 × (64 − 31.9 − 4)，按 100% HBM 计 | commit 91a7710 及以前，已弃用 |
| DRAM KV / 节点 | **1 TB**；全集群池 14 TB ≈ 43.8 亿 token（BF16） | **假设**（Atlas 800T A3 主机内存配置与 KV 可用比例待确认） |
| 本机 DRAM→HBM 带宽 | 86 GB/s | `kvcm/design/D8a-scheduling-algorithms.md` §9.1 A3 910C FabricMem 机内 rH2D（AICPU 展开）下限 |
| 跨节点池→HBM 带宽 | 78 GB/s；敏感性 23.8 GB/s | 同上 §9.1：FabricMem 跨机 rH2D 下限；ROCE rH2D |
| 存储层 | 只有 HBM + DRAM，无 SSD | 用户给定 |

## 3. Prefill 算力与 TTFT

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| TTFT 拟合式 | `TTFT = c + (1−hit)(a·N + b·N²)`，N 为 K-token；V4-Pro：c=0.0259，a=0.1856，b=0.0003338 | `kvcm/design/D8a-scheduling-algorithms.md` §8.1（**设备 A = 950**，不是 A3） |
| 拟合测点拓扑 | 64 卡 · gbs=64 | `tools/cmx-sim/measured.js`（设备 A，`deepseek-v4-pro`） |
| 单请求算力占用 | TTFT × 64 卡 / 64 并发 = 1 张 950 卡 × TTFT | 由测点拓扑推导（`noCacheTPS = N/TTFT` 与表内一致，说明 TTFT 是单请求口径） |
| 950 → A3 换算 | 1 张 950 ≈ **2 个 A3 NPU**（die）的 prefill 吞吐 | **假设**（待 A3 实测或厂商数据） |
| V4-Pro → V4.1-Flash 换算 | 拟合式中与 token 数相关的 a、b 项 × 8/49（prefill 激活参数比），固定开销 c 不变 | **假设**（拟合只有 V4-Pro；激活参数取自 V4.1 README 表 “# Activated Params 8B / 16B” 与 V4-Pro 49B） |
| A3 TTFT | `2 × TTFT_fit(V4.1 缩放后)`（同等每卡并发）+ 未掩盖的 KV 加载时间 | D8a §3.2 “掩不住”式；换算系数为 **假设** |
| 等效 NPU 数 | `Σ_请求 (NPU·s_基线 − NPU·s_KVCM) / 墙钟秒` | D8a §8.2 |
| 排队 | 不建模（无 DES） | 局限，见 README |

## 4. 负载

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| 会话到达 | Poisson；每次到达从 trace 里均匀抽一个会话、整段按原时间线回放；副本间私有页加盐隔离 | 方法设定 |
| 负载锚点 | 选到达率使**理想命中（无限容量、全局共享）下** prefill 占集群 NPU 时间 50% | **假设**（剩余给 decode；与策略无关的锚点） |
| 到达率（全规模） | CC-256k ≈10,871 会话/h；Codex ≈59,259 会话/h；Qwen coder ≈1,626,604 会话/h | 由锚点校准（V4.1 prefill 更便宜，约为上一版的 6.3 倍） |
| 仿真窗口 | 10 h，前 3 h 预热不计 | 方法设定 |
| 规模切片 | 仿真 1/k 集群（每实例 HBM、池容量与负载同比缩小，14 个实例不变），NPU 数再乘 k；主结果 k=32 | 方法设定；与 k=8 对照见 README |
| decode 写出的 KV | **入缓存**：请求结束时，引擎把输入尾页 + 输出写满的页写入本实例 HBM 并写穿到 DRAM 层；理想上限 h* 同样包含 | 对标 vLLM（满块生成 token 入 APC）与 SGLang（请求结束时整段插 radix）；方法设定 |
| 下一轮是否命中输出 | **主结果 = 原样回传**：下一轮输入以本轮输入为前缀、且原样携带本轮全部输出（含思维链）时，输出对应的页记为命中；**下界 = 剥掉思维链**：decode KV 从思维链起与下一轮输入分叉，输出全部不命中（上一版口径） | trace 只给输出 token 数（CC `out`、Qwen `output_length`）或可见文本（Codex），不分思维链/可见；按 trace 统计选主口径，见 README「decode 写出的 KV」 |
| 页粒度 | 512 token | **假设**（CPU 开销折中；vLLM V4 block = 256 源 token） |
| Codex 调用间隔 | lognormal，P50 5.2 s、P90 23.0 s | `Inferact/codex_swebenchpro_traces` README §5（trace 本身无时间戳，时间线为**合成**） |
| Codex / Qwen 服务时长 | 2 s + output / 40 tok/s | **假设**（trace 无服务时长；CC 用 trace 自带 `api_time`） |

## 5. 路由与策略

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| 路由规则 | 负载失衡（max−min > abs 且 max > 1.5×min）走最短队列，否则走最长前缀命中 | SGLang cache-aware 路由规则（`docs/research/sglang/model-runner.md`）；阈值为 **假设** |
| abs 阈值 | 11 个在途请求，**不随切片缩放** | **假设**；CC 的 subagent 扇出让单会话瞬时并发与切片无关，按 32/√k 缩到 k=32 的 6 时 per-instance LRU 命中被压低 4.5 个点（91.2% vs k=8 的 95.7%），取 11 时 k=32 与 k=8 相差 < 0.2 个点 |
| HBM 归属 | 推理引擎：每实例前缀缓存，纯 LRU，**所有策略相同**，固定 296 GB/实例（敏感性 360 GB），不随 x 轴变化 | 用户校正（2026-11-25）；`kvcm/design/D2-kv-operations.md`「操作图解」节：“八项操作都作用于池内副本；HBM 工作集由引擎自己的读写路径加载，不在 kvcm 操作面” |
| x 轴 | 只扫 DRAM 池容量：标称 14 TB × {1/32 … 2}，另加 1× 与 2× 集群 HBM（4.1 / 8.3 TB）两点 | 方法设定 |
| 收益工作区间 | 池 ≈ 1–2× 集群 HBM（4.1–8.3 TB），MRC 图黄色带 | 用户定义（2026-11-25），替代固定 1/8 标称 |
| 重点池容量 | 标称 14 TB 与 1/4 标称 3.5 TB（TTFT / 消融 / 剥掉思维链 / HBM 敏感性） | 方法设定 |
| 基线 1：per-instance LRU | 引擎 HBM LRU + 实例私有 DRAM（write-through，总量同池），LRU | 对标 vLLM APC + 本地 offload（`docs/research/vllm/block-lifecycle.md`） |
| 基线 2：shared-pool LRU | 引擎 HBM LRU + 全集群共享 DRAM 池（去重、任意实例可取），LRU | 对标 Mooncake/LMCache 共享池（被动 LRU） |
| KVCM 池保活 | 停顿期把前缀钉在共享池（ETA ≤ 2 h，且 ≤ 2×池 LRU 驻留时间）；钉预算池 50%（realistic 10%） | **假设**（D8a §5 保留 TTL = ETA + slack） |
| 钉住时长 | oracle：到下一轮到达；realistic：`2 × ETA上界 + 5 s` | **假设** |
| KVCM 零引用优先（池） | 活会话（会话级引用，含在途请求）的池页后驱逐；realistic 活跃窗 = max(4 × ETA上界, 300 s) | lake 原则 4 “引用数>0 冻结”；窗口为 **假设** |
| hook：none | 无停顿信息，池保活/零引用都不生效 | 方法设定 |
| hook：realistic | 只用该流历史间隔：点估计 = 最近 5 次间隔的几何均值，上界 = exp(μ+σ)；冷启动用全局历史中位数 | 方法设定 |
| hook：oracle | 真实间隔，且知道会话/流是否还有下一轮 | 上界 |

## PD 转换（`pd/pd_params.py`）

模型、硬件与 prefill 代价沿用上面各节；PD 实验新增：

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| KV bytes/token | 3,200 B（BF16） | 用户给定（2026-11-25） |
| HBM KV / 节点 | 296 GB（gpu_memory_utilization 0.85 → 18.5 GB/die × 16）；敏感性 360 GB | 用户给定 |
| host 权重副本 | 510 GB/节点，只在 sleep/wake 档从 DRAM 池扣（池 14 TB → 0.49×） | 用户给定；prefill 乘数按 PR #90 MRC（KVCM 现实 hook）插值 |
| decode 每步耗时 t_step | 25 ms（满 KV 的 D 节点）；敏感性 12.5 / 37.5 ms | **假设**（roofline 估算 + 固定开销，A3 无实测） |
| TPOT SLO | 50 ms | `docs/features/slo.md` ITL P50（draft） |
| D 节点 KV 填充上限 | 90% | **假设** |
| P 节点利用率上限 ρ_P | 80% | **假设**（D8a §6.1 ρ_target） |
| 混部节点 prefill 占比上限 | min(ρ_P, 1 − t_step/TPOT_SLO) = 50% | 由上推导 |
| 窗口 | 300 s | 方法设定 |
| goodput 容忍 | 过载窗口 ≤ 1% | 方法设定 |
| P 节点排空 | 2 s | **假设**（≈ TTFT P95 量级） |
| sleep/wake 权重回填 + 唤醒 | 30 s；敏感性 10 / 120 / 600 s | **假设**（PR #89 未实测） |
| 池刷尾页带宽 | 54 GB/s/NPU（FabricMem D2rH） | D8a §9.1 |
| 池重读带宽 | 78 GB/s/NPU | D8a §9.1 |
| 无池点对点迁移带宽 | 23.8 GB/s/NPU（RoCE） | D8a §9.1；16 NPU 线性叠加为**假设**（乐观） |
| 写穿粒度 | 512 token/页（写满才入池） | 与 PR #90 页粒度一致；**假设** |
| 参考负载 | 平均约 12 个节点需求；goodput 以其倍数 α 报告 | 方法设定 |
| 注入场景 | 24h 正弦：总量峰谷 2:1；CC 需求占比 50% ± 40%（强）/ ± 15%（弱）；2h 周期快变 | **注入**，非数据集 |
| 丢 KV 重算档 | 切换 2 s；每切一个节点重算 = E4 杀掉重算满载值（CC 约 6,390 / Qwen 约 5,650 NPU·s）× 切换时刻 D 侧 KV 占用比例，摊 5 分钟进 prefill；P→D 同量 | E4 推导；摊入时长与 P→D 同量为**假设** |
| 统计判据控制器 | 规划窗口 300 / 600 s（翻滚），切片 60 s，合格条件 ḡ > 2σ/√m，切换后 120 s 内或切换未完成时跳过 | Evolving Infra §5.2 / 附录 E.3（论文默认 600 s、m=10） |
| S5 配比阶跃 | P 占比目标 0.5 ↔ 0.8，阶跃于测量段 2/6/10/14/18/22 h，斜坡 5 分钟；Qwen 到达率按 CC 实际需求补齐，总需求≈12 节点；逐阶跃统计负载 α* = 0.95 × 理想动态 P:D goodput | **注入**，非数据集 |

## 6. 池局部性扩展（`kvcm_sim/locality.py`，结果见 [`results/locality.md`](results/locality.md)）

对应 Project 文档《KVCM 池化的近远端、冷热与争用》§4 的三项最小扩展与五组对照。

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| FabricMem 带宽 | 本机 86 GB/s、跨机 78 GB/s（rH2D，AICPU 展开下限；跨机上限 107） | `kvcm/design/D8a-scheduling-algorithms.md` §9.1 A3 910C |
| RoCE 带宽 | 本机 H2D 47 GB/s、跨机 23.8 GB/s | 同上 §8.1「跨机/本机怎么加」与 §9.1 |
| 池块所在节点 | `hash`：按页键哈希条带到 14 节点（扁平池默认）；`local`：写到写入请求所在节点 | `hash` 对标 Mooncake 默认分配；`local` 对标 Mooncake `ReplicateConfig::prefer_alloc_in_same_node` |
| 成本模型 | `flat`：一律跨机带宽、无争用（= 主线口径）；`topo`：分本机/跨机；`contend`：再按 `B/(1+在途流数)` 共享，跨机流同时占源节点出口与读者节点入口（多源并行读总量受入口约束），本机流占本机拷贝引擎 | NetKV（arXiv 2606.03910）`B_eff = B·(1−c)/(1+n_inflight)`，取 c=0 |
| 计入 TTFT 的读时间 | `max(0, τ − W)`；不重叠：读在 prefill 拿到算力后开始（W 不掩盖）；重叠：读从到达开始 | Evolving Infra 附录 E.3 `max(0, τ(x)−W)`；不重叠对应 SGLang HiCache / vLLM connector 的默认加载时机 |
| prefill 排队 | 每实例 16 个槽（1 NPU/请求），槽占用 = A3 计算时长 + 未掩盖读时间 | 方法设定；与主线“单请求 NPU·s = 2 × 拟合 TTFT”一致 |
| 切片口径 | 1/32 切片下计算时长 ×32、每节点带宽 ÷32；在途流数与槽占用按 Little 定律与全规模一致，时间量最后 ÷32 | 方法设定（**近似**：会话内间隔不放大） |
| 副本 | 副本作为独立条目占池容量、参与池 LRU；主副本被驱逐时连带删除 | Evolving Infra “拉取留副本挤掉 agent 的 KV”；D8a §6.5 Cost 项 |
| 被动副本 | 跨机读后读者节点留一份 | Mooncake 论文（持有者过载时转发 + 拉取留存） |
| 热点复制 | 热度 = 池读 EWMA（半衰期 300 仿真秒）；目标副本数 `r = ceil(λ_p × τ_req / ρ)`，ρ=0.5；`hot` 复制到出口最闲的节点（额外搬运计入源出口），`kvcm` 只在路由目标留存刚拉到的字节 | D8a §6.5 `r_target = ceil(λ_p/(μ_p ρ))`；`hot` 对标 TokenLake 热段复制 + 两选一 |
| 读副本选择 | 本机有就读本机；否则在持有者里两选一（出口在途流少者） | TokenLake power-of-two |
| 预测 TTFT 路由 | `argmin_i W_i + 未掩盖读_i + 计算_i` | Mooncake Conductor；Evolving Infra 附录 E.3 |
| 合成倾斜负载 | 共享长文档 32K token，全规模 80,000 篇（切片 2,500 篇；≈ 2× 集群 HBM、0.6× 池），文档按 Zipf(α) 选，α ∈ {0.6, 0.8, 1.0, 1.2, 1.4}；每会话 1–4 轮，每轮 +2 页问题 +1 页输出，间隔 lognormal 中位 20 s；到达率同样按理想命中下 prefill 50% 校准 | **假设**（cache-aware 路由会把文档分摊到各实例 HBM，所以文档总量要 > 集群 HBM、< 池，热文档才会落到池里被并发读）|
