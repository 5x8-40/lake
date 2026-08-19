# NVIDIA CMX 分析计算器

这四个页面回答不同问题，不能把结果互换。前三页是参数化分析；实测页是两组用户提供数据的回归。它们都不是 CMX benchmark、SKU 或厂商 SLA。

CMX 架构证据见 [`docs/research/nvidia-cmx.md`](../../docs/research/nvidia-cmx.md)；Agentic trace、provider cache 留存和 File Library 边界见 [`docs/research/agentic-cache-workload.md`](../../docs/research/agentic-cache-workload.md)。

## 先打开哪个页面

### 1. [`index.html`](index.html)：Prefill 需要加载多少 KV

适合回答：

- Kimi K3 单用户峰值 workload 对应多少 growing-KV load GB/s；
- 给定真实 req/s 或 GPU 计算预算时，每请求与总 KV load 是多少；
- 用户给定 Pool 读取预算后，最多能承载多少 req/s。

主要输入：模型 representation、`C/E/hit`、远程读取比例 `q`，以及 Agentic peak token/h、GPU 计算预算或外部 req/s 三种负载来源。

主要输出：访问请求吞吐 req/s、P 侧 growing/fixed/complete KV load GB/s，以及可选 Pool-read ceiling。

默认 preset 使用 Kimi K3：

- 长期 token-weighted hit：`91.9028%`；
- 单用户峰值小时：`21.215355M prompt token/h`、`21.332065M total token/h`；
- 同小时有 5 个 Cursor usage events；event 是聚合账单行，不是 5 个模型请求；
- trace 没有真实 request/resume rate，所以 req/s 和 KDA fixed-state load bandwidth 显示 `N/A`。

它不回答“多少容量能放多少会话”或“90% 到 95% 要多花多少容量”。

### 2. [`capacity.html`](capacity.html)：GPU 跑满后需要多少 KV 容量

适合回答：

- 给定用户数和 APC 命中率，Prefill GPU 跑满 5 分钟、30 分钟、1 小时会保留多少 KV；
- 同一用户的请求复用一段 KV 时，命中部分与新算部分分别占多少空间；
- Pool 副本和 usable/raw 如何放大物理容量。

主要输入：模型 representation、每请求 context、两个命中率、Prefill GPU 数、`unique token/s/GPU`、retained user 数、副本和 usable/raw。

主要输出：两个命中率下的 compute req/s、每用户共享锚点、新 KV 生成速率，以及 5/30/60 分钟的 logical/raw 容量。

它假设窗口开始时每个 retained user 已有一段热缓存，窗口内不驱逐；不同用户之间不去重。

### 3. [`economics.html`](economics.html)：比较 90% 与 95%

适合回答：

- Prefill GPU 持续跑满时，两个命中率分别需要多少 hot-prefix 容量；
- compute-constrained req/s 和 P 侧 KV load bandwidth 分别增加多少；
- 相同介质单价和副本策略下，raw 容量成本增加多少。

主要输入：两个命中率、活跃 prefix 数、GPU 计算预算、usable/raw 和副本数。未实测的 retained writes 是可选高级项，默认关闭。

主要输出：A/B 两组 hot-prefix/raw 容量、KV load GB/s、compute req/s 及增幅。页面下半部分保留匿名 Cursor trace、provider 留存和 100 GB File Library 的证据。

它不包含 CMX 采购价，也不把 token 命中直接换算成 GPU 美元成本。

### 4. [`measured.html`](measured.html)：查看实测点与拟合

适合回答：

- 两组设备、三个模型在 56%/90%/99% APC 命中率下的 TTFT、KV 量和带宽是多少；
- 给定 hit 与序列长度时，各指标的回归预测是多少；
- 每条拟合的 R² 和相对 RMSE 是否足以支持当前用途。

主要输入：设备 A/B、模型、指标，以及任意 APC 命中率和 K-token 序列长度。

主要输出：实测点、拟合曲线、当前点预测、系数和误差。页面下方保留两张完整原始表，并链接到 MR !7 comment `3702930653`、`3702931264`。

原 comment 未提供 GPU 型号和测试方法。Qwen3-235B 无 Cache TTFT/TPS 只展示，不参与拟合；范围外预测属于外推。

## 本地运行

