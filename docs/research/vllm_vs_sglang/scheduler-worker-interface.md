# Scheduler → Worker 接口对照（vLLM × SGLang）

> 源码锚点（行号会漂移，以符号为准）:  
> - vLLM:`vllm/v1/core/sched/output.py::{SchedulerOutput,NewRequestData,CachedRequestData,GrammarOutput}`；消费方 `vllm/v1/worker/gpu/model_runner.py::GPUModelRunner.execute_model`  
> - SGLang:`managers/schedule_batch.py::ScheduleBatch` → `model_executor/forward_batch_info.py::ForwardBatch`；消费方 `managers/tp_worker.py::TpModelWorker.forward_batch_generation`  
> 相关:[`sglang/model-runner.md`](../sglang/model-runner.md)、[`vllm/compute.md`](../vllm/compute.md)、lake [`../architecture/compute-layer.md`](../../architecture/compute-layer.md)「计算引擎结构」/ D1、[`../architecture/scheduling.md`](../../architecture/scheduling.md) §3.1。

本文梳理**调度侧提供给 worker / model runner 的全部字段**，对比差异，并解释背后的架构分叉——供 lake 定 `SchedulerOutput` / `NodeScheduleOutput` 时对照（D1）。

---

## 1. 交付物形态（先看清比较对象）

两边**不是同构消息**：

| | vLLM | SGLang |
|--|------|--------|
| **主交付物** | `SchedulerOutput`（+ 可选后续 `GrammarOutput`） | `ScheduleBatch` →（同进程）`ForwardBatch` |
| **边界** | Scheduler 进程 ↔ Worker 进程（Executor `collective_rpc`） | Scheduler 与 TpModelWorker / ModelRunner **同进程**（或同 TP 组内广播请求后本地组 batch） |
| **风格** | **增量 RPC**：new / cached / finished；worker 自持 `RequestState` | **整批状态**：持 `List[Req]` + 已分配的 GPU tensor；runner 薄 |
| **worker 入口** | `GPUModelRunner.execute_model(scheduler_output)` | `TpModelWorker.forward_batch_generation(batch \| forward_batch)` |
| **旧路径** | — | `ModelWorkerBatch` / `get_model_worker_batch` **已删除** |

因此「字段对照」只能按**语义桶**对齐，不能指望一一同名。

```
# vLLM
EngineCore.Scheduler.schedule()
  → SchedulerOutput
  → Executor.collective_rpc("execute_model", SchedulerOutput)
       → GPUModelRunner: add/update/free_requests → 组 InputBatch → forward

# SGLang
Scheduler 组 ScheduleBatch（含 alloc、DP sync）
  → ForwardBatch.init_new(ScheduleBatch)   # 多数 GPU 字段直接借用
  → TpModelWorker → ModelRunner.forward/sample
```

---

## 2. vLLM：提供给 Worker 的字段全集

### 2.1 `SchedulerOutput`（主信封）

源：`vllm/v1/core/sched/output.py::SchedulerOutput`。

| 字段 | 类型（语义） | Worker 用途 |
|------|--------------|-------------|
| `scheduled_new_reqs` | `list[NewRequestData]` | 首次进 batch：缓存请求态、采样/MM/LoRA、初始 block 表 |
| `scheduled_cached_reqs` | `CachedRequestData` | 续跑：增量 block / token / computed 长度 |
| `num_scheduled_tokens` | `dict[req_id, int]` | 本步每请求要算的 token 数 |
| `total_num_scheduled_tokens` | `int` | 批总 token；0 则可能走 `kv_connector.no_forward` |
| `scheduled_spec_decode_tokens` | `dict[req_id, list[int]]` | 本步要验证的 draft token ids |
| `scheduled_encoder_inputs` | `dict[req_id, list[int]]` | 本步要跑的 encoder 输入下标（多模态） |
| `num_common_prefix_blocks` | `list[int]` | 各 KV group 公共前缀 block 数（cascade attn） |
| `finished_req_ids` | `set[str]` | 通知 worker 释放缓存态 |
| `free_encoder_mm_hashes` | `list[str]` | 释放 encoder cache 条目 |
| `scheduled_encoder_input_stats` | `ScheduledEncoderInputStats?` | encoder 调度统计 |
| `preempted_req_ids` | `set[str]?` | V2 runner：本步被抢占的请求 |
| `has_structured_output_requests` | `bool` | async：本批是否含 structured output |
| `pending_structured_output_tokens` | `bool` | async：grammar bitmask 是否尚缺 token（可 defer sample） |
| `num_invalid_spec_tokens` | `dict[str,int]?` | 接受率统计校正 |
| `kv_connector_metadata` | `KVConnectorMetadata?` | 外部 KV connector（load/save）元数据 |
| `ec_connector_metadata` | `ECConnectorMetadata?` | EC connector |
| `new_block_ids_to_zero` | `list[int]?` | 新分配 block 上 GPU 清零（防脏数据） |
| `kv_cache_block_copies` | `list[KVCacheBlockCopy]?` | CoW：forward 前拷块 |
| `num_spec_tokens_to_schedule` | `int` | 动态投机：下一步 K |

