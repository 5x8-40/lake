# SGLang — Worker / ModelRunner / Spec Decode

> 源码:`3rdparty/sglang`(submodule)。路径前缀默认 `python/sglang/srt/`。  
> 本文补齐此前缺口:既有 [overview.md](overview.md) / [hicache.md](hicache.md) 只覆盖分层 KV,未单列计算执行路径。  
> lake 侧架构落点见 [`../../architecture/compute-layer.md`](../../architecture/compute-layer.md)「投机解码」;vLLM 对照见 [`../vllm/compute.md`](../vllm/compute.md)。

## 一句话定位

SGLang 的计算热路径是 **Scheduler 组 `ScheduleBatch` → (可选 SpecWorker 编排) → `TpModelWorker` → `ModelRunner.forward/sample`**。请求语义留在 host 侧 Python `Req`;device 侧只放 block table、逐步 batch tensor、以及投机所需的 seed / draft KV。投机一律 **spec-v2、drafter 与 target 共置串行**(draft → target verify → draft-extend/inject)。

## 架构总览

```
TokenizerManager  --ZMQ-->  Scheduler
                              |  Req* + ScheduleBatch
                              v
                    model_worker.forward_batch_generation
                              |
              ┌───────────────┴───────────────┐
              | non-spec                      | spec-v2
              v                               v
         TpModelWorker                 BaseSpecWorker
              |                    (EAGLE / DFLASH / DSPARK / …)
              |                         |              |
              |                    draft TpWorker   target TpWorker
              v                         v              v
         ModelRunner.forward/sample   ModelRunner   ModelRunner
              |
   req_to_token_pool (shared map, device)
   token_to_kv_pool  (target) + draft pool (PoolName.DRAFT, 若有)
```

| 层 | 职责 | 关键符号 |
|----|------|----------|
| TokenizerManager | HTTP/API、tokenize、等 detokenize | `managers/tokenizer_manager.py::TokenizerManager` |
| Scheduler | 队列、前缀命中、KV 分配写 `req_to_token`、组 batch、结果回传 | `managers/scheduler.py::Scheduler.run_batch` |
| TpModelWorker | 薄封装:`ForwardBatch.init_new` → runner forward → sample | `managers/tp_worker.py::TpModelWorker.forward_batch_generation` |
| BaseSpecWorker | 同 step 内编排 draft / verify / extend | `speculative/base_spec_worker.py::BaseSpecWorker` |
| ModelRunner | 权重、内存池、attn backend、CUDA graph、前向/采样 | `model_executor/model_runner.py::ModelRunner` |

### 初始化顺序(`Scheduler.init_model_worker`)

1. `init_tp_model_worker` → target `TpModelWorker`(`ModelRunner.initialize` → `load_model`)
2. `maybe_init_draft_worker` → `SpeculativeAlgorithm.create_worker(...)`
3. `init_memory_pools` — target 先建池;draft **共享** `req_to_token_pool`,自建 `token_to_kv_pool`
4. `init_all_attention_backends` / `init_all_cuda_graphs`
5. `self.model_worker = tp_worker` 或 spec worker(启用投机时)

---

## Batch 类型阶梯

旧路径里的 `ModelWorkerBatch` / `ScheduleBatch.get_model_worker_batch` **已删除**。今天只有:

```
Req  →  ScheduleBatch  →  ForwardBatch
         (调度侧)           (前向侧;多数 GPU 字段直接借用 SB)
```

`ForwardMode`(`model_executor/forward_batch_info.py::ForwardMode`)常用值:

| Mode | 用途 | SGLang 的分类含义 |
|------|------|---------------------|
| `EXTEND` | Prefill / 残差 prefill | 常规多 token context forward |
| `DECODE` | 普通 decode | 每请求一个 query token 的延迟敏感路径 |
| `MIXED` | chunked / 混批 | 同一 batch 同时含 extend 与 decode 几何 |
| `TARGET_VERIFY` | target 并行验证 draft(含 DFLASH 家族的 draft 前向) | 逻辑上归 `is_extend()`，但可走 decode graph |
| `DRAFT_EXTEND_V2` | EAGLE 家族 draft-extend(同步 draft KV + seed) | 仅在显式允许时才计入 extend 谓词 |
| `PREBUILT` / `SPLIT_PREFILL` | PD decode ready / PD multiplexing prefill | SGLang 的分相、PD 专用标签 |
| `DLLM_EXTEND` | Diffusion LLM mask block 推理 | 不是自回归 token decode，另有请求 phase/结果处理 |
| `IDLE` | 无本地请求的陪跑 step | DP/collective 对齐，不等同 graph capture dummy |

`CaptureHiddenMode`:`NULL` / `LAST` / `FULL` — 投机决定 target 是否吐 hidden states 给 draft。

**不要把这个枚举当作唯一执行几何。** `ForwardMode.is_extend()` 把 `MIXED`、`TARGET_VERIFY`、`SPLIT_PREFILL`、`DLLM_EXTEND` 都归为 extend，而 `is_decode()` 只承认普通单 token `DECODE`。这是为了复用调度、attention 和并行通信分派中的已有分支；target verify 的 `q_len>1` 并不自动意味着它只能使用 prefill kernel 或 prefill graph。

**对 lake**：应保留“每请求 `num_computed_tokens + 本步 q_len`”作为权威执行几何；普通生成关闭投机时就是 `q_len=1`，投机 verify 时是 `q_len>1`。可以不保留 SGLang 式持久 Prefill/Decode 状态机，但仍要从几何派生批标签（attention layout、graph bucket、MoE 通信、日志），否则“全部叫 extend”会掩盖关键执行差异。

### 字段驻留(host vs device)

| 类别 | Req(Python/CPU) | ScheduleBatch | ForwardBatch |
|------|------------------|---------------|--------------|
| 完整 token 历史 | `origin_input_ids` / `output_ids` | prefill:`prefill_input_ids_cpu`(pinned)→ resolve 后 `input_ids` **GPU**;decode:从 FutureMap gather（见「Overlap schedule」） | 借用 `input_ids`(GPU) |
| positions | — | — | `init_new` 构造(GPU) |
| seq lens | 派生 `seqlen` | `seq_lens` GPU + `seq_lens_cpu` | 借用 |
| `req_pool_idx` | host `int` | `req_pool_indices` GPU(+cpu 镜像) | 借用 |
| `out_cache_loc` | — | `prepare_for_extend/decode` 分配(GPU) | 借用 |
| block table | — | 经 `alloc_for_*` 写入 `req_to_token` | attn 经 pool 索引读 |
| extend 元数据 | `extend_range` 等 | `prefix_lens`/`extend_lens` **CPU list** | H2D → GPU + cpu 镜像 |
| sampling | `sampling_params` | `SamplingBatchInfo` | 同对象 |
| spec | — | `spec_info: SpecInput`(多为 GPU tensor) | 同对象 |

`overlap_utils.py::resolve_forward_inputs`:prefill 做 H2D;decode 从 `FutureMap.output_tokens_buf[req_pool_indices]` gather 上一步采样 token。

---

## ModelRunner 职责

`model_executor/model_runner.py::ModelRunner`

| 阶段 | 符号 | 做什么 |
|------|------|--------|
| 加载 | `initialize` → `load_model` | 分布式环境、权重上 GPU、sampler |
| 内存 | `alloc_memory_pool` | `req_to_token_pool` + `token_to_kv_pool` + allocator |
| Attn | `init_attention_backends` | backend;EAGLE3 可配 aux hidden capture |
| Graph | `init_cuda_graphs` 等 | decode/prefill graph capture |
| 前向 | `forward` → `_forward_raw` | graph replay 或 eager |
| 采样 | `sample` / `compute_logprobs_only` | logits → token |

### 关键 GPU 常驻结构

| 结构 | 位置 | 说明 |
|------|------|------|
| 权重 | `ModelRunner.model` | device |
| Req→token KV 索引 | `mem_cache/memory_pool.py::ReqToTokenPool.req_to_token` | `(size+1, max_context_len)` int32 **device**;行=请求槽,列=token 位 → KV pool index |
| Token→KV 页 | `token_to_kv_pool`(`MHATokenToKVPool` / MLA / hybrid…) | per-layer K/V **device** |
| 空闲请求槽 | `ReqToTokenPool.free_slots` | **host** Python list |
| 前缀树元数据 | Scheduler `tree_cache`(Radix/HiRadix) | **host** |
| 逐步 batch tensor | SB/FB 的 `input_ids`/`seq_lens`/`out_cache_loc`/`positions` | **device** |

分配写路径:`mem_cache/allocation.py::alloc_for_extend` / `alloc_for_decode` → `req_to_token_pool.write`。

---

## 请求数据结构:什么在 device 上?

**结论:没有「整份 Req 上 device」。** 重要语义(结束条件、采样参数、前缀节点、输出文本)留在 host `Req`;device 只镜像执行必需的索引与逐步输入。

### 留在 host 的 `Req` 字段(`managers/schedule_batch.py::Req`)

- 身份/文本:`rid`、`origin_input_text`、`origin_input_ids`、`output_ids`
- 控制:`sampling_params`、finish reason、stream/logprob/LoRA/grammar(guided decoding 与 overlap/spec 同步见 [../guided-decoding.md](../guided-decoding.md))
- 调度:`kv_committed_len`、`ReqKvInfo`、`extend_range`、chunked 计数
- 缓存策略:`last_node`、host hit 长度、`cache_protected_len`
- `req_pool_idx`:host `int`,指向 device 表的一行

### 在 device 上的镜像

| 内容 | 载体 |
|------|------|
| block table 行 | `req_to_token[req_pool_idx, :]` |
| 本步 input ids / seq lens / out_cache_loc | ScheduleBatch / ForwardBatch GPU tensor |
| 前缀 KV locs(常) | `req.prefix_indices`(GPU tensor) |
| EAGLE seed | `EagleDraftInput.hidden_states` / `topk_*` / `bonus_tokens`(GPU) |
| DFLASH/DSPARK 跨步状态 | `DFlashDraftInputV2.bonus_tokens`、`new_seq_lens`、confidence(GPU) |
| overlap 中继 | `FutureMap` 只中继 token ids / 部分 spec extras,不中继整份 Req |

