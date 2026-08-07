# Guided / Structured Decoding — SGLang × vLLM（async / overlap 下的同步）

> 源码:`3rdparty/sglang`、`3rdparty/vllm`。本文对照两边对 **grammar-guided / structured output** 的支持，重点回答：在 overlap / async scheduling 下能否避免 host↔device 同步、让 GPU 执行路径**完全无空闲**；以及 xgrammar / llguidance / outlines 等库是否把 FSM 放到了 GPU。
>
> 不涉及 HiCache / KV connector（见各 overview）；计算层总览见 [sglang/model-runner.md](sglang/model-runner.md)、[vllm/compute.md](vllm/compute.md)。thinking 开关/长度预算见 [sglang/thinking-control.md](sglang/thinking-control.md)（与 grammar 正交，可叠加）。采样参数字段对照见 [sampling-params.md](sampling-params.md)。

## 一句话结论

**两边都不能在所有场景下做到 device 完全无空闲。** 库侧只有「bitmask 打到 logits」的 GPU kernel；FSM / `accept_token` / `fill_*_bitmask` 仍在 **CPU**。overlap / async 能把这段 CPU 藏进 forward 阴影里（mask 填得比 forward 短时接近零开销），但 **async + structured（尤其再加 spec）仍会被迫等上一轮真实 token**，在 sample 前出现 GPU 气泡。

## 库层：GPU 支持到哪一步

| 能力 | xgrammar | llguidance | outlines |
|------|----------|------------|----------|
| FSM / `accept_token` / `fill_*_bitmask` | **CPU** | **CPU** | **CPU** |
| bitmask 分配 | CPU `int32` packed | CPU | CPU / 散列表 |
| `apply_token_bitmask` | CUDA / Triton / CPU | `llguidance.torch` GPU apply | 多为 `masked_fill_` |

xgrammar 官方集成文档写死：bitmask 在 CPU 填，logits 在 GPU 时再 H2D + GPU kernel。**没有「GrammarMatcher 上 GPU」这条路径。**

引擎侧对应：

| 引擎 | 填 mask | H2D | Apply |
|------|---------|-----|-------|
| SGLang | `GrammarMatcher.fill_next_token_bitmask`（CPU） | `move_vocab_mask(..., non_blocking=True)` | Triton（CUDA）/ `sgl_kernel`（HIP）/ NPU op |
| vLLM | `StructuredOutputManager.grammar_bitmask`（CPU；大批量线程池并行 fill） | `to(..., non_blocking=True)`；V2 `StructuredOutputsWorker` 专用 copy stream | xgrammar CUDA/Triton，或 V2 自研 Triton kernel |

## 理想重叠模型（两边共同追求）

```text
GPU:  forward(N) ──────────────────────────► apply_bitmask + sample(N)
CPU:           accept(N-1) + fill_bitmask(N) ─┘
```

依赖：下一步 bitmask **只依赖已生成 token**（与本步 forward 无关）；故可先 launch forward，再在 CPU 上推进 FSM / 填 mask，仅在 sample 前汇合。xgrammar 论文与 TensorRT-LLM tech blog 同一套路。

破裂点：async / overlap 下本步 sample 往往还依赖**上一 in-flight 步的真实 token**；token 未回 CPU 前不能诚实填 mask → defer sample 或关掉 overlap → GPU 空转。

## SGLang：overlap schedule × grammar

> Overlap 主循环、FutureMap、关 overlap 条件的**完整机制**见 [`sglang/model-runner.md`](sglang/model-runner.md)「Overlap schedule」；本节只谈与 grammar 的交叉。

后端选择：`--grammar-backend` ∈ `{xgrammar, outlines, llguidance, none}`（默认 xgrammar）。

### 非 spec：可重叠

`event_loop_overlap` 顺序：

1. `run_batch` 起 GPU forward  
2. `process_batch_result`（`copy_done.synchronize` → token 回 CPU → `accept_token`）  
3. `launch_batch_sample_if_needed` → `update_regex_vocab_mask` → apply → sample  

注释写明：**sample 依赖上一 batch 的 grammar 状态**。正常 decode 下这就是「forward 与 CPU grammar 重叠」；mask 时延 ≪ forward 时开销接近零。