### 2.2 `NewRequestData`（嵌在 `scheduled_new_reqs`）

| 字段 | 语义 |
|------|------|
| `req_id` | 请求 id |
| `prompt_token_ids` | prompt tokens（可 None） |
| `prefill_token_ids` | V2 runner 用 prefill 段 |
| `mm_features` | 多模态特征规格 |
| `sampling_params` / `pooling_params` | 采样 / pooling |
| `block_ids` | `tuple[list[int], …]` 各 KV group 的 block 表 |
| `num_computed_tokens` | 已算（前缀命中）token 数 |
| `lora_request` | LoRA |
| `prompt_embeds` / `prompt_is_token_ids` | 嵌入式 prompt |

### 2.3 `CachedRequestData`（嵌在 `scheduled_cached_reqs`）

| 字段 | 语义 |
|------|------|
| `req_ids` | 本步续跑请求 |
| `resumed_req_ids` | 抢占恢复：`new_block_ids` **替换**而非追加 |
| `new_token_ids` | **PP 专用**新 token；非 PP 时常为空 |
| `all_token_ids` | MRV1 connector：未在上步调度的请求补传全 token |
| `new_block_ids` | 每请求新增（或恢复时整表）block |
| `num_computed_tokens` | 每请求已算长度 |
| `num_output_tokens` | 每请求已产出长度（区分 context/decode phase） |

### 2.4 同一步的旁路交付（不算进 `SchedulerOutput` 本体，但 worker 会吃）

| 交付 | 入口 | 语义 |
|------|------|------|
| `GrammarOutput` | 常与 `sample_tokens` 路径配合 | `structured_output_request_ids` + `grammar_bitmask` |
| `IntermediateTensors` | PP：`execute_model(..., intermediate_tensors=)` | 非首段激活输入 |
| DP pad 共识 | runner 内 `dispatch_cg_and_sync_dp` | **不在** SchedulerOutput 里；worker/runner 侧再 sync |

Worker 侧状态机（V2）对 `SchedulerOutput` 的消费序：`finish_requests` → `free_states` → `add_requests` → `update_requests` → `block_tables.apply_staged_writes` → 组 batch → forward。

---

## 3. SGLang：提供给 Worker 的字段全集

SGLang worker 实际吃的是 **`ForwardBatch`**；其字段大多从 **`ScheduleBatch`** 借用或 `init_new` 派生。下列按「Scheduler 已备好、进入 forward 路径」列出。

### 3.1 `ForwardMode`（一等枚举）

源：`forward_batch_info.py::ForwardMode`。

| 值 | 用途 |
|----|------|
| `EXTEND` | Prefill / 残差 prefill |
| `DECODE` | 普通 decode |
| `MIXED` | chunked 混批 |
| `IDLE` | DP 陪跑空步 |
| `TARGET_VERIFY` | target 验证 draft |
| `DRAFT_EXTEND_V2` | draft-extend（同步 draft KV + seed） |
| `PREBUILT` | PD 分离 decode：KV 已就绪 |
| `SPLIT_PREFILL` | PD multiplexing 分层 prefill |
| `DLLM_EXTEND` | 扩散 LLM |

另：`CaptureHiddenMode = NULL | LAST | FULL`（投机要不要吐 hidden）。

### 3.2 `ScheduleBatch`：调度侧批状态（进 worker 前的权威包）

源：`schedule_batch.py::ScheduleBatch`。分组列出。

**A. 请求与引擎寿命资源**

| 字段 | 语义 |
|------|------|
| `reqs` | `List[Req]`：完整请求对象（采样、grammar、token 历史在 Req 上） |
| `req_to_token_pool` / `token_to_kv_pool_allocator` / `tree_cache` | 池与 radix **指针**（引擎私有） |
| `model_config` / `device` / `enable_overlap` | 配置 |
| `hisparse_coordinator` | HiSparse 协调器引用 |