Scheduler 完成 finish / cache / 输出组装**始终依赖 host `Req`**。

---

## 投机解码框架

### 算法枚举与选 worker

`speculative/spec_info.py::SpeculativeAlgorithm`:

| 枚举 | Worker | 备注 |
|------|--------|------|
| `EAGLE` / `EAGLE3` | `EAGLEWorkerV2` 或 `MultiLayerEagleWorkerV2` | `enable_multi_layer_eagle` 时走多层 |
| `FROZEN_KV_MTP` | `FrozenKVMTPWorkerV2` | assistant 读 **target** KV,无 draft KV |
| `DFLASH` | `DFlashWorkerV2` | diffusion 块 draft |
| `DSPARK` | `DSparkWorkerV2` | DeepSeek-V4 self-draft;唯一 `supports_ragged_verify()` |
| `STANDALONE` / `NGRAM` | 对应 worker | lake 不主攻 |

**NEXTN 不是独立枚举**:CLI 别名经 `arg_groups/speculative_hook.py::_resolve_speculative_algorithm_alias` 解析为 `EAGLE`(Gemma4 assistant draft → `FROZEN_KV_MTP`)。

工厂:`SpeculativeAlgorithm.create_worker` / 插件 `spec_registry.py::register_algorithm`。

谓词速查:

| 谓词 | 含义 |
|------|------|
| `is_eagle()` | EAGLE / EAGLE3 / FROZEN_KV_MTP |
| `is_dflash_family()` | DFLASH + DSPARK |
| `supports_target_verify_for_draft()` | draft 前向用 `TARGET_VERIFY` |
| `supports_ragged_verify()` | **仅 DSPARK** |
| `carries_draft_hidden_states()` | PD disagg 需传 draft seed HS |
| `has_draft_kv()` | NGRAM 为 false |

### 共享契约(`base_spec_worker.py`)

- `BaseSpecWorker`:`target_worker` + `draft_worker` + `forward_batch_generation` + 池/graph 初始化
- `EagleDraftWorkerBase`:`draft()` / `draft_extend()`(EAGLE 家族)

**EAGLE 家族 decode 一步时序**:

```
1. draft()          # pre-target:产 EagleVerifyInput
2. target verify    # TARGET_VERIFY + accept/reject
3. on_publish(seq)  # overlap fence
4. draft_extend()   # post-target:同步 draft KV + 下一轮 seed
```

**DFLASH 家族**:无 `EagleDraftWorkerBase`;draft 是普通 `TpModelWorker`;用 **inject target HS → draft KV** 代替 draft-extend 前向。

拒绝采样:`reject_sampling.py::chain_speculative_sampling_triton`(Leviathan;topk=1/chain)。  
Ragged verify:`ragged_verify.py::RaggedVerifyLayout`(仅 DSPARK)。

### Draft KV 池

- Draft **共享** `req_to_token_pool`(同槽位布局),**自建** `token_to_kv_pool`(层数常更少)
- HiCache 命名:`mem_cache/hicache_storage.py::PoolName.DRAFT`;L3 key 后缀 `.{PoolName.DRAFT}`
- 注册:`HiCacheController.set_draft_kv_pool` / `maybe_register_hicache_draft`

---

## 各方案适配

### 1. EAGLE / EAGLE3

| 项 | 内容 |
|----|------|
| 入口 | `eagle_worker_v2.py::EAGLEWorkerV2` + `EagleDraftWorker` |
| I/O | `eagle_info.py::{EagleDraftInput, EagleVerifyInput, EagleDraftExtendInput}` |
| 共用 | `eagle_worker_common.py::{prepare_for_draft, run_eagle_verify, build_eagle_verify_input}` |

**Prefill**:target(`CaptureHiddenMode.FULL`) → publish → `_draft_extend_for_prefill`(填 draft KV + seed)。  
**Decode**:`draft` → `verify` → publish → `_draft_extend_for_decode`。

| 从 target 取 | 用途 | 驻留 |
|--------------|------|------|
| hidden states(FULL/LAST) | draft-extend / 下一轮 seed | GPU `[tokens或bs, H]` |
| logits | verify 采样 | GPU |
| KV | target 自有;draft 另池 | 分离 `token_to_kv_pool` |

- EAGLE3:aux hidden 层由 target capture(`spec_aux_hidden_state` / `configure_aux_hidden_state_capture`);常忽略 CLI token map
- Seed:`topk_p`/`topk_index`/`hidden_states`/`bonus_tokens` 均 **GPU、按 batch 行**
- Graph:draft decode(`EAGLEDraftCudaGraphRunner`)、draft-extend(`EAGLEDraftExtendCudaGraphRunner`)、target verify(decode verify graph)
- Draft-extend 按 `num_draft_tokens` **左 pad** 对齐 accept 块(`prepare_for_draft_extend`)
- 前缀命中后:target 只算 extend 段 HS;draft-extend 只为该段写 draft KV(前缀段不重 draft)

### 2. MTP / NEXTN(单层,走 EAGLE 路径)

| CLI/概念 | 解析 | Worker |
|----------|------|--------|
| `NEXTN` / 多数 MTP head | → `EAGLE` | `EAGLEWorkerV2`(topk=1 chain 常见) |
| `enable_multi_layer_eagle` | 仍是 EAGLE 枚举 | `MultiLayerEagleWorkerV2` |
| Gemma4 assistant | → `FROZEN_KV_MTP` | 见下节 |

单层 MTP 在实现上就是 **EAGLE 基础设施 + 1-layer draft head**,不是另一套 worker。

### 3. Frozen-KV MTP

| 项 | 内容 |
|----|------|
| 入口 | `frozen_kv_mtp_worker_v2.py::FrozenKVMTPWorkerV2`(fork `EAGLEWorkerV2` 但不走其 `__init__`) |
| Draft | `FrozenKVMTPDraftWorker` |
| I/O | `frozen_kv_mtp_info.py::{FrozenKVMTPDraftInput, FrozenKVMTPVerifyInput, FrozenKVMTPContext}` |

- Draft **无 KV 池**(dummy `max_total_num_tokens=64`,`out_cache_loc=None`);attn 绑 target KV 只读视图(`build_frozen_kv_mtp_context` / `bind_frozen_kv_context`)
- `_draft_extend_*` = **只选 seed**,不跑 draft-extend 前向;seed 前向嵌在下一步 `draft_forward` 开头
- Graph:`FrozenKVMTPCudaGraphRunner`;无 draft-extend graph
- 相对 EAGLE:省 draft KV / draft-extend 算力;adaptive 关闭

### 4. Chain MTP vs Non-chain(多层)

入口:`multi_layer_eagle_worker_v2.py::{MultiLayerEagleWorkerV2, MultiLayerEagleDraftWorker}`。

```python
# MultiLayerEagleDraftWorker.__init__
self.chain_mtp_hidden_states = draft_arch in ["Step3p5MTP"]
```

| 模式 | 层间 hidden 来源 | Prefill draft-extend capture |
|------|------------------|------------------------------|
| **Chain**(`Step3p5MTP`) | 每步用**本层上一步输出 HS** | `CaptureHiddenMode.FULL` |
| **Non-chain** | 继续用 **target HS** | `CaptureHiddenMode.LAST` |

- **每 step 一个 `ModelRunner`**:`draft_runner_list`(`is_multi_layer_eagle=True`),共享 embed/lm_head
- `draft()` 主要整理 draft-extend 已算好的 topk(无多步 draft decode graph)
- Draft-extend graph:`MultiLayerEagleMultiStepDraftExtendCudaGraphRunner` + `multi_layer_eagle_utils.py::rotate_input_ids`
- Verify:`run_eagle_verify(..., metadata_ready_pre_pad=True, finalize_tree_path=False)` — 不做 topk>1 的 tree-path 压缩
- Decode extend 后 `hidden_states` 可置 `None`(下轮靠 extend 时提案);`topk_*` 宽为 `topk * num_steps`

### 5. DFLASH

| 项 | 内容 |
|----|------|
| 入口 | `dflash_worker_v2.py::DFlashWorkerV2` |
| 状态 | `dflash_info_v2.py::DFlashDraftInputV2` |
| Verify I/O | `dflash_info.py::DFlashVerifyInput` |
| 工具 | `dflash_utils.py`(accept、mask、fused KV inject) |

**Prefill**:target FULL aux HS → **inject** 到 draft KV(`_append_target_hidden_to_draft_kv_by_loc`,radix 安全,赶在 scheduler 改 cache 前)→ 丢掉 HS,只留 `bonus` + `seq_lens`。  
**Decode**(无独立 draft_extend):mask 块 `[bonus \| MASK…]` → draft `TARGET_VERIFY` → target verify(线性因果)→ accept → inject 已提交 HS → 下一轮只带 bonus。

| 点 | 行为 |
|----|------|
| Draft KV | 独立池;可选 sliding window(`speculative_draft_window_size`);KV 由 **target HS 投影注入**,非自回归写 |
| Seed | `bonus_tokens` GPU;无 recurrent draft HS |
| Graph | draft block + target verify;无 draft-extend graph |
| Pad | 均匀 block;`prepare_for_decode` 可 over-alloc `committed + 2*block` |

### 6. DSPARK(**仅 SGLang**)

