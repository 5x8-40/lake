# KVCM trace 回放仿真：参数表

每个参数标注来源；没有可核实来源的一律标 **假设**，等待用户校正。代码里的对应值在 [`kvcm_sim/params.py`](kvcm_sim/params.py)（硬件/模型/算力）与 [`kvcm_sim/replay.py`](kvcm_sim/replay.py) `Config`（策略/路由）。两处改动要同步本表。

## 1. 模型：DeepSeek V4.1

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| 层结构 | 与 V4-Pro 相同：30 层 C4（4:1 压缩 + indexer）+ 31 层 C128（128:1 压缩）+ 1 层 MTP | **假设**（仓库无 V4.1 资料）；V4-Pro 结构取自 `tools/cmx-sim/sim.js` `MODELS.v4pro`（vLLM 2026-04-24 附录 + DeepSeek-V4 §4.2.1） |
| KV 精度 | BF16 | **假设**：A3（910C）无 FP8 计算；仓库 V3.2 昇腾口径同样按 BF16（`kvcm/design/D8-joint-scheduling.md` 83.9 KiB/token） |
| growing KV bytes/token | **9,848 B**（= 30×(1024+256)/4 + 31×1024/128） | 由 `tools/cmx-sim/sim.js` `PROFILES.v4pro` `bf16-logical`（1024 B main / 256 B index）推导 |
| 同上（FP8 敏感性） | 5,511 B（584 / 132 B entry） | `tools/cmx-sim/sim.js` `fp8-vllm`（vLLM FP8 entry payload） |
| 每会话固定状态（SWA + compressor） | ≈27 MB/会话，**仿真忽略** | 由 `sim.js` `v4CompressorStateBytes` / SWA 128 token 推导；100K 上下文时约占 2.7%，在回放精度以内 |
| 权重 | 1.6 TB | **假设**：V4-Pro 1.6T 参数，W8A8 1 B/参数 |

## 2. 硬件：华为昇腾 A3 集群

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| 规模 | 14 节点 × 16 NPU = 224 NPU | 用户给定（2026-11-24） |
| HBM / NPU | 64 GB | **假设**：910C 每芯片 128 GB、2 die，按 die 计 NPU（16 NPU/节点 = 8 芯片） |
| 实例切分 | 2 节点 / 实例（32 NPU），共 7 个实例；PD 混部 | **假设**：1.6 TB 权重单节点（1 TB HBM）放不下 |
| 激活 / workspace 预留 | 4 GB / NPU | **假设** |
| HBM KV / 实例 | **320 GB**（= 32 × (64 − 50 权重 − 4 预留)）；全集群 2.24 TB ≈ 2.27 亿 token | 由上面三项推导 |
| DRAM KV / 节点 | **1 TB**；全集群池 14 TB ≈ 14.2 亿 token | **假设**（Atlas 800T A3 主机内存配置与 KV 可用比例待确认） |
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
| A3 TTFT | `2 × TTFT_fit`（同等每卡并发）+ 未掩盖的 KV 加载时间 | D8a §3.2 “掩不住”式；换算系数为 **假设** |
| 等效 NPU 数 | `Σ_请求 (NPU·s_基线 − NPU·s_KVCM) / 墙钟秒` | D8a §8.2 |
| 排队 | 不建模（无 DES） | 局限，见 README |

## 4. 负载

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| 会话到达 | Poisson；每次到达从 trace 里均匀抽一个会话、整段按原时间线回放；副本间私有页加盐隔离 | 方法设定 |
| 负载锚点 | 选到达率使**理想命中（无限容量、全局共享）下** prefill 占集群 NPU 时间 50% | **假设**（剩余给 decode；与策略无关的锚点） |
| CC-256k 到达率 | ≈1,750 会话/h ≈ 59 req/s（全规模） | 由锚点校准 |
| 仿真窗口 | 10 h，前 3 h 预热不计 | 方法设定 |
| 规模切片 | 仿真 1/k 集群（容量与负载同比缩小，7 个实例不变），NPU 数再乘 k；主结果 k=8 | 方法设定；k=4 与 k=16 对照见 README |
| 页粒度 | 512 token | **假设**（CPU 开销折中；vLLM V4 block = 256 源 token） |
| Codex 调用间隔 | lognormal，P50 5.2 s、P90 23.0 s | `Inferact/codex_swebenchpro_traces` README §5（trace 本身无时间戳，时间线为**合成**） |
| Codex / Qwen 服务时长 | 2 s + output / 40 tok/s | **假设**（trace 无服务时长；CC 用 trace 自带 `api_time`） |

## 5. 路由与策略

| 参数 | 取值 | 来源 / 标签 |
|---|---|---|
| 路由规则 | 负载失衡（max−min > abs 且 max > 1.5×min）走最短队列，否则走最长前缀命中 | SGLang cache-aware 路由规则（`docs/research/sglang/model-runner.md`）；阈值为 **假设** |
| abs 阈值 | 全规模 32 个在途请求，切片按 32/√k 缩放 | **假设** |
| 基线 1：per-instance LRU | 每实例私有 HBM + 私有 DRAM（write-through），LRU | 对标 vLLM APC + 本地 offload（`docs/research/vllm/block-lifecycle.md`） |
| 基线 2：shared-pool LRU | 每实例 HBM + 全集群共享 DRAM 池（去重、任意实例可取），LRU | 对标 Mooncake/LMCache 共享池（被动 LRU） |
| KVCM keep-alive | 停顿期把前缀钉在 HBM（ETA ≤ 30 s）或共享池（ETA ≤ 2 h，且 ≤ 2×该层 LRU 驻留时间）；钉预算 HBM 30%、池 50% | **假设**（D8a §5 保留 TTL = ETA + slack） |
| 钉住时长 | oracle：到下一轮到达；realistic：`2 × ETA上界 + 5 s` | **假设** |
| KVCM ETA 预热 | 预测 ETA 超过 HBM 驻留时间时，在 ETA − 5 s 把前缀从池预放置到目标实例 HBM | D8a §4 预取；提前量为 **假设** |
| KVCM 零引用优先 | 活会话（会话级引用，含在途请求）的页后驱逐；realistic 活跃窗 = max(4 × ETA上界, 300 s) | lake 原则 4 “引用数>0 冻结”；窗口为 **假设** |
| hook：none | 无停顿信息，keep-alive/预热/零引用都不生效 | 方法设定 |
| hook：realistic | 只用该流历史间隔：点估计 = 最近 5 次间隔的几何均值，上界 = exp(μ+σ)；冷启动用全局历史中位数 | 方法设定 |
| hook：oracle | 真实间隔，且知道会话/流是否还有下一轮 | 上界 |