**B. 仅调度用、ForwardBatch 不读（节选）**

| 字段 | 语义 |
|------|------|
| `batch_is_full` / `chunked_req*` / `decoding_reqs` | chunked / 混批调度辅助 |
| `inner_idle_batch` | DP IDLE 内嵌批 |
| `split_*` | split prefill 状态 |
| `req_pool_indices_cpu` | overlap 路径 CPU 镜像 |
| `hicache_consumer_index` | HiCache CPU→GPU 加载同步 |
| `dp_cooperation_info` / `prefill_stats` / `forward_iter` | 指标 |

**C. 交叉到 ForwardBatch 的 GPU / 批张量**

| 字段 | 语义 |
|------|------|
| `input_ids` / `prefill_input_ids_cpu` / `mix_running_indices` | 本步输入 token |
| `input_embeds` / `replace_embeds` / `replace_positions` | 嵌入替换 |
| `ne_token_table` / `ne_skip_token_table_update` | ngram embedding |
| `req_pool_indices` | 请求槽 → pool 行 |
| `seq_lens` / `seq_lens_cpu` / `orig_seq_lens` / `seq_lens_sum` | 序列长度 |
| `out_cache_loc` / `out_cache_loc_dsv4` | **本步 KV 写入槽** |
| `mamba_track_*` / `mamba_cow_*` / `mamba_clear_indices` | hybrid/Mamba 调度与 CoW |
| `encoder_lens` / `encoder_out_cache_loc` | encoder-decoder |
| `extend_input_logprob_token_ids` | extend 段 logprob 用 token |

**D. 模式 / DP / 投机 / 采样标志**

| 字段 | 语义 |
|------|------|
| `forward_mode` / `global_forward_mode` / `recv_skipper_forward_mode` | 本步与全局 mode |
| `is_extend_in_batch` / `can_run_dp_cuda_graph` / `can_run_dp_breakable_cuda_graph` | DP 共识标志 |
| `tbo_split_seq_index` | two-batch overlap 切分 |
| `spec_verify_tier_num_tokens` | verify tier |
| `return_logprob` / `is_prefill_only` / `return_hidden_states` / `has_grammar` | 行为开关 |
| `spec_algorithm` | 投机算法枚举 |
| `extend_num_tokens` | extend 总 token |
| `prefix_lens` / `extend_lens` / `extend_logprob_start_lens` | extend 几何（CPU list） |
| `global_num_tokens` / `global_num_tokens_for_logprob` / `global_spec_verify_tier_num_tokens` | **DP all_gather 结果** |
| `sampling_info` | `SamplingBatchInfo`（批采样态） |
| `spec_info` | `SpecInput`（draft/verify 结构，多含 GPU tensor） |
| `multimodal_inputs` / `top_logprobs_nums` / `token_ids_logprobs` / `encoder_cached` / `encoder_lens_cpu` | MM / logprob / encoder host 元数据 |
| `dllm_config` | 扩散 LLM |

### 3.3 `ForwardBatch`：真正进 ModelRunner.forward 的对象

源：`forward_batch_info.py::ForwardBatch`。除借用 ScheduleBatch 的上述字段外，**forward 侧再派生/填充**：

| 字段（FB 侧增量） | 语义 |
|-------------------|------|
| `positions` | 位置 id（init_new 构造） |
| `extend_seq_lens` / `extend_prefix_lens` / `extend_start_loc` + cpu 镜像 | extend 几何 GPU 化 |
| `global_num_tokens_gpu` / `global_num_tokens_for_logprob_gpu` 等 | DP 尺寸上装置 |
| `original_global_num_tokens_cpu` / `_original_batch_size` / `_original_forward_mode` | pad 前快照 |
| `num_token_non_padded` (+ cpu) | 非 pad token 数 |
| `dp_padding_mode` / `dp_local_start_pos` / `dp_local_num_tokens` / `global_dp_buffer_len` | DP pad 运行时 |
| `lora_ids` / `rids` | 自 `reqs` 导出 |
| `capture_hidden_mode` / `return_hidden_states_before_norm` | 投机 hidden 捕获 |
| `attn_cp_metadata` / `attn_dcp_metadata` / `dcp_kv_mask` | 上下文/decode CP |
| `tbo_parent_token_range` / `tbo_padded_len` / `tbo_children` | TBO 子批 |
| `ngram_embedding_info` | ngram |
| `mm_input_embeds` / `cross_attention_custom_mask` / `mrope_positions` | MM / Qwen2-VL |
| `next_token_logits_buffer` / `temperature` / `top_p` 等 | forward/sample 运行时缓冲 |
| `hidden_states` / `residual` / `split_index` | split prefill 中间态 |