### 硬缺口：overlap + spec + grammar

```1630:1641:3rdparty/sglang/python/sglang/srt/managers/scheduler.py
        # We do not support overlap + spec + grammar yet,
        # so we need to turn off overlap for this batch.
        # TODO(lsyin): support overlap + spec + grammar
        need_grammar_sync = (
            batch
            and not batch.spec_algorithm.is_none()
            and batch.has_grammar
            and batch.forward_mode.is_decode()
            and len(self.result_queue) > 0
        )
```

`is_disable_overlap_for_batch` 在此强制 drain `result_queue`，**关掉 overlap**。

额外同步：grammar 启用时 TP 对 sampled token ids 做 `all_reduce(MIN)`（`sampler.py::_sync_token_ids_across_tp`），防各 rank 采样非确定性导致 FSM 分叉。

**SGLang 判断**：非 spec guided ≈ 能藏进 overlap；**spec + grammar 明确不支持 overlap，做不到无空闲**。

### draft tree 的预计算：已有覆盖与未消除的依赖

「为每个可能接受长度预生成 grammar 状态，验证后按 `accepted_len` 选择」不是空白思路：SGLang target verify 前已经做了它的**前半段**。`spec_utils.py::traverse_tree` DFS draft tree，在每条合法分支上 `grammar.accept_token`，为该节点填下一 token 的 mask，再以 `grammar.rollback(1)` 回到父状态；`generate_token_bitmask` 将整棵树的 CPU packed masks 交给 verify batch。这样 target 可在一次 `q_len > 1` verify 中对每个 draft 节点应用与其前缀相符的 grammar mask，不必等 verify 后才生成这些 mask。

但这不是可长期持有、按接受长度直接切换的 matcher snapshot，且不能消除下一轮的全部 host 依赖：

1. **接受的 draft 前缀**：可按 target 的 `accepted_len` 选择对应分支；若 draft 是线性链，状态数为 `K+1`，若是 tree 则随合法节点数增长。SGLang 以 DFS + rollback 临时重建，避免为每个节点复制完整 `GrammarMatcher`。
2. **拒绝位置的 replacement / 全接受后的 bonus token**：它们是 target sampling 的真实输出，verify 前未知，不能由有限个 draft-prefix 状态覆盖（除非枚举整个 vocab）。最终已提交 grammar state 仍须接收这个 token，或由 GPU FSM 在 device 上推进。
3. **分支选择本身**：如果 `accepted_len`、replacement token 或 finish 判定必须回 host 才能更新请求状态，下一轮仍有 D2H/CPU 依赖。预计算能缩短 grammar mask 准备的关键路径，**不能单独证明 overlap × spec × grammar 无气泡**。

因此 SGLang 的 `need_grammar_sync` 仍然合理：它保护的是上述“真实结果已返回前不能提交 host grammar 游标”的依赖，不是因为 verify 内完全没有分支状态的预计算。

## vLLM：async scheduling × structured output

后端：`xgrammar`（默认）/ `outlines` / `guidance`(llguidance) / `lm-format-enforcer`。

### 理想路径（`EngineCore.step`）

```text
execute_model(non_block=True)
  → get_grammar_bitmask(...)   # CPU，与 GPU forward 并行
  → sample_tokens(grammar_output)
```

与 xgrammar 重叠模型一致；H2D 已 `non_blocking`，V2 runner 用独立 copy stream 避免阻塞 memcpy（历史 PR #12563 同类问题）。

### async + structured：defer sample

`AsyncScheduler` 给 in-flight 请求加 `num_output_placeholders`；若 structured 请求尚有未兑现 placeholder，置 `pending_structured_output_tokens`。

`step_with_batch_queue`：

- **无 pending**：立刻 `get_grammar_bitmask` + `sample_tokens(non_block)`  
- **有 pending**：**defer** `sample_tokens`，等先验 batch 产出真实 token（及可选 draft 校验）后再填 bitmask  

→ 当前 batch 的 forward 可能已结束，GPU 在等 sample → **气泡**。再加 speculative decoding 时还要 draft token 回 CPU，defer 更重。