| 件 | 符号 |
|----|------|
| Worker | `dspark_components/dspark_worker_v2.py::DSparkWorkerV2` |
| Config | `dspark_config.py::resolve_runtime_config` |
| Planner | `dspark_planner.py::DSparkVerifyPlanner` |
| Draft | `dspark_draft.py::DraftBlockProposer` |
| Verify | `dspark_verify.py::{TargetVerifyExecutor, DsparkVerifyEpilogue}` |
| Inject | `dspark_kv_inject.py::TargetHiddenKvInjector` |
| Ragged | `ragged_verify.py` + `kernels/dspark_verify_window.py` / `dspark_accept.py` |
| 校准 | `dspark_sps.py` / `dspark_sts.py` / `dspark_block_accept_estimator.py` |

跨步状态复用 `DFlashDraftInputV2`。

**Decode(`_forward_decode`)**:alloc verify window → markov propose(`gamma`) → confidence → ragged layout → target verify → accept → inject HS。

| 名 | 含义 | 驻留 |
|----|------|------|
| `gamma` | draft 步数(`num_draft_tokens - 1`) | host int |
| verify window | `gamma + 1`(anchor + drafts) | host |
| `bonus_tokens` / `confidence` | 跨步 | GPU |
| `verify_lens` | 每请求 verify 宽 | GPU(+cpu 镜像) |

Ragged 模式(`SGLANG_RAGGED_VERIFY_MODE`):`static` / `cap-accept` / `compact`;graph 按 **token bucket** 键控。  
相对 DFLASH:markov head + 可选 confidence + ragged verify + epilogue 可 fold accept/commit。

#### Ragged verify：几何在 device，bucket 决策可在 host

固定宽度 verify 会把 batch 内每个请求都 pad 到同一 `gamma+1`。DSPARK 的 `RaggedVerifyLayout` 改为每请求一条 `verify_lens[i] >= 1`，以 `qo_indptr` 表示不等长 query 的前缀和，并派生 `extend_start_loc`；attention 因此能在一个 batch 内跑不同 verify 宽度，而不是为短请求计算无效 draft 位。

- `RaggedVerifyLayout.from_verify_lens_device` 以 device `verify_lens` 通过 `BuildQoIndptr` 生成 device `qo_indptr`，可避免为 layout 本身把长度同步回 host。
- `from_verify_lens` 的 host 路径保留 `verify_lens_cpu`、`total_verify_tokens`，用 `round_up_grid` 决定 CUDA graph 的 token bucket；这是**形状选择**需要的 host 信息，不是 attention 必然要求的 D2H。
- `static` 保固定窗口，`cap-accept` 限制有效接受宽度，`compact` 按有效 token 紧凑；三者在 padding、可 capture bucket 与调度复杂度之间取舍。

> **对 lake**：`InputBatch.query_start/query_end`（或等价的 device `qo_indptr`）应是 ragged query 几何的权威执行接口，而 Host `Req` 的长度只用于调度与结果提交。不要在纯 Torch GPU 路径对 query 长度逐项 `.item()`；若 graph bucket 必须由 host 选择，应把这次同步明确限制在 bucket 边界，并测量它是否落在 forward 阴影内。

---

## 方案对照表

| 维度 | EAGLE/EAGLE3 | 多层 MTP | Frozen-KV MTP | DFLASH | DSPARK |
|------|--------------|----------|---------------|--------|--------|
| Orchestrator | `EAGLEWorkerV2` | `MultiLayerEagleWorkerV2` | fork EAGLE | `DFlashWorkerV2` | `DSparkWorkerV2` |
| Draft 包装 | `EagleDraftWorker` | 每步一 runner | `FrozenKVMTPDraftWorker` | 普通 `TpModelWorker` | 普通 `TpModelWorker` |
| Pre-target | tree/chain draft | 整理提案 | seed+在 target KV 上 draft | mask 块 draft | markov 块 |
| Target verify | tree + retrieve | 同左,无 compact | 同 EAGLE | 线性块 | 线性 + ragged |
| Post-target | draft-extend 前向 | 多步 extend | 只选 seed | HS inject | HS inject |
| Draft KV | 自有,draft+extend 写 | 自有 | **无** | inject | inject |
| Recurrent HS | 有 | chain vs target | seed 有 | 无(仅 bonus) | 无(仅 bonus) |
| Ragged | 否 | 否 | 否 | 否 | **是** |
| lake 主攻 | ✓ | ✓ | 可选 | ✓ | ✓(唯一参考) |

与 lake `post_forward` / `pre_forward` 二阶段的大致对应:

| lake 阶段 | EAGLE 家族 | DFLASH 家族 |
|-----------|------------|-------------|
| `post_forward`(target 后) | draft-extend / seed 选择 | inject HS → draft KV |
| `pre_forward`(下轮 target 前) | `draft()` 自回归多 token | mask/markov 并行产块 |

---

## ModelRunner 职责(本项目视角)

`model_executor/model_runner.py::ModelRunner` 是 **GPU 执行内核**(类注释:`runs the forward passes of the models`)。Worker / SpecWorker 管编排;Runner 管:

| 职责 | 符号 |
|------|------|
| 持模型权重 | `load_model` → `self.model` |
| 持 KV / block 表 | `alloc_memory_pool` → `req_to_token_pool` + `token_to_kv_pool` |
| Attn / CUDA graph | `init_attention_backends` / `init_cuda_graphs` |
| 跑一步 | `forward` → `_forward_raw`(graph 或 eager) |
| 采样 | `sample` |

不管:请求队列、前缀命中、组 batch、投机 draft/verify 时序(在 Scheduler / SpecWorker / `TpModelWorker`)。

真正跑权重的路径一律是:

```
TpModelWorker → ModelRunner.forward → self.model
```

投机时多一层编排 Worker(`EAGLEWorkerV2` 等)罩在外面,内部仍经 draft/target 各自的 `TpModelWorker`→`ModelRunner`。NGRAM 无 draft runner;Frozen-KV MTP 的 draft runner 不写自有 KV,但仍经 `ModelRunner` 跑 draft head。

---

## CUDA graph：按捕获形状和 runner 选路，不只看 `is_extend`

SGLang 维护独立 `PrefillCudaGraphRunner` 与 `DecodeCudaGraphRunner`。前者以 `ForwardMode.EXTEND` capture、按 prefill token bucket 固定地址 buffer；后者以 decode batch/token bucket capture，并可适配 speculative 与 ragged verify 的 replay metadata。

| 运行几何 | 典型 runner | 关键行为 |
|----------|-------------|----------|
| 普通 prefill / 残差 prefill | prefill graph（可用时）或 eager | `PrefillCudaGraphRunner` capture mode=`EXTEND` |
| chunked prefill 的 `MIXED` batch | prefill graph（满足形状/后端时）或 eager | 为 replay 把混批几何规范化到 extend capture buffer；不表示 batch 失去 decode 部分 |
| 普通 `DECODE` | decode graph | 延迟路径，固定 decode buffers / batch bucket |
| `TARGET_VERIFY` | decode graph（可 capture 时）或 eager | `eagle_prepare_for_verify` 明确询问 `decode_cuda_graph_runner.can_run_graph` 并加载 batch；capture-time 的并行通信标签可因此与 eager verify 不同 |
| DSPARK ragged verify | decode graph 的 token bucket 或 eager | `RaggedVerifyLayout.graph_num_tokens` 决定 bucket；device layout 可无同步，host bucket 决策仍可能需要镜像 |

因此“extend 可能跑 prefill graph，也可能跑 decode graph”在语义上是对的：`TARGET_VERIFY` 被 `is_extend()` 归类，却使用 decode runner；`MIXED` 可以复用 EXTEND capture。graph runner 选择还受 backend、bucket、hidden capture、并行通信和静态 buffer 地址约束，不能仅由 `q_len` 或枚举名称推断。

> **对 lake**：统一的 `execute_model(SchedulerOutput)` 入口可接受任意 q_len，但 graph 选择应是显式的纯派生函数，如 `(query geometry, spec kind, attention backend, parallel mode, capture buckets) -> graph/eager plan`。这保留代码接口的统一性，又不会让 `DECODE` 名称的删除误删低延迟 graph、MoE 或 attention 的专用优化。

---

## Overlap schedule（异步调度 / 消 device 空泡）

> 默认开启（`--disable-overlap-schedule` 关掉）。目标：让 **CPU 收尾藏进 GPU forward 阴影**，并用 **FutureMap 在 device 侧接力 token**，避免「等上步 token 回 CPU 再开下步」的空泡。  
> Grammar 专项（何时仍被迫同步）见 [`../guided-decoding.md`](../guided-decoding.md)；vLLM 对等物是 async scheduling + `pending_structured_output_tokens`。

### 要消的空泡是什么

同步调度一步大致是：`forward → sync → token 回 CPU → 更新 Req/grammar → 组下批 → H2D → forward`。  
GPU 在 sync / CPU 工作段空转。Overlap 把这段拆开并行。

### 主循环：`event_loop_overlap`

锚点：`managers/scheduler.py::Scheduler.event_loop_overlap`。

```
while True:
  recv / get_next_batch
  if disable_overlap_for_batch:          # 被迫同步：先排空上批
      process_batch_result(result_queue.pop)
  if batch:
      batch_result = run_batch(batch)    # 启动 GPU forward（不立刻 process）
      result_queue.append((batch.copy(), batch_result))
  if last_batch and not disable_overlap:
      process_batch_result(last)         # CPU：copy_done、更新 Req、grammar accept…
  launch_batch_sample_if_needed(...)     # 常依赖上批 grammar 态后再 sample
  last_batch = batch
```

稳态 decode 时间线：

```
GPU:  … | forward(N)              | forward(N+1)             | …
CPU:  … | process(N-1) + sample(N)| process(N) + sample(N+1)| …
```

`run_batch` 只把工作丢上 GPU 并入队；**上批的 CPU 处理与本批 forward 重叠**。注释原文：*overlaps the CPU processing and GPU computation*。

### FutureMap：device 侧 token 接力

仅 CPU∥GPU 不够：下一步 decode 的 `input_ids` 是上步 sample 的 token。若每步都等 token 回 host 再 H2D，仍出泡。