```bash
# 从仓库根目录启动
python3 -m http.server --directory .
# 打开 http://127.0.0.1:8000/tools/cmx-sim/

node tools/cmx-sim/verify.js
node tools/cmx-sim/verify-measured.js
```

## GitLab Pages

同一 private Pages 站点包含四个页面：

- <https://lake-13d9f7.gitlab.io/>
- <https://lake-13d9f7.gitlab.io/capacity.html>
- <https://lake-13d9f7.gitlab.io/economics.html>
- <https://lake-13d9f7.gitlab.io/measured.html>

只有已登录的项目成员可访问。MR !7 发布分支版本；合并后由 `main` 更新。

## 字节口径

页面区分四种字节：

1. **Logical payload**：模型公式要求的有效元素。
2. **Engine entry payload**：每个有效 entry 的实际结构，可能包含 scale/pad。
3. **Engine page allocation**：entry 经 block/page/alignment 和 sliding-window admission 后的分配量。
4. **Custom wire / CMX serialization**：Pool / Memos 对象格式。

CMX wire 尚未公开，不能用 logical payload 或 HBM page 代替。当前 engine-page 结果只覆盖审计过的 V4 vLLM base-ring profile；其他 profile 返回 `N/A`。

### DeepSeek V4 blog 对齐

vLLM 2026-04-24 blog 的 **9.62 GiB** 使用 `1 Mi-token = 1,048,576 token`，并计算 V4-Pro BF16 的 **growing KV + 128-token SWA**：

```text
c4a/layer
  = (128 + 1,048,576 / 4) × 1024 + (1,048,576 / 4) × 256
  = 320.125 MiB
c128a/layer
  = (128 + 1,048,576 / 128) × 1024
  = 8.125 MiB
paged KV
  = 30 × 320.125 MiB + 31 × 8.125 MiB
  = 9.624633789 GiB ≈ 9.62 GiB
```

该 headline 不含 compressor continuation state 或 allocator page：

| V4-Pro BF16 @ 1,048,576 | GiB |
|---|---:|
| growing KV | 9.6171875 |
| blog paged KV（growing + 7.625 MiB SWA） | 9.624633789 |
| + 17.84375 MiB compressor state | 9.642059326 |
| vLLM settled engine pages | 9.647872925 |

blog 的 V3.2-style 对照为 `83.875 GiB`，所以缩减比例约 `8.71×`。若把“1M”解释成十进制 `1,000,000`，同一 paged-KV 公式得到 `9.17910 GiB`；两种 token 口径不能混用。

V4-Flash 的绝对 GiB 是根据 21 个 c4a、20 个 c128a 和 2 个 SWA-only layer 推导，blog 没有发布该绝对值。

### 其他 1 Mi-token 校验点

| 模型/profile | growing payload | fixed/window state | transferable payload |
|---|---:|---:|---:|
| V4-Flash BF16 | 6.7188 GiB | 5.3750 MiB SWA + 11.6406 MiB compressor | 6.7354 GiB |
| V4-Pro FP8 main + FP4 index payload | 4.9135 GiB | 4.3486 MiB SWA + 17.8438 MiB compressor | 4.9352 GiB |
| GLM-5.2 FP8 logical | 46.5820 GiB | 0 | 46.5820 GiB |
| GLM-5.2 FP8 logical + MTP | 47.2734 GiB | 0 | 47.2734 GiB |
| Kimi K3 FP8 MLA + vLLM KDA | 13.5000 GiB | 428.5547 MiB | 13.9185 GiB |

V4 base/speculative/online-C128 compressor 是不同 representation；V4/GLM MTP 是可选 component。K3 KDA recurrent state 是 FP32，不能只算 growing MLA。

## KV 加载页公式

```text
M = floor(C × hit / block) × block
U = C − M + E

growing_load/request = q × selected_growing(M)
state_load/request   = q × selected_state(M)
complete_load/request = growing_load + state_load

external req/s      = user_supplied_req/s
GPU-saturated req/s = effective_unique_tok/s/GPU × GPUs / U

KV_load_bandwidth = req/s × complete_load/request
```

- `q` 是命中字节中实际从 Pool 读取的比例。
- `U=0` 时 GPU-saturated req/s 无定义，只能使用外部请求率。
- 外部 req/s 不随命中率变化。
- Pool 读取预算为 0 表示未知；填写后得到用户假设下的 ceiling，不是实测吞吐。