DP token 数 **all_gather 发生在 Scheduler**（`dp_attn.py::prepare_mlp_sync_batch_raw`），写入 `ScheduleBatch.global_num_tokens*`；ModelRunner 的 `forward_batch.prepare_mlp_sync_batch` 只做 **pad/消费**。

---

## 4. 语义桶对照（能对齐的）

| 语义桶 | vLLM | SGLang | 对齐度 |
|--------|------|--------|--------|
| 本步要算哪些请求 | `scheduled_new_reqs` + `scheduled_cached_reqs.req_ids` | `reqs` / `rids` | 中：增量 vs 全量 |
| 本步每请求 token 数 | `num_scheduled_tokens` | decode≈bs；extend=`extend_num_tokens`+`extend_lens` | 中：字典 vs mode 几何 |
| 已算前缀长度 | `num_computed_tokens` | `prefix_lens` / `num_computed` 在 Req | 中 |
| KV 定位 | `block_ids` / `new_block_ids` | `out_cache_loc` + `req_to_token` 行 | **低**：表行 vs 写槽 |
| 投机草稿 | `scheduled_spec_decode_tokens` | `spec_info` + `ForwardMode.TARGET_VERIFY` 等 | **低**：token 列表 vs 富结构 |
| 多模态 | `scheduled_encoder_inputs` + NewRequest.`mm_features` | `multimodal_inputs` / `encoder_*` | 中 |
| 结束/释放 | `finished_req_ids` / `free_encoder_mm_hashes`（Worker **只清 runner 态**；KV 已在 Scheduler `update_from_output` 放下） | Scheduler `process_batch_result` 内 `release_kv_cache`，**不进** FB 信封 | 低（位置不同；阶段对照见 [`sglang/block-lifecycle.md`](../sglang/block-lifecycle.md) / [`vllm/block-lifecycle.md`](../vllm/block-lifecycle.md)「请求结束的调度阶段」） |
| 抢占 | `preempted_req_ids` / `resumed_req_ids` | 调度内 retract；无同构 Output 字段 | 低 |
| Structured output | `GrammarOutput` + pending 标志 | `has_grammar` + `Req.grammar` | 中（时序模型不同） |
| 外部 KV | `kv_connector_metadata` | HiCache / disagg 另路径，无同名 metadata | 低 |
| 新块清零 / CoW | `new_block_ids_to_zero` / `kv_cache_block_copies` | mamba 专用 `mamba_cow_*` | 低 |
| 前向模式 | （隐式） | `ForwardMode` 一等 | **SGLang 独有显式** |
| DP step sync | runner/`dispatch_cg_and_sync_dp` | Scheduler `global_num_tokens*` + IDLE | **层级不同** |
| Cascade attn | `num_common_prefix_blocks` | 无对等 | vLLM 独有 |
| 采样参数载体 | 首次在 NewRequest；后续 runner 态 | 每步 `sampling_info` | 中 |

---

## 5. 对不上的字段清单（差异表）

### 5.1 仅 vLLM `SchedulerOutput` 侧显著、SGLang 无同构

| 字段 | 背后原因（见 §6） |
|------|-------------------|
| new/cached 二分 + `resumed_req_ids` | 跨进程增量同步 worker 缓存态 |
| `all_token_ids`（MRV1） | connector 与调度步不同步时的补传 |
| `num_common_prefix_blocks` | cascade attention 调度提示 |
| `finished_req_ids` / `preempted_req_ids` 进信封 | worker 需显式被告知释放/抢占 |
| `kv_connector_metadata` / `ec_connector_metadata` | 插件式外置 KV |
| `new_block_ids_to_zero` / `kv_cache_block_copies` | worker 持物理 KV，调度指挥清零/CoW |
| `GrammarOutput` 独立消息 | async sample 与 forward 拆开 |
| `num_invalid_spec_tokens` / `num_spec_tokens_to_schedule` | 动态投机与统计挂在调度输出 |
| `pending_structured_output_tokens` | async grammar 缺 token 时 defer |

