# NVIDIA CMX 分析计算器

这三个页面回答不同问题，不能把结果互换。它们都是参数化分析，不是 CMX benchmark、SKU 或厂商 SLA。

CMX 架构证据见 [`docs/research/nvidia-cmx.md`](../../docs/research/nvidia-cmx.md)；Agentic trace、provider cache 留存和 File Library 边界见 [`docs/research/agentic-cache-workload.md`](../../docs/research/agentic-cache-workload.md)。

## 先打开哪个页面

### 1. [`index.html`](index.html)：一个工作点需要多少流量

适合回答：

- 某模型、context、命中率和 GPU 数下，CMX read/write offered load 是多少；
- P→D direct、P→CMX→D、P/D local 三种 handoff 如何改变流量；
- 用户给定链路预算后，链路最多承载多少 req/s。

主要输入：模型 representation、`C/E/hit`、`q/a`、GPU 数、unique token/s/GPU、handoff 和可选链路预算。

主要输出：bytes/request、GPU-saturated 或外部 `λ`、CMX/P→D GB/s、可选 link ceiling。

它不回答“多少容量能放多少会话”或“90% 到 95% 要多花多少容量”。

### 2. [`capacity.html`](capacity.html)：给定容量能放多少完整会话

适合回答：

- 一张 GPU、一组 G2 内存或一个 CMX POD 能放多少份指定 representation；
- growing KV 与 fixed state 使用不同分片数时，单 GPU HBM 占用是多少；
- Pool 副本和 usable/raw 如何改变会话数。

主要输入：模型 representation、session token、HBM/G2/CMX 容量、分片、副本、usable/raw 和 GB/GiB 单位。

主要输出：每 GPU、rack、POD 的完整会话数。

它从“已有容量”反推会话数，不从命中率推导所需容量。

### 3. [`economics.html`](economics.html)：比较 90% 与 95%

适合回答：

- Prefill GPU 持续跑满时，两个命中率分别需要多少 hot-prefix 和近期写入容量；
- compute-constrained req/s、CMX read/write offered load 分别增加多少；
- 相同介质单价和副本策略下，raw 容量成本增加多少。

主要输入：两个命中率、活跃 session 数、写入保留时间、GPU 计算预算、usable/raw 和副本数。

主要输出：A/B 两组 selected-basis working set、raw 容量、read/write GB/s、compute req/s 及增幅。页面下半部分保留匿名 Cursor trace、provider 留存和 100 GB File Library 的证据。

它不包含 CMX 采购价，也不把 token 命中直接换算成 GPU 美元成本。

## 本地运行

```bash
# 从仓库根目录启动
python3 -m http.server --directory .
# 打开 http://127.0.0.1:8000/tools/cmx-sim/

node tools/cmx-sim/verify.js
```

## GitLab Pages

同一 private Pages 站点包含三个页面：

- <https://lake-13d9f7.gitlab.io/>
- <https://lake-13d9f7.gitlab.io/capacity.html>
- <https://lake-13d9f7.gitlab.io/economics.html>

只有已登录的项目成员可访问。MR !7 发布分支版本；合并后由 `main` 更新。

## 字节口径

页面区分四种字节：

1. **Logical payload**：模型公式要求的有效元素。
2. **Engine entry payload**：每个有效 entry 的实际结构，可能包含 scale/pad。
3. **Engine page allocation**：entry 经 block/page/alignment 和 sliding-window admission 后的分配量。
4. **Custom wire / CMX serialization**：P/D 或 Memos 对象格式。

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

## 流量页公式

```text
M = floor(C × hit / block) × block
U = C − M + E

prefix_cmx_read/request = q × selected_representation(M)

P→D local:    decode_cmx_read=0, direct=0
P→D direct:   decode_cmx_read=0, direct=selected_representation(C+E)
P→CMX→D:      decode_cmx_read=selected_representation(C+E), effective_a=1

cmx_write/request
  = effective_a × [selected_growing(C+E) − selected_growing(M) + final_state]

external λ      = user_supplied_req/s
GPU-saturated λ = effective_unique_tok/s/GPU × GPUs / U

cmx_read_offered  = λ × (prefix_cmx_read + decode_cmx_read)
cmx_write_offered = λ × cmx_write/request
direct_offered    = λ × direct/request
```