Agentic preset 没有 request 数，直接按 token demand 计算：

```text
peak_prompt_token/s = 21,215,355 / 3600
cache_read_token/s  = peak_prompt_token/s × 91.9028%
growing_KV_load     = cache_read_token/s × selected_growing_bytes/token × q
```

Kimi K3 FP8 的 `selected_growing_bytes/token = 24 × 576 = 13,824 B`，默认 growing-KV load 为 `0.07487 GB/s`。原始峰值小时实际 Cache Read 为 `20,131,868 token/h`，对应 `0.07731 GB/s`。两者不同，是因为 preset 组合了长期平均 hit 与峰值 prompt demand。

KDA fixed state 不是 token-proportional。没有底层 request/resume rate 时，完整 KV load 不能计算，页面明确显示 `N/A`。

## 容量页公式

```text
M = floor(C × hit / block) × block
U = C − M + E
Q = GPUs × effective_unique_token/s/GPU
compute_req/s = Q / U

anchor/user
  = selected_growing(M) + latest_continuation_state
user_anchor
  = retained_users × anchor/user

new_growing(T)
  = compute_req/s × T × [selected_growing(C + E) − selected_growing(M)]

logical_capacity(T)
  = user_anchor + new_growing(T)
raw_capacity(T)
  = logical_capacity(T) × pool_copies / usable_fraction
```

含义：

- 一个用户只有一段共享 KV，命中块只在 `user_anchor` 中计一次，不按请求重复写；
- 窗口内所有新算 growing KV 都保留；5/30/60 分钟分别使用 `T=300/1800/3600s`；
- continuation state 只保留每用户最新一份，不乘请求数；
- 对 Kimi K3 的线性 growing KV，GPU 满载时 `new_growing/s = Q × bytes/token`，与命中率无关；高命中率会提高 req/s，并扩大每用户起始热锚点；
- `usable_fraction=0` 表示未知，raw 容量显示 `N/A`；
- 若 `U=0`，没有 unique token 可让 GPU 跑满，compute req/s 无定义。

默认示例：Kimi K3 FP8、`C=1,048,576`、72 GPU、10k unique token/s/GPU、1000 retained users、1 副本、80% usable。

| 指标 | 90% | 95% |
|---|---:|---:|
| compute req/s | 6.86 | 13.72 |
| 用户锚点 | 12.27 TiB | 12.93 TiB |
| 新 KV 生成速率 | 9.95 GB/s | 9.95 GB/s |
| 5 min raw | 18.74 TiB | 19.56 TiB |
| 30 min raw | 35.71 TiB | 36.53 TiB |
| 1 h raw | 56.08 TiB | 56.90 TiB |

## 90% / 95% 对比公式

该页固定使用 GPU-saturated req/s：

```text
compute_req/s = effective_unique_tok/s/GPU × GPUs / U

hot_prefix
  = active_sessions × selected_representation(M)

retained_writes
  = 0                                      # default
  = compute_req/s × retention × write/request  # optional

raw_storage
  = pool_copies × (hot_prefix + retained_writes) / usable_fraction

capacity_cost_increase
  = raw_storage_B / raw_storage_A − 1

performance_increase
  = compute_req/s_B / compute_req/s_A − 1
```

假设：

- 每个活跃 session 保留一份命中前缀；
- active session/prefix 数不是 Cursor trace 实测值，默认 `1` 表示 per-prefix 归一化；
- retained writes 默认关闭；启用后才假设窗口内新版本互不去重；
- 相同介质单价、usable/raw 和副本策略下，容量成本与 raw bytes 成正比；
- req/s 是计算约束值。若 KV load 超过真实 Pool 读取能力，GPU 无法持续跑满。

默认 Kimi K3 FP8 示例：`C=1,048,576`、72 GPU、10k unique token/s/GPU、1 个 active prefix、retained writes 关闭、80% usable、1 副本。

| 指标 | 90% | 95% | 增幅 |
|---|---:|---:|---:|
| compute req/s | 6.86 | 13.72 | 100.00% |
| P 侧 KV load | 92.56 GB/s | 195.08 GB/s | 110.75% |
| raw storage / active prefix | 15.71 GiB | 16.55 GiB | 5.38% |