### 5.2 仅 SGLang 侧显著、vLLM `SchedulerOutput` 无同构

| 字段 | 背后原因（见 §6） |
|------|-------------------|
| `ForwardMode` / `global_forward_mode` / IDLE | 同进程批对象直接编码执行形态；DP 陪跑一等公民 |
| `global_num_tokens*` 等 DP sync 结果 | **Scheduler 层** mlp sync（见 model-runner DP 节） |
| `can_run_dp_cuda_graph*` / `is_extend_in_batch` | 与上同一 all_gather |
| `out_cache_loc` / `req_pool_indices` / 已组 `input_ids`·`seq_lens` | 调度已做完 alloc + 组张量；runner 薄 |
| `prefix_lens` / `extend_lens` / `extend_logprob_start_lens` | extend 几何显式 |
| `spec_info`（富结构） | SpecWorker 与 target 共置，状态直接挂批 |
| `sampling_info` 整包 | 采样批态跟批走，不必增量同步 |
| `tree_cache` / pool 指针 / `hicache_consumer_index` | 调度与内存池同进程 |
| `tbo_*` / `dllm_*` / `split_*` / `PREBUILT` | TBO、扩散、PD decode 就绪等产品路径 |
| `mamba_track_*` / `mamba_cow_*` | hybrid 状态调度细节进批 |

### 5.3 名字像、语义不对齐

| 概念 | vLLM | SGLang | 错位 |
|------|------|--------|------|
| 「block 句柄」 | 逻辑 `block_ids` 表 | `out_cache_loc` 写槽 + `req_to_token` 行 | 表 vs 本步写位置 |
| 「投机输入」 | draft **token ids** | `spec_info` + mode | 扁平 vs 结构化 |
| 「已算长度」 | `num_computed_tokens` | `prefix_lens` / seq | 前缀命中 vs 当前 seq |
| 「本步工作量」 | per-req `num_scheduled_tokens` | mode + `extend_num_tokens` | 统一字典 vs 分 mode |
| 「空闲陪跑」 | `execute_dummy_batch` / `_dummy_run` | `ForwardMode.IDLE` 真实调度空步 | dummy 旗标 vs 一等 mode |

---

## 6. 架构差异分析（字段分叉的根因）

### 6.1 进程边界：增量消息 vs 同进程批对象

- **vLLM**：Scheduler 与 Worker 可分离（mp Executor）。跨进程成本迫使 `SchedulerOutput` 做 **diff**：首次全量 `NewRequestData`，之后只推 `CachedRequestData`；`finished_req_ids` 显式回收 worker 侧缓存。
- **SGLang**：调度与 `ModelRunner` 同进程（每 GPU 一 Scheduler 时更甚）。`ScheduleBatch` 可直接挂 `Req`、池指针、GPU tensor，**无需** new/cached 协议。

→ 字段爆炸点不同：vLLM 多「生命周期增量」字段；SGLang 多「已物化张量 / 池句柄」字段。

### 6.2 Worker 胖瘦：状态机在 runner vs 在 scheduler

- **vLLM V2 `GPUModelRunner`**：吃 `SchedulerOutput` 后自己 `add/update/free_requests`、维护 `RequestState` / `InputBuffers` / `block_tables`，再组 attn 输入——**GPU 侧状态机 + 执行**。
- **SGLang `ModelRunner`**：吃已备好的 `ForwardBatch`，主责 forward/sample/graph——**薄执行内核**；请求生命周期在 Scheduler + `Req`。

→ vLLM Output 偏「指令」；SGLang Batch 偏「已装填弹药」。

### 6.3 KV 所有权：调度填表 vs 调度写槽

- **vLLM**：`KVCacheManager` 在调度侧分配 `block_ids`，经 Output 交给 worker；worker `apply_staged_writes` 维护 device block table。物理 KV 仍在引擎。
- **SGLang**：`alloc_for_extend/decode` 在调度路径写 `req_to_token` 并产出 `out_cache_loc`；attn 经 pool 索引读。同样引擎私有，但**暴露给 forward 的是写槽而非整表 diff**。

→ 两边都「引擎拥有 KV」，但 **Output 里 KV 句柄的形状不同**（表行增量 vs 本步写位置）。

### 6.4 前向模式：隐式工作量 vs 显式 ForwardMode