**vLLM 判断**：常见「非 pending」路径接近零开销；**async 下只要 structured 依赖 in-flight token，就不能保证 device 全程忙**。

## 场景对照

| 场景 | SGLang | vLLM |
|------|--------|------|
| 普通 guided + overlap/async | 可重叠，近零 | 可重叠，近零 |
| guided + speculative | **强制关 overlap** | defer sample / 等 draft |
| FSM 上 GPU？ | 否 | 否 |
| GPU kernel | apply bitmask | apply bitmask（+ copy stream） |
| 编译异步 | grammar_queue + future | `_use_async_grammar_compilation` + executor |

TensorRT-LLM 更进一步：把 grammar advance / mask gen 挂 **CUDA callback**，塞进 CUDA graph，减轻 spec+guided 同步——**本仓 submodule 的 SGLang / vLLM 均未做到这一档**。

## 与 lake 的关系

| 关注点 | 参考实现 | lake |
|--------|----------|------|
| 结构化约束正确性 | xgrammar / llguidance FSM + bitmask | 可直接复用同库或等价接口；FSM 游标属请求控制态（抢占重算时须随迁或重放 token 复原，见 [`../architecture/scheduling.md`](../architecture/scheduling.md)） |
| 隐藏 CPU 开销 | forward ∥ fill_bitmask，sample 前汇合 | **应照搬**此重叠契约；worker 上报信号、gateway 管过载，不在引擎内为 grammar 降 batch |
| device 绝对无空闲 | 未做到（async/spec 破洞） | 若要绝对零气泡：自研 GPU FSM 或 CUDA-callback-in-graph（TRT-LLM 方向），代价远高于接 xgrammar——**默认接受「mask ≪ forward」近零，不把绝对无空闲当硬 SLO** |
| grammar 归属 | host `Req` / scheduler 侧 manager | 与 lake「语义状态在 host、device 只镜像执行必要张量」一致（见 model-runner「请求数据结构」） |

### lake 的后续候选：GrammarFrontier（非 C13 交付）

当 C4 接入真实 speculative decoding 后，可将每个 structured 请求在一次 verify 前得到的结果显式建模为**一次性 `GrammarFrontier`**，而不是把它误称为持久 GPU state：

```text
host committed matcher state
  + draft tree (token ids, parent/sibling)
      → DFS accept / fill mask / rollback
      → GrammarFrontier { node → packed next-token mask, parent relation }
      → async H2D copy → target verify
      → accepted path + replacement/bonus token
      → host commit，或未来 device FSM commit
```

- **短期（CPU FSM）**：frontier 的 packed mask 与 verify input 同生命周期，copy 可与 target forward 重叠；验证结果可先在 device 侧用于 token/KV relay，但 host 要在提交 replacement/bonus token 前推进权威 matcher。若这一步赶不上下一轮 sample，保留 C13 的 drain/defer，不以错误状态换吞吐。
- **规模边界**：只遍历 draft tree 已出现且 grammar 合法的节点；不按 vocab 枚举 replacement token，也不复制完整 matcher。frontier 的节点数、CPU DFS 时间、packed-mask bytes/H2D 时间应被 P7 记录，作为 draft 宽度/深度的预算信号。
- **长期（消除最后依赖）**：需要可在 GPU 上推进并提交 DFA/FSM state，或类似 TensorRT-LLM 的 CUDA callback-in-graph。此时 host 只异步镜像游标；必须定义 device state 的请求级所有权、抢占/恢复的 checkpoint，以及 TP 各 rank 一致的 token/state 提交协议。

**决策**：C13 当前“spec + structured + overlap 时 drain”保持不变。`GrammarFrontier` 是在真实 spec 接入后测量的优化候选；只有 benchmark 证明 host commit 是瓶颈，才评估 GPU FSM / CUDA callback 的工程成本。它不改变 KV 属池、Host `Req` 归 `NodeScheduler`、也不引入引擎私有请求状态。

**结论**：接现成库 + 重叠调度即可覆盖主流 structured output；不要假设「库已把 guided decoding 全部 GPU 化」。spec + guided 的无气泡路径是增量课题，不是开箱能力。