- `q` 是命中字节中实际从 CMX 读取的比例；`a` 是写入 CMX 的比例。
- `U=0` 时 GPU-saturated `λ` 无定义，只能使用外部到达率。
- 外部 `λ` 不随命中率变化。
- 链路预算为 0 表示未知；填写后得到用户假设下的 ceiling，不是实测吞吐。

## 容量页公式

```text
hbm_session
  = selected_growing / growing_shard_ranks
  + selected_state / state_shard_ranks

pool_session = selected_total × pool_copies
usable_cmx   = raw_cmx × usable_fraction
sessions     = floor(usable_capacity / session_bytes)
```

分片和副本必须来自实际 TP/CP/engine deployment，页面不从 GPU 数猜测。

## 90% / 95% 对比公式

该页固定使用 GPU-saturated `λ`：

```text
compute_req/s = effective_unique_tok/s/GPU × GPUs / U

hot_prefix
  = active_sessions × selected_representation(M)

retained_writes
  = compute_req/s × retention_seconds × cmx_write/request

raw_storage
  = pool_copies × (hot_prefix + retained_writes) / usable_fraction

capacity_cost_increase
  = raw_storage_B / raw_storage_A − 1

performance_increase
  = compute_req/s_B / compute_req/s_A − 1
```

假设：

- 每个活跃 session 保留一份命中前缀；
- 每次请求的新写入版本在 retention 窗口内互不去重；
- 相同介质单价、usable/raw 和副本策略下，容量成本与 raw bytes 成正比；
- req/s 是计算约束值。若 offered read/write 超过真实链路，GPU 无法持续跑满。

默认 V4-Pro BF16 示例：`C=1,048,576`、72 GPU、10k unique token/s/GPU、10k 活跃 session、10 分钟 retention、80% usable、1 副本。

| 指标 | 90% | 95% | 增幅 |
|---|---:|---:|---:|
| compute req/s | 6.86 | 13.72 | 100.00% |
| CMX read | 63.93 GB/s | 134.95 GB/s | 111.09% |
| CMX write | 7.27 GB/s | 7.46 GB/s | 2.52% |
| raw storage | 110.91 TiB | 116.91 TiB | 5.41% |

95% 时 unique token 减半，所以计算约束 req/s 约翻倍；每请求写入后缀也约减半，因此 growing-write throughput 基本抵消，而 prefix read 随请求率明显上升。

## 实现锚点

- vLLM `vllm/v1/kv_cache_interface.py::MLAAttentionSpec.real_page_size_bytes`：V4 `fp8_ds_mla` main entry 为 584 B。
- vLLM `vllm/v1/kv_cache_interface.py::SlidingWindowSpec.max_admission_blocks_per_request`：sliding-window admission 需额外处理跨 block 窗口。
- vLLM `vllm/models/deepseek_v4/compressor.py::CompressorStateCache`：C4/C128 residual 是 FP32 sliding state。
- vLLM `vllm/models/deepseek_v4/attention.py::DeepseekV4Indexer`：FP8 index entry 为 132 B；FP4 有效 payload 为 68 B，但当前仍按 132 B 分配。
- SGLang `deepseek_v4_memory_pool.py::{DeepSeekV4SingleKVPool,DeepSeekV4IndexerPool,get_compress_state_ring_size}`：584 B main、132/68 B index 和不同 compressor representation。
- vLLM `vllm/model_executor/layers/mamba/mamba_utils.py::{MambaStateDtypeCalculator.kda_state_dtype,MambaStateShapeCalculator.kda_state_shape}`：K3 recurrent/conv state dtype 与 shape。

这些实现给出 engine payload/page/state，不给出 CMX wire。计算器保留这一区别。

来源：

- [vLLM：DeepSeek V4](https://vllm.ai/blog/2026-04-24-deepseek-v4)
- [DeepSeek-V4 paper](https://arxiv.org/abs/2606.19348)
- [GLM-5.2 official model article](https://huggingface.co/blog/zai-org/glm-52-blog)
- [Kimi K3 official model card](https://huggingface.co/moonshotai/Kimi-K3)

后续合同见 [#22](https://gitlab.com/BeeBreeze/lake/-/issues/22) 和 [#23](https://gitlab.com/BeeBreeze/lake/-/issues/23)。