- **vLLM**：用 `num_scheduled_tokens`、是否在 prefill、spec dict 等**推断**本步形态；无单一 mode 枚举进 Output。
- **SGLang**：`ForwardMode` 覆盖 extend/decode/mixed/idle/verify/draft-extend/prebuilt/…，图捕获与 kernel 选择直接打在 mode 上。

→ SGLang 字段多「mode + 几何」；vLLM 多「per-req 计数」。

### 6.5 DP 同步层级

- **SGLang**：`prepare_mlp_sync_batch_raw` 在 **Scheduler** `all_gather` token 数 / graph 标志 → 写 `global_num_tokens*` → 必要时 `IDLE`。
- **vLLM**：DP 协调偏 `DPCoordinator` + wave；pad/graph 共识多在 **runner**（`dispatch_cg_and_sync_dp`），**不进** `SchedulerOutput`。

→ 同是「跨 DP 对齐」，字段出现的层不同——lake 已定跟 SGLang：**sync 落 node_scheduler**（[`scheduling.md`](../../architecture/scheduling.md) §3.1）。

### 6.6 投机与 structured output 的挂载

- **vLLM**：投机以 **token 列表**进 Output；structured 用 **独立 `GrammarOutput`**，配合 async `pending_*` defer sample。
- **SGLang**：投机以 **`spec_info` + ForwardMode** 挂在批上（SpecWorker 编排）；grammar 挂 `Req`，`has_grammar` 批级标志。

→ 丰富度与异步拆分策略不同，导致字段无法一一映射。

### 6.7 外置 KV / 存算分离接口

- **vLLM**：一等 `kv_connector_metadata`——调度已决定的外部 KV 动作随 Output 下发。
- **SGLang**：HiCache / disagg 走 mem_cache / disaggregation 子系统，**不**做成 SchedulerOutput 式 metadata 字段。

→ lake 的 `pool_iface` ready/done 更接近「必经存算边界」，但**不应照搬** vLLM connector metadata 进引擎权威；表组装归 agent（Q1/Q2）。

---

## 7. 对 lake 的含义（定 D1 时怎么用本文）

结合已定：V2 目录 + 薄 runner、`node_scheduler` 出信封、DP sync 在 Scheduler、KV/表归 agent。

| 决策倾向 | 依据 |
|----------|------|
| **信封外形偏 vLLM** | 增量 new/cached/finished 利于「一份调度扇出多卡」与日后进程切分；对齐 `execute_model(SchedulerOutput)` |
| **执行形态 / DP 字段偏 SGLang** | `ForwardMode`（或等价枚举）+ `global_num_tokens*` + IDLE 进信封；sync 不进 ModelRunner |
| **KV 句柄两边都不照搬** | 不传引擎权威 `block_ids` / `out_cache_loc`；改为 read/write set 或与 agent ready 契约衔接（lake 独有） |
| **投机分阶段** | 初版可先 vLLM 式 draft token 列表；深化再引入 SGLang 式 `spec_info` |
| **Grammar** | 参考 vLLM 拆 `GrammarOutput` 的异步能力，但挂载点跟 lake `sample/` 时序另定（见 guided-decoding research） |
| **不要进 lake Output 的** | pool 指针、`tree_cache`、HiCache consumer index、worker 私有 RequestState 全量 |

**lake 独有、两边都无的**（D1 必须自定）：`read_set` / `write_set`（或等价）、与 `pool_iface` ready/done 的时序字段、角色配置下的可接 batch 约束。

---

## 8. 代码索引

| 概念 | 文件:符号 |
|------|-----------|
| vLLM 调度输出 | `vllm/v1/core/sched/output.py::{SchedulerOutput,NewRequestData,CachedRequestData,GrammarOutput}` |
| vLLM 消费 | `vllm/v1/worker/gpu/model_runner.py::GPUModelRunner.execute_model` / `add_requests` / `update_requests` |
| vLLM DP pad | `vllm/v1/worker/gpu/dp_utils.py::dispatch_cg_and_sync_dp` |
| SGLang 批 | `managers/schedule_batch.py::ScheduleBatch` |
| SGLang 前向批 | `model_executor/forward_batch_info.py::{ForwardBatch,ForwardMode}` |
| SGLang worker 入口 | `managers/tp_worker.py::TpModelWorker.forward_batch_generation` |
| SGLang DP sync | `managers/scheduler_components/dp_attn.py::prepare_mlp_sync_batch_raw` |
| 对照叙事 | [`sglang/model-runner.md`](../sglang/model-runner.md)「与 vLLM ModelRunner V2 对照」「Data Parallel」 |