| 机制 | 符号 | 作用 |
|------|------|------|
| 缓冲 | `FutureMap.output_tokens_buf` | 按 `req_pool_indices` 存上步 sample 的 token（GPU） |
| 写入 | `FutureMap.stash` / `publish` + `_relay_forward_payload` | sample 后把 bonus token（及 spec extras）写入 buf |
| 读出 | `overlap_utils.resolve_forward_inputs` | decode：`input_ids = output_tokens_buf[req_pool_indices]`（GPU gather）；prefill：pinned `prefill_input_ids_cpu` non_blocking H2D；混批：`mix_running_indices` 拼两路 |
| Spec extras | `RelayPayload`（`topk_*` / `hidden_states` / …） | overlap 路径经 FutureMap relay；非 overlap V2 可直接装 `next_draft_input` |
| 不中继 | 整份 `Req` | FutureMap **只**中继 token ids / 部分 spec extras |

`FutureMap` 由 `spec_algorithm.create_future_map` 在 Scheduler 初始化时创建（`scheduler.py`）。

### 延迟 sample：`launch_batch_sample_if_needed`

锚点：`scheduler.py::launch_batch_sample_if_needed`。

若 `GenerationBatchResult.delay_sample_func` 非空：在 `forward_stream` 上跑延迟 sample → `_relay_forward_payload` → `copy_to_cpu`。  
把 sample 从「forward 结束立刻做」挪到「上批 `process_batch_result` 之后」，便于与 grammar bitmask 重叠（mask 填得比 forward 短时≈零开销）。完成后清掉 closure，避免 structured output 路径 VRAM 泄漏。

### 何时关掉 overlap（被迫同步）

`is_disable_overlap_for_batch` → 先 `pop_and_process` 排空 `result_queue`，再开本批：

| 条件 | 原因 |
|------|------|
| **spec + grammar + decode** 且 queue 非空（`need_grammar_sync`） | 尚不支持 overlap×spec×grammar；TODO 在源码注释 |
| 连续两个 extend/prefill（`SGLANG_DISABLE_CONSECUTIVE_PREFILL_OVERLAP`） | 优先首包 TTFT，略损吞吐 |
| DP attention | 用全局 `is_extend_in_batch` 做关 overlap 决策，保证各 rank 一致，防死锁 |

### 与 vLLM async scheduling 对照

| | SGLang overlap | vLLM async |
|--|----------------|------------|
| 主循环 | `event_loop_overlap` + `result_queue` | `AsyncScheduler` + `step_with_batch_queue` / placeholder |
| Token 接力 | **FutureMap GPU buf** | 多依赖 runner 态 / placeholder；无同名 FutureMap |
| Structured | 非 spec 可藏进阴影；**spec+grammar 强制关 overlap** | `pending_structured_output_tokens` → defer sample |
| 默认 | overlap **开** | async 可配 |

细节与「能否绝对无空闲」结论见 [`../guided-decoding.md`](../guided-decoding.md)。

### 对 lake（已定）

见 [`../../architecture/compute-layer.md`](../../architecture/compute-layer.md) 决策 5–6：

- **Host `Req` 全在 `node_scheduler`**；引擎无长期 RequestState。
- **默认 overlap**；落点在 `node_scheduler`（与 DP sync 同层）；FutureMap 类 device 接力待 D10。
- 请求结束 → **`agent.on_request_finished`**（非引擎内 `cache_finished_req`）。
- 与 ready/done / 上批 finished∩本批 prepare 的槽位规则并入 D5；spec+grammar 等仍可作关 overlap 例外（同 guided-decoding）。

### 请求结束与资源释放（阶段）

请求结束判定 + KV 收尾在 **`process_batch_result`（Scheduler CPU）**，经 `release_kv_cache` → `cache_finished_req`；不在 ModelRunner.forward。  
完整层级（free vs 驱逐 vs L3）与 **vLLM `update_from_output` 双端分工**对照见 [block-lifecycle.md](block-lifecycle.md)「层级 1 — 请求结束」与 [../vllm/block-lifecycle.md](../vllm/block-lifecycle.md) 同节。lake 落点见上「对 lake」。

---

## 与 vLLM ModelRunner V2 对照

> Scheduler→Worker **字段全集**、差异表与架构根因见专文 [`../scheduler-worker-interface.md`](../scheduler-worker-interface.md)（定 lake D1 时优先读）。

vLLM 现有两套 runner,开关 `VLLM_USE_V2_MODEL_RUNNER`(`vllm_config.use_v2_model_runner`),在 `gpu_worker.py` 分支构造:

| | 路径 | 形态 |
|--|------|------|
| **V1** | `vllm/v1/worker/gpu_model_runner.py::GPUModelRunner` | 单体大文件 |
| **V2** | `vllm/v1/worker/gpu/model_runner.py::GPUModelRunner` | 拆到 `gpu/` 子包 |

下文对比 **V2** 与 SGLang `ModelRunner`。vLLM 侧专文见 [`../vllm/compute.md`](../vllm/compute.md)。

### 一句话

- **SGLang `ModelRunner`**:偏「执行内核」——加载、池、attn/graph、`forward`/`sample`。请求生命周期在 Scheduler。
- **vLLM V2 `GPUModelRunner`**:偏「GPU 侧状态机 + 执行」——吃 `SchedulerOutput`,自己维护 request state / block table / input buffers,投机挂在 runner 内。

### 职责边界

| 维度 | SGLang | vLLM V2 |
|------|--------|---------|
| 入口 | `TpModelWorker.forward_batch_generation` → `ModelRunner.forward` | `Worker.execute_model` → `GPUModelRunner.execute_model(SchedulerOutput)` |
| 请求状态 | host `Req`,Scheduler 管 | runner 内 `RequestState` + `add/update/free_requests` |
| Batch 组装 | `ScheduleBatch` → `ForwardBatch.init_new` | runner 内 `prepare_inputs` / `prepare_attn` / `InputBuffers` |
| Block table | `ReqToTokenPool`(runner 建,Scheduler 写) | `block_tables` 在 runner 内 `apply_staged_writes` |
| 投机 | **外层** SpecWorker 编排多个 `TpModelWorker`/`ModelRunner` | **内嵌** `self.speculator = init_speculator(...)` |
| KV 外置 | HiCache 在 mem_cache/controller | 一等 `KVConnector` 挂在 runner |
| 采样 | 常跟 forward 同路径 | `execute_model` 与 `sample_tokens` 可拆开(overlap) |

V2 `execute_model` 开头即更新请求状态(`finish/free/add/update_requests` + `block_tables.apply_staged_writes`);SGLang 等价逻辑在 `Scheduler` + `ScheduleBatch`,Runner 只吃已备好的 `ForwardBatch`。

### 结构形态

```
# vLLM V2
GPUModelRunner
  ├── RequestState / InputBuffers / block_table
  ├── ModelState(default / mamba_hybrid / encoder_decoder…)
  ├── spec_decode/*(eagle / mtp / dflash / dspark…)
  ├── sample/*、cudagraph、kv_connector、mm、pp…
  └── model

# SGLang
Scheduler → SpecWorker? → TpModelWorker → ModelRunner
                              │              ├── pools / attn / graph
                         (draft/verify)      └── model
```

### 投机放哪

| | SGLang | vLLM V2 |
|--|--------|---------|
| 编排层 | SpecWorker(与 Scheduler 对接) | Runner 内 Speculator |
| Draft 模型 | 常另建 draft `TpModelWorker` + `ModelRunner` | Speculator 挂 target runner,proposer 组件化 |
| DSPARK 等 | 成熟路径在 SpecWorker | V2 已有 `gpu/spec_decode/dspark/`(相对新) |

lake 取舍(见 compute-layer):执行编排仿 SGLang 共置串行;`proposer↔speculator` 划分可参考 vLLM;模块拆分 / connector 注入点偏 V2;两边都不要把 request state 或 KV 池权威塞进 runner。

### Dummy run / warmup

**vLLM V2:复用生产入口 + flag。** `_dummy_run` 造假 `SchedulerOutput`(`_dummy_req_*`),关 `KVConnector`,再调:

```text
execute_model(dummy_scheduler_output, dummy_run=True,
              skip_attn_for_dummy_run=…, is_profile=…)
```

`dummy_run=True` 时跳过 `add/update/free_requests`,改走 `InputBatch.make_dummy` / `prepare_dummy_attn`。可选再 `speculator.propose` 保 DP/EP 同步。用途:profile(`is_profile`)、CUDA graph capture、`Worker.execute_dummy_batch`(DP idle)、V2 `warmup_kernels(execute_model, sample_tokens)`。

**SGLang:不走 `forward_batch_generation(dummy_run=…)`**,专用路径造 dummy `ForwardBatch`:

| 场景 | 路径 |
|------|------|
| Kernel warmup / FlashInfer autotune | `runner/base_runner.py::BaseRunner._dummy_run`(切片静态 buffer → 拼 batch → `ModelRunner.forward`) |
| CUDA graph capture | `capture_prepare(size)` → `capture_one_shape`(warmup + capture);见 `decode_cuda_graph_runner` / `prefill_cuda_graph_runner` |
| 运行时 DP 无活请求 | `ForwardMode.IDLE`(真实调度空步,不是 capture dummy) |

投机 capture 常把 mode 设为 `TARGET_VERIFY` 等。可钩子:`ModelRunner.prepare_dummy_forward_batch`。

| | vLLM V2 | SGLang |
|--|---------|--------|
| 是否复用生产入口 | 是:`execute_model` + `dummy_run` | 否:专用 `_dummy_run` / `capture_prepare` |
| 假输入形态 | 假 `SchedulerOutput` | 假 `ForwardBatch` + 静态 buffer |
| 跳过真实请求状态 | `dummy_run` 分支 | 本来就不经 Scheduler/`Req` |
| DP idle | `_dummy_run` / `execute_dummy_batch` | `ForwardMode.IDLE` |