95% 时 unique token 减半，所以计算约束 req/s 约翻倍；每请求加载的前缀更长，因此 KV load bandwidth 增幅超过 100%。Kimi 的固定 KDA state 不随 prefix token 线性增长，所以结果略低于理想化的 `111.11%`。

## 实测页拟合公式

`N` 使用原表的 K-token 单位。每个设备、每个模型独立拟合：

```text
TTFT       = c + (1-h) × (aN + bN²)
单层读KV   = k × h × N
读带宽      = d_read × h × N / fitted_TTFT
写带宽      = d_write × (1-h) × N / fitted_TTFT
1H新增KV    = 写带宽 × 3600 / 1024
1H处理总量  = 1H新增KV / (1-h)
```

- TTFT 使用相对误差加权最小二乘，避免 1M 序列完全支配短序列；
- 单层读 KV 的 `k` 使用过原点最小二乘；
- `d_read`、`d_write` 分别最小化相对带宽误差。V4 的读写 representation 不对称，不能套用 `写=读×(1-h)/h`；
- 页面公开每个指标的普通 R² 和相对 RMSE；小时量沿用原表的 `1024 GB = 1 TB` 口径；
- 两张原始表的小时量已四舍五入，拟合预测中的小时换算保持精确恒等；
- 无 Cache 列、OOM 和空值不进入拟合。

## 实现锚点

- vLLM `vllm/v1/core/kv_cache_manager.py::KVCacheManager.get_computed_blocks`：APC 将命中 block 与待计算 token 分开。
- SGLang `python/sglang/srt/mem_cache/radix_cache.py::RadixCache.match_prefix`：APC hit 表示连续前缀命中，不是任意 KV 字节命中。
- vLLM `vllm/v1/kv_offload/base.py::OffloadPolicy.BLOCK_LEVEL`：只 offload 新计算 block，跳过已存的 prefix-hit block。
- SGLang `python/sglang/srt/mem_cache/radix_cache.py::RadixCache.cache_finished_req`：插入完成请求后释放树中已有的重复 KV。
- LMCache `lmcache/v1/token_database.py::ChunkedTokenDatabase._prefix_hash/process_tokens`：链式内容哈希使相同前缀块共用 key。
- SGLang `python/sglang/srt/mem_cache/hiradix_cache.py::prefetch_from_storage` + `cache_controller.py::_page_get_zero_copy`：P 侧从外部存储加载 KV。
- vLLM `vllm/v1/kv_cache_interface.py::MLAAttentionSpec.real_page_size_bytes`：V4 `fp8_ds_mla` main entry 为 584 B。
- vLLM `vllm/v1/kv_cache_interface.py::SlidingWindowSpec.max_admission_blocks_per_request`：sliding-window admission 需额外处理跨 block 窗口。
- vLLM `vllm/models/deepseek_v4/compressor.py::CompressorStateCache`：C4/C128 residual 是 FP32 sliding state。
- vLLM `vllm/models/deepseek_v4/attention.py::DeepseekV4Indexer`：FP8 index entry 为 132 B；FP4 有效 payload 为 68 B，但当前仍按 132 B 分配。
- SGLang `deepseek_v4_memory_pool.py::{DeepSeekV4SingleKVPool,DeepSeekV4IndexerPool,get_compress_state_ring_size}`：584 B main、132/68 B index 和不同 compressor representation。
- vLLM `vllm/model_executor/layers/mamba/mamba_utils.py::{MambaStateDtypeCalculator.kda_state_dtype,MambaStateShapeCalculator.kda_state_shape}`：K3 recurrent/conv state dtype 与 shape。

这些实现给出 APC 匹配、P 侧加载和 engine payload/page/state，不给出用户 request trace 或 CMX wire。计算器保留这些区别。

来源：

- [vLLM：DeepSeek V4](https://vllm.ai/blog/2026-04-24-deepseek-v4)
- [DeepSeek-V4 paper](https://arxiv.org/abs/2606.19348)
- [GLM-5.2 official model article](https://huggingface.co/blog/zai-org/glm-52-blog)
- [Kimi K3 official model card](https://huggingface.co/moonshotai/Kimi-K3)

后续合同见 [#22](https://gitlab.com/BeeBreeze/lake/-/issues/22) 和 [#23](https://gitlab.com/BeeBreeze/lake/-/issues/23)。