## 代码索引

> 符号名稳定锚定，行号会漂移——找不到时 `grep -n "符号名" 3rdparty/<repo>/<文件路径>`。

### 库 / 后端

| 机制 | 文件:符号 |
|------|-----------|
| SGLang xgrammar grammar 对象 | `python/sglang/srt/constrained/xgrammar_backend.py`::`XGrammarGrammar`（`accept_token` / `fill_vocab_mask` / `move_vocab_mask` / `apply_vocab_mask`） |
| SGLang llguidance | `python/sglang/srt/constrained/llguidance_backend.py`::`GuidanceBackend` |
| SGLang outlines | `python/sglang/srt/constrained/outlines_backend.py` |
| SGLang grammar 编译队列 | `python/sglang/srt/constrained/grammar_manager.py`::`GrammarManager` |
| SGLang Triton apply | `python/sglang/kernels/ops/grammar/bitmask_ops.py`::`apply_token_bitmask_inplace_triton` |
| vLLM xgrammar backend | `vllm/v1/structured_output/backend_xgrammar.py`::`XgrammarGrammar.fill_bitmask` |
| vLLM 引擎侧 manager | `vllm/v1/structured_output/__init__.py`::`StructuredOutputManager.grammar_bitmask` |
| vLLM apply（V1 runner 路径） | `vllm/v1/structured_output/utils.py`::`apply_grammar_bitmask` |
| vLLM apply（V2 + copy stream） | `vllm/v1/worker/gpu/structured_outputs.py`::`StructuredOutputsWorker.apply_grammar_bitmask` |

### 调度 / overlap / async

| 机制 | 文件:符号 |
|------|-----------|
| SGLang overlap 主循环 | `python/sglang/srt/managers/scheduler.py`::`event_loop_overlap` / `launch_batch_sample_if_needed` |
| SGLang 关 overlap（spec+grammar） | `scheduler.py`::`is_disable_overlap_for_batch`（`need_grammar_sync`） |
| SGLang 填/打 mask | `python/sglang/srt/sampling/sampling_batch_info.py`::`update_regex_vocab_mask` / `apply_logits_bias` |
| SGLang accept token | `python/sglang/srt/managers/scheduler_components/batch_result_processor.py`::`_accept_grammar_tokens` |
| SGLang TP sync（grammar） | `python/sglang/srt/layers/sampler.py`::`_sync_token_ids_across_tp` |
| SGLang spec 路径 bitmask | `python/sglang/srt/speculative/spec_utils.py`::`generate_token_bitmask` |
| SGLang draft tree grammar frontier | `python/sglang/srt/speculative/spec_utils.py`::`traverse_tree`（`accept_token` → `fill_vocab_mask` → `rollback`） |
| vLLM sync step 重叠 | `vllm/v1/engine/core.py`::`step`（`execute_model` → `get_grammar_bitmask` → `sample_tokens`） |
| vLLM async batch queue + defer | `vllm/v1/engine/core.py`::`step_with_batch_queue`（`pending_structured_output_tokens`） |
| vLLM async placeholder | `vllm/v1/core/sched/async_scheduler.py`::`AsyncScheduler._update_after_schedule` |
| vLLM bitmask 入口 | `vllm/v1/core/sched/scheduler.py`::`get_grammar_bitmask` |
| vLLM sample 打 mask | `vllm/v1/worker/gpu_model_runner.py`::`sample_tokens` |

### 外部文档（非本仓）

| 主题 | 出处 |
|------|------|
| xgrammar CPU fill + GPU apply + 与 forward 重叠 | [XGrammar paper](https://arxiv.org/abs/2411.15100)；[Engine integration](https://xgrammar.mlc.ai/docs/tutorials/engine_integration.html) |
| CUDA callback + CUDA graph（spec+guided） | [TensorRT-LLM tech blog](https://nvidia.github.io/TensorRT-LLM/latest/blogs/tech_blog/blog12_Combining_Guided_Decoding_and_Speculative_Decoding.html) |
| vLLM 非阻塞 bitmask H2D | [vllm#12563](https://github.com/vllm-project/vllm/pull/12563) |