---

## Data Parallel:拓扑、选路与空闲同步

> 场景:32 卡一次启动、`dp=32`。两边都是 **每 DP rank 独立 Scheduler/EngineCore**,**不**共享一个全局调度器。共享的是前端入口与(可选)协调/路由进程。  
> vLLM 官方概述:`3rdparty/vllm/docs/serving/data_parallel_deployment.md`。

### 一句话对照

| | SGLang | vLLM |
|--|--------|------|
| 每 DP rank | 独立 `Scheduler` 进程(+ 本 rank 的 `ModelRunner`/KV) | 独立 `EngineCore` 进程(+ 内嵌 `Scheduler` + Worker/`GPUModelRunner`;TP>1 时再挂 TP workers) |
| 选路方 | `DataParallelController` | 内部 LB:`DPLBAsyncMPClient`(API server 侧);或外部 LB / hybrid |
| 协调空闲↔忙碌 | 每步 `all_gather`(`prepare_mlp_sync_batch`,dp-attn) | `DPCoordinator` + wave + `execute_dummy_batch` |
| 全局无请求 | `batch=None` → `on_idle`(可 `IdleSleeper`) | wave 结束 → `engines_running=False` pause |
| 仅 1 请求 | 进一个 rank;其它 rank `ForwardMode.IDLE` 陪跑(需 sync 时) | 进一个 EngineCore;其它 rank `execute_dummy_batch` 陪跑(MoE/需对齐时) |

### SGLang

#### 进程拓扑(`dp=32` 于 32 卡)

```
HTTP / TokenizerManager          ← 通常 1 份(node0)
        │
        ▼
DataParallelController           ← 1 份:只做 DP 选路,不跑模型
        │  ZMQ PUSH × 32
        ├──► Scheduler[dp0] + ModelRunner @ GPU0
        ├──► Scheduler[dp1] + ModelRunner @ GPU1
        │         …
        └──► Scheduler[dp31] + ModelRunner @ GPU31
        │
Detokenizer                      ← 通常 1 份
```

| 配置 | 启动 | 含义 |
|------|------|------|
| 普通 DP:`--dp 32 --tp 1` | `launch_dp_schedulers`:循环 32 次,每次一个 TP group | 32 个互不共享 KV 的副本 |
| DP Attention:`--tp 32 --dp 32 --enable-dp-attention` | `launch_dp_attention_schedulers`:一个大 TP world,仍 32 个 Scheduler | 权重按 TP 布局;attention 按 DP 分请求;MoE 等仍跨 rank sync(`tp_size % dp_size == 0`) |

调度状态**不共享**:每 Scheduler 自有 waiting/running、`Req`、`req_to_token`。

#### 请求如何判给哪个 rank

在 **DataParallelController**、进 Scheduler **之前**(`sock_send(self.workers[rank], req)`):

| 策略 | 行为 |
|------|------|
| `ROUND_ROBIN` | active workers 上轮转 |
| `TOTAL_REQUESTS` | 选 `running+waiting` 最少 |
| `TOTAL_TOKENS` | 选总 token 最少(请求数 tie-break) |
| `FOLLOW_BOOTSTRAP_ROOM` | PD:`bootstrap_room % dp_size` |
| 外部指定 | `routed_dp_rank` 直达 |

一条请求**只进一个** dp rank 的 Scheduler。

#### 全局无请求 / 仅一条请求

需 `require_mlp_sync`(`enable_dp_attention` 或 gathered buffer)时:

1. **全局 0 请求**:每步仍 `prepare_mlp_sync_batch` → `all_gather` 见 `max(global_num_tokens)==0` → **不造 IDLE** → `batch=None` → `on_idle()`(自检/指标/`IdleSleeper` poll)。无 model forward。服务进程仍活、可接新请求。
2. **仅 1 请求到 rank A**:Controller 派到 A → A 组真实 batch;`all_gather` 后 `need_idle_batch = max(global_num_tokens)>0` → 空闲 rank `get_idle_batch()` → `ForwardMode.IDLE` → **所有 rank 都 `run_batch`**,IDLE 陪跑 collective,避免 hang。

非 sync 的纯 DP 副本:其它 rank 完全不动。

锚点:`data_parallel_controller.py::{launch_dp_schedulers,launch_dp_attention_schedulers,round_robin_scheduler,maybe_external_dp_rank_routing}`、`dp_attn.py::prepare_mlp_sync_batch_raw`、`schedule_batch.py::prepare_for_idle`、`scheduler.py::on_idle`、`dp_attention.py::compute_dp_attention_world_info`。

### vLLM

#### 进程拓扑(`--data-parallel-size=32`)

官方:`docs/serving/data_parallel_deployment.md`。

```
API server(s)  ← 可 --api-server-count 横向扩展;内部 LB 时仍单 HTTP 入口
     │  选 EngineCore
     ▼
DPCoordinator  ← DP>1:收各 engine 队列统计、管 wave、广播 START_DP_WAVE
     │
     ├──► EngineCore[dp0] ── Scheduler + Executor → Worker(s) / GPUModelRunner
     ├──► EngineCore[dp1]
     │         …
     └──► EngineCore[dp31]
```

- **每 DP rank = 一个 EngineCore 进程**,内嵌**自己的** `Scheduler`(非共享)。
- TP>1 时:每个 EngineCore 再挂 `TP` 个 GPU worker(`DP×TP` 总 GPU 数)。
- MoE:expert 组默认大小 `DP×TP`;可 `--enable-expert-parallel`。此时各 DP rank **不完全独立**,有活请求时无活 rank 必须 dummy forward 对齐。

部署模式:

| 模式 | 含义 |
|------|------|
| Internal LB(默认自包含) | 单入口;`DPLBAsyncMPClient` 在 API server 内选 engine |
| Hybrid LB | `--data-parallel-hybrid-lb`:每节点 API 只喂本机 ranks,上游再拆流量 |
| External LB | 每 rank 独立 `vllm serve --data-parallel-rank i`;外部路由器分 HTTP |

#### 请求如何判给哪个 rank

**Internal LB**(`core_client.py::DPLBAsyncMPClient.get_core_engine_for_request`):

1. 若请求带 `data_parallel_rank` → 直达该 engine。
2. 否则按各 engine 的 `(waiting, running)` 打分:`score = waiting * 4 + running`,选最小;扫描起点按 client 旋转破平局。
3. 统计来自 `DPCoordinator` 周期发布(约 100ms);本地对 waiting 做投机 +1 减统计滞后。
4. 首请求且 engines paused 时,前端顺带通知 Coordinator(`FIRST_REQ`)→ 广播 `START_DP_WAVE` 拉起各 engine busy loop。

文档说明:当前内部 LB **尚未**做 KV-prefix 感知选路(留 TODO);大规模 DP 可加 `--api-server-count`。

**External LB**:HTTP 层外部决定打到哪个 rank 的 endpoint;Coordinator 仍可做 wave(尤其 MoE)。

#### 全局无请求 / 仅一条请求

`EngineCore.run_busy_loop` / `DPEngineCoreProc`(DP 变体):

1. **全局 idle**:`_has_global_unfinished_reqs` 经 DP 组 all-reduce(每 32 step 一次优化)见全闲 → `engines_running=False`,发 `wave_complete`,pause。API 仍在,engine 不空转 GPU。
2. **仅 1 请求到 rank A**:A 的 EngineCore 真正 `schedule`+`execute_model`;其它 rank 本地无 ready 请求时走 `execute_dummy_batch()` → `GPUModelRunner._dummy_run` → V2 即 `execute_model(..., dummy_run=True)`,保证 MoE/EP collective 不 hang。
3. Coordinator 在「有人收到新请求而全局 paused」时发 `START_DP_WAVE`,把其它 engine 从 pause 拉回 busy loop(以便它们能 dummy/跟上)。

锚点:`vllm/v1/engine/core.py::EngineCore` / `run_busy_loop` / `execute_dummy_batch`、`coordinator.py::DPCoordinator`、`core_client.py::DPLBAsyncMPClient.get_core_engine_for_request`、`gpu_worker.py::execute_dummy_batch`、`gpu/model_runner.py::_dummy_run`。

### 32 卡 dp32 场景小结

| 问题 | SGLang | vLLM |
|------|--------|------|
| 共享一个 Scheduler? | **否**,32 个独立 Scheduler | **否**,32 个独立 EngineCore 各带 Scheduler |
| 谁决定请求去哪? | 见下「双层管理模式」 | Internal:`DPLBAsyncMPClient`;或外部 LB |
| 全局空闲 GPU 在干什么? | event loop + 可选 sleep;无 forward | engine pause(wave 结束) |
| 1 请求如何带动其它 rank? | mlp sync `all_gather` → IDLE batch | Coordinator wave + 无活 rank `execute_dummy_batch` |
| KV | 每 rank 独立 | 每 EngineCore 独立;`max-num-seqs` **per rank** |

### SGLang 双层管理模式(引擎内 DP + 外部 Gateway)

SGLang 把「多副本选谁」拆成**可叠加的两层**,与 vLLM Internal/External 并不一一对应——生产上常是 **Gateway 选 worker 实例 + 实例内再 DP Controller 选 rank**。

```
Client
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ 层 B: sgl-model-gateway(Rust,原 sglang_router)          │
│   policy: cache_aware(默认) / round_robin / …           │
│   PD 时可分别选 prefill worker + decode worker            │
│   输出:某个 HTTP/gRPC worker URL(常是一整台 serve 入口) │
└─────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│ 层 A: 单次 `sglang serve` 进程树内                        │
│   DataParallelController(仅 node_rank==0 跑 event_loop) │
│   LoadBalanceMethod: ROUND_ROBIN / TOTAL_REQUESTS /     │
│     TOTAL_TOKENS / FOLLOW_BOOTSTRAP_ROOM                │
│   或请求带 routed_dp_rank → 直达                         │
│   输出:某个 dp_rank 的 Scheduler ZMQ                     │
└─────────────────────────────────────────────────────────┘
  │
  ▼
Scheduler[dp_i] → (TP/PP 广播) → ModelRunner…
```

#### 层 A — 引擎内 `DataParallelController`(偏 Internal)

| 项 | 行为 |
|----|------|
| 何时存在 | `dp_size > 1`(或 elastic EP)时拉起;多机时各 node 起本地 Scheduler,但 **routing event_loop 只在 node0** |
| 选路依据 | 队列/token 负载(`DPBudget` 吃各 rank shm 快照),或轮转;PD bootstrap room 哈希 |
| KV 感知? | **否**——不看 HiCache/radix 全局视图 |
| 覆盖点 | `maybe_external_dp_rank_routing`:上层若已指定 `routed_dp_rank`,本层不再打分 |
| 多机 | `launch_dp_schedulers` / `launch_dp_attention_schedulers` 按 `nnodes` 切 GPU;worker ZMQ 表在 node0 汇聚后广播端口 |

≈ vLLM **Internal LB**,但挂在 serve 进程树里,不是 API server 进程里的 `DPLBAsyncMPClient`。

#### 层 B — 外部 `sgl-model-gateway`(偏 External + 软 cache-aware)

| 项 | 行为 |
|----|------|
| 进程 | 独立 Rust 网关(`sgl-model-gateway`,`python -m sglang_router.launch_router`);与引擎解耦 |
| 默认策略 | **`cache_aware`**(`main.rs` CLI default) |
| 其它策略 | `random` / `round_robin` / `power_of_two` / `prefix_hash` / `manual`;PD 可对 prefill/decode 分设 |
| cache_aware 机制 | 每 worker 维护**近似 radix 树**(按请求文本历史插入,非实时查引擎 HiCache);负载均衡时走最短队列;平衡时走最长前缀匹配;`cache_threshold` 以下改选「树更小=缓存更空」的 worker;可 mesh 同步树操作 |
| 与真实 KV | **软共享**:靠路由历史猜前缀亲和,不是控制面强一致位置视图。补强见下「层 B 演进」 |
| PD | `pd_router` 分别 `pick` prefill/decode worker,仍吃同一套 policy |
| DP-aware | 网关可把选择落到具体 `routed_dp_rank`(透传层 A),见 `mini_lb.py` 等;默认仍常叠层 A |

≈ vLLM **External LB**(网关定 worker),但默认带 **近似 cache-aware**,比纯轮询强一档、比 lake「读存储池命中视图」弱一档。

#### 层 B 演进(与 lake 全局 Router 重叠的上游轴)

SGLang **战略上也在做「全局 Router 调度」**,问题域与 lake §1.1 高度重叠;差异在一致性与是否取消层 A。详见 [pain-points.md](pain-points.md) §1.2。

| 阶段 | 组件 | KV 信息从哪来 | 状态 |
|------|------|---------------|------|
| 生产现状 | `sgl-model-gateway` `cache_aware` | 请求文本历史 → 近似树 | **已上线**(默认) |
| 事件进 router | `experimental/sgl-router` `cache_aware_zmq` | 引擎 `ZmqEventPublisher` → `KvEventIndex` / `HashTree` | **树内实验代码**,非生产默认 |
| 独立目录 | [#31458](https://github.com/sgl-project/sglang/issues/31458) KV Indexer | 旁路 bridge → `hash→worker→tier` gRPC | **RFC**(2026-07),树外 Rust 服务 |
| SessionAware 产品化 | [#25760](https://github.com/sgl-project/sglang/issues/25760) | bucket + `sticky→cache_aware→load_based`;长期接 [#21846](https://github.com/sgl-project/sglang/issues/21846) agent hint | Step 0–1 勾完;真 KVEvent cache_aware / HA **未勾** |

演进轴:**近似历史 → KVEvent 进 router → 独立 Indexer**。全程旁路/最终一致、字节仍归 worker;默认双层权威(层 B+层 A)未被 roadmap 废除。

#### 两层如何叠(4 机 × 8 卡示例)

| 部署 | 层 B | 层 A | 谁最终定 rank |
|------|------|------|----------------|
| 单机 `dp=8`,无 gateway | 无 | Controller 在 8 rank 里选 | 层 A |
| 4 机各 `dp=8`,前置 gateway | gateway 在 4 个 worker URL 里 cache_aware/轮询 | 每机 Controller 再在本机 8 rank 里选 | **两层都决策**(二次分发) |
| gateway + 每机 `dp=1` | gateway 直接选到卡/实例 | 无 Controller | 仅层 B |
| 上层指定 `routed_dp_rank` | 可先定到某 serve | Controller 直达该 rank | 上层(+层 A 透传) |

痛点与 lake 对照:[pain-points.md](pain-points.md) §1.2——上游承认智能应在 router(#25760 / #27574),并已有 `cache_aware_zmq` 原型;但层 A 与近似/旁路目录并存,尚未收束为单一选路权威。

#### 与 lake §1.1 倾向的映射

| SGLang | lake 取舍 |
|--------|-----------|
| 层 B gateway + cache_aware 近似树(+ 演进中的 `cache_aware_zmq` / Indexer) | **强化并收束为唯一权威**:Go Router + **存储池命中视图镜像**(真本地命中/D-direct);可借鉴事件→树的形态,不照搬最终一致旁路 |
| 层 A DataParallelController | **默认取消权威二次分发**(External 式);负载信号仍可上报给 Router |
| `routed_dp_rank` 透传 | Router 输出即终点;调试可保留显式 rank 覆盖 |
| kv_events / Indexer 旁路 | 控制面 etcd **强一致**位置视图,非最终一致旁路 |
| SessionAware bucket + sticky/load 链(#25760) | 负载/亲和作 Router 加权输入;不设 mode-to-mode 降级链(失败走 F4) |

锚点:层 A — `data_parallel_controller.py::{DataParallelController,LoadBalanceMethod,maybe_external_dp_rank_routing,event_loop}`;层 B 生产 — `sgl-model-gateway/src/policies/cache_aware.rs::CacheAwarePolicy`,`routers/http/pd_router.rs`,`main.rs`(`--policy` default `cache_aware`);层 B 实验 — `experimental/sgl-router/src/policies/kv_events/{mod.rs,index.rs,tree.rs}`、`config/types.rs::PolicyKind::CacheAwareZmq`。

### 对 lake

- **选路形态倾向 External 式**(已写入 [`../../architecture/scheduling.md`](../../architecture/scheduling.md) §1.1):对外逻辑上只有一层 **KV 感知 Router** 做 `f → (模式, 节点/rank)`,计算端点不再内建 Internal/Hybrid 式二次 DP LB(不照搬 head 上 `DPLBAsyncMPClient` / SGLang **层 A** `DataParallelController` 权威分发)。
- 对标 SGLang 时:吸取**层 B 上移智能**的方向(#25760 / `cache_aware_zmq` / #31458);用真命中视图升级其 cache_aware;拒绝「Gateway 选机 + 机内 Controller 再选 rank」的默认双层权威。
- Router 可多实例水平扩展(无状态 + 命中视图镜像),但扩展的是同质选路面,不是「每机再嵌 DPLB」。计算侧独立副本 vs TP/PP 联合由部署决定;Router 只选定逻辑执行单元。
- 引擎侧 waiting/running 等只作**上报信号**供 Router 加权,不作第二层权威分发——否则看不见存储池本地命中,损害 D-direct。
- 执行层若保留跨 rank MoE/DP collective,空闲 rank 的「陪跑」语义必须有(IDLE 或 dummy),且宜由**池/控制面可见的同步原语**驱动,而非引擎私有调度器隐式 all_gather。
- 持续跟踪上游:#25760 Step 3(真 KVEvent cache_aware)、#31458 一致性语义、实验 `sgl-router` 是否并入 `sgl-model-gateway`。
- 本项参考实现见上锚点。

---

## Tensor / Pipeline Parallel:控制面

> 焦点:**谁驱动一步、工作如何扇出到各 GPU**,不是切分数学。与上节 DP 对照——DP 是多副本独立调度;TP/PP 是**同一副本内**多卡锁步。

### 一句话对照

| | SGLang | vLLM |
|--|--------|------|
| `tp=N, pp=M, dp=1` 进程 | **N×M 个 Scheduler**(每 GPU 一个完整 Scheduler 事件循环) | **1 个 EngineCore**(1 个 Scheduler)+ **N×M 个 Worker** |
| 谁 schedule | **每个** TP/PP rank 各自 `get_next_batch_to_run`(从同一请求流重建 batch) | **仅** EngineCore 内一个 Scheduler |
| 工作如何到非 0 卡 | Leader ZMQ 收请求 → gloo `broadcast_pyobj` → 各 rank 自跑 forward | Executor `collective_rpc` 把同一份 `SchedulerOutput` 扇出(mp MessageQueue / Ray DAG) |
| TP 数据面 | NCCL `tp_group` all_reduce / logits gather | 同:Worker 内 NCCL TP collectives |
| PP 激活 | `PPProxyTensors` + `pp_group.send/recv_tensor_dict`;阶段间还可 P2P 传 req 列表 | Worker `irecv/isend` intermediates;V2 `PPHandler` 回传 sampled tokens;EngineCore `batch_queue` 深度≈`pp_size` |
| 与 DP 关系 | 普通 DP:再 ×D 套独立 TP 组;dp-attn:一个 TP world 内拆 attn_dp | DP=多 EngineCore;每个 core 内再挂 TP×PP workers |

### SGLang

#### 拓扑

```
Tokenizer ──ZMQ──► Scheduler[pp0,tp0] only   ← 仅 attn_tp_rank==0 && pp_rank==0 绑 PULL
                      │  broadcast_pyobj (gloo cpu_group / attn_tp_cpu_group)
                      ▼
            Scheduler[pp*,tp*] 全部:process_input_requests
                      │  各自 get_next_batch_to_run(同请求流 → 同构 batch)
                      ▼
            各自 run_batch → TpModelWorker → ModelRunner.forward
                      │
            TP: NCCL all_reduce / gather(层内)
            PP: 阶段间 proxy / output tensor_dict + 可选 req P2P
```

- World:`rank = tp_size * pp_rank + tp_rank`,`world_size = tp * pp`(`distributed/bootstrap.py::_init_parallel_groups`)。
- **不是**「一个 Scheduler + N 个无调度 worker」——每个 GPU 进程都跑完整 Scheduler;靠广播请求流对齐。
- 拉起:`entrypoints/engine.py::_launch_scheduler_processes` 嵌套 `pp_rank`×`tp_rank`;有普通 DP 时再经 `DataParallelController.launch_dp_schedulers` 乘副本数。

#### TP 控制

| 步骤 | 机制 | 符号 |
|------|------|------|
| 收请求 | 仅 leader 绑 ZMQ | `SchedulerIpcChannels.create(is_rank_zero=...)` |
| 扇出请求 | gloo `broadcast_pyobj` | `request_receiver.py::_broadcast_reqs_across_ranks` |
| 组 batch | 各 rank 本地重建,**不**广播 `ScheduleBatch` | `Scheduler.get_next_batch_to_run` |
| 前向 | 各 rank 都调用 | `run_batch` → `TpModelWorker.forward_batch_generation` |
| 层内同步 | NCCL | `RowParallelLinear` → `tensor_model_parallel_all_reduce`;`LogitsProcessor` gather |

#### PP 控制

循环:`scheduler_pp_mixin.py::SchedulerPPMixin.event_loop_pp`(由 `dispatch_event_loop` 选中)。微批深度≈`pp_size + pp_async_batch_depth`。

每微批(简化):

1. stage0:ZMQ+TP 广播请求;stage>0:P2P 收 req 列表再 TP 广播  
2. 非末段:P2P 把 req 转给下一 stage  
3. 非首段:`_pp_recv_proxy_tensors` 收上一阶段激活(`PPProxyTensors`)  
4. `_pp_launch_batch` → `run_batch(..., pp_proxy_tensors)`  
5. 非末段:`pp_group.send_tensor_dict` 发 proxy;末段采样后沿环回传 `output`(token ids 等)  
6. 前段收 `output` → `process_batch_result`

Worker 分工:末段 PP rank 在 `forward_batch_generation` 里 sample;非末段返回 `pp_hidden_states_proxy_tensors`。

#### 与 dp-attention

`compute_dp_attention_world_info`:`attn_tp_size = tp/(dp·cp)`,`attn_dp_rank` 由 `tp_rank` 推出。`--tp 32 --dp 32 --enable-dp-attention` → 32 个 Scheduler、一个 NCCL world、`attn_tp=1`;请求按 attn_dp 路由,MoE 路径仍要对齐(IDLE)。广播范围缩到 `attn_tp_group`。

### vLLM

#### 拓扑

```
API / EngineCoreClient
        │
        ▼
EngineCore  × DP          ← 每 DP rank 一个;内嵌唯一 Scheduler
        │
        ▼
Executor (mp | ray | uni)
        │  collective_rpc:同一份 SchedulerOutput 扇出
        ▼
Worker × (TP×PP)          ← 每 GPU 一个;无独立 Scheduler
        │
   TP: NCCL 层内 collective
   PP: irecv/isend IntermediateTensors + PPHandler 回传 sampled tokens
```

例:`tp=4,pp=2,dp=1` → 1 EngineCore + 8 Workers;`tp=2,pp=1,dp=4` → 4 EngineCore × 各 2 Workers。

官方:`docs/serving/parallelism_scaling.md`、`docs/serving/data_parallel_deployment.md`。

#### TP 控制

| 步骤 | 机制 | 符号 |
|------|------|------|
| 调度 | 仅 EngineCore | `EngineCore.step` → `scheduler.schedule()` |
| 扇出 | Executor RPC,**不是**「driver worker 经 TP 组广播 SchedulerOutput」 | `Executor.collective_rpc` / `MultiprocExecutor`(SHM `MessageQueue`) / Ray DAG |
| 执行 | 每 Worker 收同一 `SchedulerOutput` | `Worker.execute_model` → `GPUModelRunner.execute_model` |
| 层内 | NCCL TP group | `get_tp_group()` + 模型算子 |

`is_driver_worker`(每 PP stage 的 TP0)是 worker 侧标志,不是 schedule 扇出主路径。

#### PP 控制

| 层面 | 机制 |
|------|------|
| 流水填充 | `EngineCore.batch_queue` + `step_with_batch_queue`;并发 batch 数≈`pp_size`(见 `VllmConfig.max_concurrent_batches`) |
| 同一步各 stage | 仍靠 `collective_rpc` 收到**同一** `SchedulerOutput` |
| 激活传递 | 非首段 `get_pp_group().irecv_tensor_dict`;非末段 `isend`(`gpu_worker.Worker.execute_model`) |
| 采样结果回传 | V2:`PPHandler`(`worker/gpu/pp_utils.py`)侧流 NCCL broadcast,延迟≈`pp_size` 步,供前段更新状态 |

「微批」在此主要指 EngineCore 批队列深度,不是另套独立 PP microbatch 调度器。

### DP vs TP vs PP(两边共识)

| | DP | TP | PP |
|--|----|----|-----|
| 副本数 / 调度器 | 多副本、多 Scheduler(或 EngineCore) | 单副本内切宽度 | 单副本内切深度 |
| 请求归属 | 一条请求进一个副本 | 一条请求所有 TP rank 同算 | 一条请求流水过各 stage |
| 无本地活时 | 可真正 idle(除非 MoE/dp-attn 要陪跑) | 必须锁步 forward | 必须锁步(可队列填满流水线) |
| 控制扇出 | LB / Controller | 广播请求(SGLang)或广播 SchedulerOutput(vLLM) | 同上 + 阶段间激活/token |

### 对 lake

- **TP**:宜「单节点控制面/agent 一次决策 → 多卡执行」,更接近 vLLM「一份 schedule + Executor 扇出」,避免 SGLang「每卡一个完整 Scheduler」的状态复制成本——尤其存算分离后队列/KV 本就不该 per-rank 权威。
- **PP**:激活 P2P + 采样回传仍属计算面;ready/done fence 与池放置勿与 PP stage 握手缠死。
- **与 DP 叠加**:Router 选副本(DP);副本内 TP/PP 是部署细节。集体通信陪跑规则见上节 DP。

锚点(SGLang):`engine.py::_launch_scheduler_processes`、`request_receiver.py::_broadcast_reqs_across_ranks`、`scheduler_pp_mixin.py::event_loop_pp`、`forward_batch_info.py::PPProxyTensors`、`bootstrap.py::_init_parallel_groups`。  
锚点(vLLM):`executor/abstract.py::collective_rpc`、`executor/multiproc_executor.py::MultiprocExecutor`、`engine/core.py::step_with_batch_queue`、`worker/gpu_worker.py::execute_model`、`worker/gpu/pp_utils.py::PPHandler`。

---

## 与本系统的关键差异

| 维度 | SGLang | lake |
|------|--------|------|
| Worker 状态 | 进程持有权重 + L1/L2 KV | worker 无状态;权重/KV 归存储池 |
| Draft KV | 实例私有 draft pool(+ HiCache `PoolName.DRAFT` 可落 L3) | drafter KV 与 target KV 同归存储池权威、跨请求复用/随迁 |
| Seed hidden | 请求内 GPU `spec_info`,命中后 draft-extend **重算** | 同先按重算式;是否跨请求缓存待定 |
| Block table | Scheduler/引擎写 `req_to_token` | 本地池 agent 组装;引擎只 replay |
| 编排 | SpecWorker 内嵌 Scheduler 的 `model_worker` | 仿共置串行;`Drafter.post/pre_forward` 统一自回归与 diffusion |
| DSPARK | SpecWorker 成熟路径;vLLM V2 亦有较新实现 | 以 SGLang `dspark_components/` 为主参考 |
| Runner 胖瘦 | 薄执行内核 | 宜保持薄(仿 SGLang);模块拆分/connector 可借 V2 |
| Overlap / 异步调度 | `event_loop_overlap` + FutureMap(见上专节) | **已定默认开**;落 `node_scheduler`;结束打 agent;`Req` 全在 scheduler(见 compute-layer 决策 5–6) |
| Dummy / graph warmup | 专用 `_dummy_run` / `capture_prepare` | 待定;若 runner 极简,更贴近 SGLang「另造 batch」而非塞进生产 `execute` flag |
| DP 拓扑 | 每 rank 独立 Scheduler;Controller 选路;dp-attn 用 IDLE 陪跑(vLLM:每 rank EngineCore + Coordinator wave + dummy) | 选路归控制面;陪跑若需要则显式,勿把隐式 all_gather 嵌进无状态 worker |
| TP/PP 控制 | 每 GPU 一 Scheduler;ZMQ leader + gloo 广播请求;PP 用 proxy tensor_dict(vLLM:一 Scheduler + Executor 扇出 SchedulerOutput;PP 用 irecv/isend + PPHandler) | 副本内宜「一份调度决策 + 多卡执行」(偏 vLLM);勿每卡复制权威队列 |

详见 [`../../architecture/compute-layer.md`](../../architecture/compute-layer.md);DP 专节见上「Data Parallel」。

---

## 代码索引

> 符号名稳定;行号会漂移。找不到时 `grep -n "符号名" 3rdparty/sglang/python/sglang/srt/<文件>`。

### 热路径

| 概念 | 文件:符号 |
|------|-----------|
| Tokenizer 入口 | `managers/tokenizer_manager.py::TokenizerManager.generate_request` |
| 调度一步 | `managers/scheduler.py::Scheduler.run_batch` |
| 非投机 forward | `managers/tp_worker.py::TpModelWorker.forward_batch_generation` |
| 请求 / batch | `managers/schedule_batch.py::Req` / `ScheduleBatch` |
| ForwardBatch / Mode | `model_executor/forward_batch_info.py::ForwardBatch` / `ForwardMode` / `CaptureHiddenMode` |
| ModelRunner | `model_executor/model_runner.py::ModelRunner`(`load_model` / `alloc_memory_pool` / `forward` / `sample`) |
| Dummy forward 钩子 | `model_runner.py::ModelRunner.prepare_dummy_forward_batch` |
| Warmup / dummy run | `model_executor/runner/base_runner.py::BaseRunner._dummy_run` / `warmup` |
| Graph capture 造 batch | `runner/base_cuda_graph_runner.py::capture_prepare` / `capture_one_shape`;实现见 `decode_cuda_graph_runner.py` / `prefill_cuda_graph_runner.py` |
| Req→token 表 | `mem_cache/memory_pool.py::ReqToTokenPool` |
| 分配写表 | `mem_cache/allocation.py::alloc_for_extend` / `alloc_for_decode` |
| H2D / overlap 解析 | `managers/overlap_utils.py::resolve_forward_inputs` |
| Overlap 主循环 | `managers/scheduler.py::Scheduler.event_loop_overlap` |
| 关 overlap 条件 | `scheduler.py::is_disable_overlap_for_batch`（`need_grammar_sync`） |
| 延迟 sample | `scheduler.py::launch_batch_sample_if_needed` / `_relay_forward_payload` |
| FutureMap | `managers/overlap_utils.py::FutureMap`（`stash` / `publish` / `output_tokens_buf`）+ `RelayPayload` |
| 结果信封 | `managers/utils.py::GenerationBatchResult`（含 `delay_sample_func`） |

### Data Parallel(SGLang)

| 概念 | 文件:符号 |
|------|-----------|
| DP 控制器 / 选路 | `managers/data_parallel_controller.py::DataParallelController` |
| 普通 DP 拉起 | `launch_dp_schedulers` |
| DP-attn 拉起 | `launch_dp_attention_schedulers` |
| 轮转 / 负载 / 直达 | `round_robin_scheduler` / `total_requests_scheduler` / `maybe_external_dp_rank_routing` |
| LB 枚举 | `LoadBalanceMethod`(ROUND_ROBIN / TOTAL_REQUESTS / TOTAL_TOKENS / FOLLOW_BOOTSTRAP_ROOM) |
| mlp sync / IDLE | `managers/scheduler_components/dp_attn.py::prepare_mlp_sync_batch_raw` / `get_idle_batch` |
| IDLE batch | `managers/schedule_batch.py::ScheduleBatch.prepare_for_idle` |
| 全局空闲 | `managers/scheduler.py::Scheduler.on_idle` |
| attn DP 布局 | `layers/dp_attention.py::compute_dp_attention_world_info` |
| 外部网关 / 策略 | `sgl-model-gateway/`:`policies/cache_aware.rs::CacheAwarePolicy`、`policies/mod.rs::LoadBalancingPolicy`、`routers/http/pd_router.rs` |
| 网关默认 policy | `sgl-model-gateway/src/main.rs`(`--policy` default `cache_aware`) |
| 实验 KVEvent 路由 | `experimental/sgl-router/src/policies/kv_events/mod.rs`(`KvEventIndex`) · `tree.rs::HashTree` · `config/types.rs::PolicyKind::CacheAwareZmq` |
| DP rank 透传 | `managers/data_parallel_controller.py::maybe_external_dp_rank_routing`;gateway `mini_lb.py` 写 `routed_dp_rank` |

### Tensor / Pipeline Parallel(SGLang)

| 概念 | 文件:符号 |
|------|-----------|
| 拉起 TP×PP 进程 | `entrypoints/engine.py::_launch_scheduler_processes` |
| 并行组初始化 | `distributed/bootstrap.py::_init_parallel_groups` → `parallel_state.py::initialize_model_parallel` |
| ZMQ 仅 leader | `managers/scheduler_components/ipc_channels.py::SchedulerIpcChannels.create` |
| 请求广播 | `managers/scheduler_components/request_receiver.py::_broadcast_reqs_across_ranks` |
| 事件循环分发 | `managers/scheduler.py::dispatch_event_loop` |
| PP 循环 | `managers/scheduler_pp_mixin.py::SchedulerPPMixin.event_loop_pp` |
| PP 激活 / 回传 | `_pp_launch_batch` / `_pp_send_dict_to_next_stage` / `_pp_recv_proxy_tensors` |
| Proxy 类型 | `model_executor/forward_batch_info.py::PPProxyTensors` |
| TP forward | `managers/tp_worker.py::TpModelWorker.forward_batch_generation` |

### 对照 vLLM(路径在 `3rdparty/vllm/`)

| 概念 | 文件:符号 |
|------|-----------|
| V2 开关 | `vllm/config/vllm.py::VllmConfig.use_v2_model_runner`(`VLLM_USE_V2_MODEL_RUNNER`) |
| Worker 选 V1/V2 | `vllm/v1/worker/gpu_worker.py`(据 `use_v2_model_runner` 构造) |
| V2 runner | `vllm/v1/worker/gpu/model_runner.py::GPUModelRunner` |
| V2 生产前向 | `GPUModelRunner.execute_model` / `sample_tokens` |
| V2 dummy | `GPUModelRunner._dummy_run` → `execute_model(..., dummy_run=True)` |
| DP idle dummy | `gpu_worker.py::Worker.execute_dummy_batch`;`engine/core.py::execute_dummy_batch` |
| DP busy loop / wave | `vllm/v1/engine/core.py::run_busy_loop` / `_has_global_unfinished_reqs` |
| DP 协调 | `vllm/v1/engine/coordinator.py::DPCoordinator` |
| 内部 LB 选 engine | `vllm/v1/engine/core_client.py::DPLBAsyncMPClient.get_core_engine_for_request` |
| 每 rank EngineCore | `vllm/v1/engine/core.py::EngineCore`(内嵌 `Scheduler`) |
| Executor 扇出 | `vllm/v1/executor/abstract.py::collective_rpc`;`multiproc_executor.py::MultiprocExecutor` |
| PP 批队列 | `vllm/v1/engine/core.py::step_with_batch_queue` |
| PP 激活 / token 回传 | `worker/gpu_worker.py::execute_model`(irecv/isend);`worker/gpu/pp_utils.py::PPHandler` |
| V2 warmup | `vllm/v1/worker/gpu/warmup.py::warmup_kernels` |
| V1 单体 | `vllm/v1/worker/gpu_model_runner.py::GPUModelRunner` |
| 官方 DP / 并行说明 | `docs/serving/data_parallel_deployment.md` / `docs/serving/parallelism_scaling.md` |

### 投机

| 概念 | 文件:符号 |
|------|-----------|
| 算法枚举 / 工厂 | `speculative/spec_info.py::SpeculativeAlgorithm.create_worker` |
| 插件注册 | `speculative/spec_registry.py::register_algorithm` |
| NEXTN 别名 | `arg_groups/speculative_hook.py::_resolve_speculative_algorithm_alias` |
| 基类契约 | `speculative/base_spec_worker.py::BaseSpecWorker` / `EagleDraftWorkerBase` |
| EAGLE 一步 | `speculative/eagle_worker_v2.py::EAGLEWorkerV2.forward_batch_generation` |
| EAGLE verify | `speculative/eagle_worker_common.py::run_eagle_verify` |
| EAGLE seed I/O | `speculative/eagle_info.py::EagleDraftInput` |
| 多层 + chain | `speculative/multi_layer_eagle_worker_v2.py::MultiLayerEagleDraftWorker.chain_mtp_hidden_states` |
| Frozen MTP | `speculative/frozen_kv_mtp_worker_v2.py::FrozenKVMTPWorkerV2` |
| DFLASH 一步 | `speculative/dflash_worker_v2.py::DFlashWorkerV2.forward_batch_generation` |
| DSPARK decode | `speculative/dspark_components/dspark_worker_v2.py::DSparkWorkerV2._forward_decode` |
| Ragged layout | `speculative/ragged_verify.py::RaggedVerifyLayout` |
| 拒绝采样 | `speculative/reject_sampling.py::chain_speculative_sampling_triton` |
| Draft 池名 | `mem_cache/hicache_storage.py::PoolName.DRAFT` |
| Draft 公共工厂 | `speculative/draft_worker_common.py::build_draft_tp_worker` |
| 批执行模式谓词 | `model_executor/forward_batch_info.py::ForwardMode` |
| verify 选 decode graph | `speculative/eagle_utils.py::eagle_prepare_for_verify` |
| prefill graph capture/replay | `model_executor/runner/prefill_cuda_graph_runner.py::PrefillCudaGraphRunner` |
| decode graph + ragged replay | `model_executor/runner/decode_cuda_graph_runner.py::DecodeCudaGraphRunner` |

### 相关文档

| 文档 | 内容 |
|------|------|
| [overview.md](overview.md) / [hicache.md](hicache.md) | L1/L2/L3 分层与 HiRadix |
| [block-lifecycle.md](block-lifecycle.md) | block 释放/降层 |
| [moe-communication.md](moe-communication.md) | MoE routing、DeepEP `auto` 与 graph 例外 |
| [loading-and-integrations.md](loading-and-integrations.md) | 权重多线程/远端加载、外部依赖集成边界 |
| [pain-points.md](pain-points.md) | HiCache×EAGLE/MTP 组合 hang 等 |
| [`../vllm/compute.md`](../vllm/compute.md) | vLLM V1/V2 runner、dummy、DP/TP/PP 控制面 |
| [`../../architecture/compute-layer.md`](../../architecture/compute-layer.md) | lake 计算层与投机落点 |
