# Sampling Parameters — SGLang × vLLM

> 源码:`3rdparty/sglang/python/sglang/srt/sampling/sampling_params.py`、`managers/io_struct.py::GenerateReqInput`；`3rdparty/vllm/vllm/sampling_params.py`。  
> 结构化约束细节见 [guided-decoding.md](guided-decoding.md)；thinking 预算见 [sglang/thinking-control.md](../sglang/thinking-control.md)。

## 一句话结论

两边共享 OpenAI 风格核心采样（temperature / top_p / top_k / min_p / penalties / stop / n），但**命名、挂载位置、扩展能力不同**。SGLang 的 `n` 是**同 prompt 复制成 n 条独立采样**（不是 beam search）；vLLM 在开启 speculative decoding 时**硬禁** `min_p` 与 `logit_bias`——因为 spec 路径根本不装这两个 logits processor，装了也会与 rejection sampling 分布不一致。penalty 在 V1 async 下有硬 sync 空泡风险，V2 / SGLang 走设备侧持久统计（深挖 3）。采样状态跟请求走 Decode 侧、不进 KV 池；spec 兼容是一张矩阵而非单一禁令；`n` 副本可共享前缀 KV（深挖 4）。

## 两边都有（核心采样）

| 参数 | vLLM | SGLang | 备注 |
|------|------|--------|------|
| `temperature` | ✓ 默认 1.0；≈0 → greedy | ✓ 默认 1.0；≈0 → 内部改 `top_k=1` | greedy 语义不同 |
| `top_p` | ✓ (0,1] | ✓ | |
| `top_k` | ✓ `0`/`-1`=全词表 | ✓ `-1`→内部 `TOP_K_ALL` | 禁用约定不同 |
| `min_p` | ✓ | ✓ | vLLM：与 spec decode **硬不兼容**（见下） |
| `presence_penalty` | ✓ [-2,2] | ✓ | |
| `frequency_penalty` | ✓ [-2,2] | ✓ | |
| `repetition_penalty` | ✓ >0 | ✓ (0,2] | vLLM 上界更松 |
| `n` | ✓ | ✓ | 并行样本数；**不是 beam**（见下） |
| `stop` / `stop_token_ids` | ✓ | ✓ | |
| `ignore_eos` | ✓ | ✓ | |
| `skip_special_tokens` | ✓ | ✓ | |
| `spaces_between_special_tokens` | ✓ | ✓ | |
| `logit_bias` | ✓ `dict[int,float]` | ✓ `dict[str,float]` | vLLM：与 spec decode **硬不兼容** |
| seed | `seed` | `sampling_seed` | 命名不同 |
| 最大/最小生成长度 | `max_tokens` / `min_tokens` | `max_new_tokens` / `min_new_tokens` | 命名不同 |
| stop 串是否保留在输出 | `include_stop_str_in_output` | `no_stop_trim`（语义相反） | |

## 结构化约束

| | vLLM | SGLang |
|--|------|--------|
| 形态 | 嵌套 `structured_outputs` | 扁平字段 |
| JSON schema | `structured_outputs.json` | `json_schema` |
| JSON object | `json_object` | （用 schema / `$$ANY$$` 等） |
| regex | ✓ | ✓ |
| grammar | `grammar` | `ebnf` |
| choice | ✓ | ✗（引擎 SamplingParams 无） |
| structural_tag | ✓ | ✓ |

互斥：两边都要求多种 grammar 约束只能选一种。详见 [guided-decoding.md](guided-decoding.md)。

## 仅 vLLM（引擎 `SamplingParams`）

| 参数 | 作用 |
|------|------|
| `bad_words` | 禁止词（tokenize 成 token 序列） |
| `allowed_token_ids` | 白名单 token |
| `thinking_token_budget` | thinking 长度预算（`-1`=不限） |
| `repetition_detection` | N-gram 重复早停 |
| `logprobs` / `prompt_logprobs` | 采样 / prompt logprobs |
| `logprob_token_ids` | 指定 token 的 logprob |
| `flat_logprobs` | 扁平 logprob 格式 |
| `detokenize` | 是否 detokenize |
| `output_kind` | CUMULATIVE / DELTA / FINAL_ONLY |
| `extra_args` | 插件扩展 |
| `BeamSearchParams` | **独立** beam search API（`beam_width` / `length_penalty`…） |

## 仅 SGLang

**在 `SamplingParams` 上：**

| 参数 | 作用 |
|------|------|
| `stop_regex` | 正则 stop |
| `custom_params` | 给 custom logit processor 的 JSON 参数 |
| `stream_interval` | 流式间隔 |

**在请求级 `GenerateReqInput`（不进 SamplingParams）：**

| 参数 | 对应 vLLM |
|------|-----------|
| `return_logprob` / `top_logprobs_num` / `logprob_start_len` / `token_ids_logprob` | ≈ `logprobs` + `prompt_logprobs` + `logprob_token_ids` |
| `custom_logit_processor` | vLLM 靠 `logits_processors` / 插件，非同构字段 |
| `return_hidden_states` / `return_routed_experts` | vLLM 另有 routed experts 相关配置 |

内置 custom processor 示例：`ThinkingBudgetLogitProcessor`、`DisallowedTokensLogitsProcessor`（近似 vLLM 的 thinking budget / allowed·bad tokens，但是扩展路径）。

---

## 深挖 1：SGLang「主路径是 n + sampling」是什么意思？

**不是 beam search。** OpenAI / 两边引擎的 `n` 语义都是：同一 prompt 产出 **n 条彼此独立的完整 completion**。SGLang 的实现是请求展开：

1. `GenerateReqInput._handle_parallel_sampling` 读 `sampling_params["n"]` → `parallel_sample_num`  
2. `_normalize_batch_inputs` 把 batch 扩成 `batch_size * n`  
3. `_expand_inputs` 把 `text` / `input_ids` / `sampling_params` 等 **按 n 份复制**  
4. 之后每条副本走普通 continuous-batching **独立采样**（各自 temperature / top_p / RNG）

```401:451:3rdparty/sglang/python/sglang/srt/managers/io_struct.py
    def _handle_parallel_sampling(self):
        ...
            self.parallel_sample_num = self.sampling_params.get("n", 1)
        ...
    def _normalize_batch_inputs(self):
        ...
            num = self.batch_size * self.parallel_sample_num
```

| | `n` + sampling（SGLang / OpenAI 主路径） | 经典 beam search（vLLM `BeamSearchParams`） |
|--|------------------------------------------|---------------------------------------------|
| 假设数关系 | n 条**独立**序列，互不看对方分数 | 共享前缀的 beam，按联合分数剪枝 |
| 每步动作 | 各自 sample 一个 next token | 对每条 beam 扩词表 / top-k，再全局取 top-`beam_width` |
| 打分 | 无跨序列比较 | `length_penalty` 等联合打分 |
| 多样性来源 | 随机采样（temperature 等） | 搜索宽度 + 可选温度 |
| SGLang | **这就是 `n` 的全部含义** | 引擎主路径**无**等价 `BeamSearchParams` |

所以说「主路径是 n + sampling」=：要多候选时，用 **独立并行采样**；不要指望 SGLang 默认 decode 环里有 HF/vLLM 那种 beam 状态机。vLLM 则把 beam 拆成**另一条 API**（`BeamSearchParams`），与 `SamplingParams.n` 分开。

### 实现结果会不会有差异？

**会有系统性差异——它们不是同一算法的两种实现。**

| | vLLM beam search（`BeamSearchParams`） | SGLang `n` + sampling（≈ vLLM 的 `SamplingParams.n`） |
|--|----------------------------------------|------------------------------------------------------|
| 每步 | 各 beam 扩候选，**全局**按累计 logprob 留 `beam_width` 条 | n 条序列**各自** sample，互不看对方 |
| 目标 | 近似高概率序列（MAP / 搜索） | 从分布里独立抽 n 个样本 |
| 分数 | `cum_logprob / len^length_penalty`（见 `beam_search/utils.py`） | 无跨序列打分 |
| 多样性 | 宽度内的竞争路径；`temperature=0` 时偏确定 | 靠 temperature / top_p / seed，路径可完全分叉 |
| 前缀 | 共享前缀、剪掉弱分支 | 无共享剪枝；弱路径也会跑完 |

典型对照：

1. **同一 prompt、`beam_width = n` 且 temperature=0**：beam 更偏向「模型认为概率高」的几条；`n` 采样若每条都 greedy（top-1）会因无搜索而**全部相同**（同一条贪心路径）；有温度时则是 n 条随机轨迹——候选集合通常与 beam 的 top-n **对不齐**。
2. **有温度时**：`n` 采样方差大、可重复性依赖 seed；beam 仍由联合分数主导，结果更像「几条高分变体」。
3. **等价对比对象**：不要拿「vLLM beam」对「SGLang `n`」。要比两边「多候选」是否一致，应对 **vLLM `SamplingParams.n` ↔ SGLang `n`**（都是独立并行采样）；beam 是第三条路径。

用途分工：要「几条好答案 / 搜索」→ beam；要「多样本估计 / 投票 / 多样性」→ `n`。

---

## 深挖 2：为何 vLLM 禁止 `min_p` / `logit_bias` 与 speculative decoding 同开？

硬拒绝在 `SamplingParams._validate_spec_decode`：

```877:882:3rdparty/vllm/vllm/sampling_params.py
        if self.min_p > _SAMPLING_EPS or self.logit_bias:
            raise ValueError(
                "The min_p and logit_bias sampling parameters "
                "are not yet supported with speculative decoding."
            )
```

**根因不是「理论上不能」，而是「spec 路径没接上」。**

### 普通采样路径

`build_logitsprocs` 默认装三个内置 processor：

```49:53:3rdparty/vllm/vllm/v1/sample/logits_processor/__init__.py
BUILTIN_LOGITS_PROCESSORS: list[type[LogitsProcessor]] = [
    MinTokensLogitsProcessor,
    LogitBiasLogitsProcessor,
    MinPLogitsProcessor,
]
```

`MinPLogitsProcessor.apply`：softmax → 按 `max_prob * min_p` 阈值 → 填 `-inf`。  
`LogitBiasLogitsProcessor.apply`：对指定 `(req, token)` 加 bias。两者都假定 logits 形状是 **`[num_reqs, vocab]`**（每请求一行）。

### Spec 路径

开启 speculative decoding 时，**同一函数故意只留 `MinTokensLogitsProcessor`**：

```200:209:3rdparty/vllm/vllm/v1/sample/logits_processor/__init__.py
    if vllm_config.speculative_config:
        ...
        logger.warning(
            "min_p and logit_bias parameters won't work with speculative decoding."
        )
        return LogitsProcessors(
            [MinTokensLogitsProcessor(vllm_config, device, is_pin_memory)]
        )
```

Rejection sampler 侧的 logits 是 **展平的** `[Σ(draft_i)+batch, vocab]`（每条 draft 位置一行 + bonus）。它对已支持的约束做了「按 draft 展开」：

- penalties / `allowed_token_ids`：`repeat_interleave(num_draft_tokens)` 后 mask  
- `bad_words`：`apply_bad_words_with_drafts`  
- `min_tokens`：`MinTokensLogitsProcessor.apply_with_spec_decode`  

**没有** `MinPLogitsProcessor.apply_with_spec_decode`，也**没有**在 rejection 路径里调用 `LogitBiasLogitsProcessor`。

### 若强行同开会怎样？

Rejection sampling（Leviathan 等）要求：用 **与最终采样一致的 target 分布** `q(·)` 去验收 draft token。若 `min_p` / `logit_bias` 只在「无 draft 的普通 sampler」上生效、却未作用到 verify 的每一行 logits，则：

1. accept/reject 用的是**未约束**（或错误约束）的 `q`；  
2. 最终输出分布 ≠ 用户请求的 min_p / bias 分布；  
3. 更糟时静默错误（看起来在跑，结果不对）。

因此引擎选择 **请求级硬失败**（`ValueError`），而不是装上半截处理器。自定义 logits processor 在 spec 下同样直接拒绝（`STR_SPEC_DEC_REJECTS_LOGITSPROCS`）。

> 旁注：rejection sampler 注释还提到 draft verify 阶段对 top_p/top_k 的支持与 bonus token 采样不同——那是另一条限制；与本次「禁 min_p/logit_bias」直接相关的是 **processor 未装 + 无 draft 展开 apply**。

### 对本系统

| 点 | 借鉴 |
|----|------|
| `n` | 多候选默认用独立并行采样即可；若要真 beam，需单独状态机与 API，勿与 `n` 混名；**结果集合与 beam 不对齐**（见上） |
| min_p / logit_bias × spec | 任何「改 target 分布」的约束，必须进入 **verify 展平行** 的同一 apply 路径；做不到就明确拒绝，禁止静默忽略 |

---

## 深挖 3：penalty 与 device 空泡（含 vLLM V2 + 上游讨论）

三个参数：`presence_penalty` / `frequency_penalty` / `repetition_penalty`（均依赖已生成 token 历史）。共性：上一步采样结果必须进入 penalty 状态；apply 在 **forward 之后、sample 之前**。不会像 grammar 那样强制关 overlap，但可引入「等上一轮 token」或「每步重建计数」开销。

### SGLang（overlap 友好，仍有边界）

| 环节 | 行为 | 空泡风险 |
|------|------|----------|
| 累计 | `cumulate_output_tokens`：host last token → `non_blocking` H2D → GPU `scatter`/`scatter_add` | H2D 异步；前提是 `Req.output_ids` 已有上步 token |
| overlap apply | `update_penalties` → `acc_additive` / `acc_scaling`（`[B,V]` GPU），sample 时只 `add_` / 缩放 | apply **纯 GPU** |
| 上步 token | overlap 仍经 `process_batch_result`（`copy_done.synchronize`）再 `prepare_for_decode` | 与 grammar 同类边界，**不**单独关 overlap |
| 成本 | 每步可能 `zeros([B,V])` | GPU **忙**而非空转；大 vocab 拉长 sample 前准备 |

Spec 用「放松版」：对已累计 buffer `repeat_interleave`（见 `eagle_utils` / dflash 路径）。无 penalty 时 `is_required=False` 整段跳过。

### vLLM V1 + async（硬 sync）

`v1/sample/ops/penalties.py::apply_all_penalties`：每步 Python `list[list[int]]` → pad → `non_blocking` H2D → 全量 bincount；注释自评 *quite inefficient*。

async 下 `_sample` 前调用 `InputBatch.update_async_output_token_ids`：对 placeholder `-1` 填真实 token 时执行 **`async_copy_ready_event.synchronize()`**——典型 device 空泡。async+spec+penalty 还强制 draft token D2H（[#30495](https://github.com/vllm-project/vllm/pull/30495)）。

### vLLM Model Runner V2（目标形态）

源码：`vllm/v1/worker/gpu/sample/penalties.py` + `input_batch.py::post_update`。

**设备侧持久统计**，不再每步从 CPU 重建整段 output history：

| 状态 | 形状 / 位置 | 作用 |
|------|-------------|------|
| `prompt_bin_mask` | `[max_reqs, ceil(V/32)]` int32 GPU | prompt 出现过的 token（packed） |
| `output_bin_counts` | `[max_reqs, V]` int32 GPU | 已生成频次 |
| 三个 penalty 标量 | `UvaBackedTensor` | presence / frequency / repetition |

流程：

1. **入队**：`add_request` 记系数；`apply_staged_writes` 用 Triton `bincount` 从 `RequestState.all_token_ids`（GPU）建 mask/counts；`index_fill_` 清零时注释 *Avoid sync*。  
2. **sample 前**：`_penalties_kernel` 读 counts 改 logits；spec 时在 kernel 内用 `input_ids` + `expanded_local_pos` **临时**叠 draft 计数（不写回 persistent，避免错误接受污染）。  
3. **sample 后**：`post_update` 在 GPU 上给 `output_bin_counts` +1 并更新 `all_token_ids`——**无 D2H、无 Python list pad**。

代价：`output_bin_counts` 很大（源码 TODO：可能占 **数 GB** GPU 显存）。相对 V1，把「等 CPU 历史」空泡路径基本拆掉。

### 空泡对照

| | V1 + async | V2 | SGLang overlap |
|--|------------|-----|----------------|
| 历史依赖 | CPU list + **硬 sync** | GPU 持久 counts + `post_update` | GPU scatter 累计 |
| 每步成本 | O(rows × max_out_len) CPU | Triton apply + 小更新 | `[B,V]` 准备仍重 |
| 上游态度 | 已知痛点；[#47540](https://github.com/vllm-project/vllm/pull/47540) 拟回灌 V2 模型 | 目标形态 | 修 spec 正确性 + 减 Python 热路径 |

### 上游 Issue / PR（调研快照）

**vLLM**（讨论集中在 async 正确性 → 性能重构）：

| 链接 | 要点 |
|------|------|
| [#23569](https://github.com/vllm-project/vllm/pull/23569) Fully overlap model execution | async 落地；评论出现 frequency_penalty + async → CUDA scatter assert（`-1` placeholder） |
| [#27878](https://github.com/vllm-project/vllm/issues/27878) | 0.11.0 async 下 penalty **不生效**（`output_token_ids` 空） |
| [#26467](https://github.com/vllm-project/vllm/pull/26467) | BugFix：penalty/bad_words 兼容 async；*minimally-invasive*，计划大幅重构 |
| [#27910](https://github.com/vllm-project/vllm/pull/27910) | 混 batch（部分有 penalty）时 #26467 仍坏 |
| [#30495](https://github.com/vllm-project/vllm/pull/30495) | async **+ spec** 支持 penalty/bad_words（draft D2H）——正确性换同步 |
| [#29699](https://github.com/vllm-project/vllm/pull/29699) | V2：bin counts + UVA（设计入口；能力已在树内） |
| [#40657](https://github.com/vllm-project/vllm/pull/40657) | V2 Triton 性能/编译（`tl.range`、避免 layout 转换） |
| [#47540](https://github.com/vllm-project/vllm/pull/47540) OPEN | V2 式持久统计**回灌 V1**；量化 4k history **6ms/step** → 28k **47ms/step**，并点名 async `synchronize()`；DP 下最长序列拖死整 wave |

**SGLang**（偏正确性 + Python 热路径，少谈 async event sync）：

| 链接 | 要点 |
|------|------|
| [#5703](https://github.com/sgl-project/sglang/pull/5703) | 加入 repetition penalty |
| [#26011](https://github.com/sgl-project/sglang/issues/26011) / [#26319](https://github.com/sgl-project/sglang/pull/26319)、[#26027](https://github.com/sgl-project/sglang/pull/26027) | 热路径遍历未激活 penalizer → Python 开销（open perf PR） |
| [#28179](https://github.com/sgl-project/sglang/issues/28179) / [#28181](https://github.com/sgl-project/sglang/pull/28181) | `repetition_penalty` 曾被忽略（缺 `BatchedRepetitionPenalizer`） |
| [#28180](https://github.com/sgl-project/sglang/issues/28180) 及 [#28200](https://github.com/sgl-project/sglang/pull/28200)/[#28242](https://github.com/sgl-project/sglang/pull/28242)/[#28535](https://github.com/sgl-project/sglang/pull/28535) | DFlash/spec-v2 下 repetition_penalty 未 cumulate → **死循环** |

社区叙事：vLLM 先修 async 下「不生效/crash」，再承认 V1 每步重建太慢、用 V2 持久计数消空泡；SGLang 已是 GPU 累计 buffer（更近 V2），讨论焦点在 **spec 漏更新** 与 **orchestrator Python 开销**。

### 对本系统

| 点 | 借鉴 |
|----|------|
| 统计落点 | **常驻 device**（bin counts / scatter 累计），禁止每步 list→pad→H2D |
| async/overlap | sample 前避免强制 `event.synchronize()`；token 历史更新与 forward 重叠或纯 GPU `post_update` |
| 显存 | `[max_reqs, vocab]` int32 要进容量规划（V2 TODO） |
| 无 penalty 请求 | 整段跳过（`is_required` / `use_penalty`），勿为混 batch 误 sync |

---

## 深挖 4：采样状态归属 · Spec 兼容矩阵 · `n` 与前缀共享

### 4.1 采样状态归属（PD / 混部 / D-direct）

**原则：KV 是「前缀张量」；采样状态是「生成游标」——二者生命周期不同，不要默认一起搬。**

| 状态 | 典型载体 | 是否随 KV 池块走 |
|------|----------|------------------|
| 前缀 / 已写回的 KV blocks | 存储池 L0–L3 | ✓（池权威） |
| `output_ids` / 已采样 token 序列 | 请求对象（Decode worker） | ✗ |
| presence / frequency / repetition 计数 | GPU buffer 或 CPU list（跟 batch/req） | ✗ |
| grammar FSM / bitmask 游标 | CPU matcher（挂在 req） | ✗ |
| stop / min_tokens / finish 判定 | 调度 + sampling metadata | ✗ |
| RNG / seed 游标 | sampler 侧 | ✗ |
| logprobs 累积、custom logit processor 句柄 | 请求级 | ✗ |

参考实现里的落点：

1. **SGLang PD**：Prefill 实例在 prefill 完成时**会 sample 出 first token**，写入 `req.output_ids`，并可选 `grammar.accept_token`；随后 `send_kv_chunk` 只推 KV。Decode 侧 `process_prebuilt` 用已缓冲的 last token 重建 `SamplingBatchInfo`，并对 grammar 再 `accept_token`（若尚未 accept）。见 `disaggregation/prefill.py::process_batch_result_disagg_prefill`、`decode_schedule_batch_mixin.py::process_prebuilt`。  
2. **vLLM PD**：引擎经 KV connector 搬 block；sampling / structured-output 状态留在继续 decode 的 engine 请求上（proxy 把同一逻辑请求从 P 转到 D）。  
3. **混部**：P+D 同进程，状态本就在同一 `Req` / `InputBatch`，无跨机交接。  
4. **D-direct**：前缀 KV 已在执行节点 HBM；仍须在该节点**新建或恢复**采样游标（空 output、penalty 零计数、grammar 初始态）。本地命中省的是 KV 传输，不是采样状态。

对 lake：池只权威 KV 位置；Router 选 D 节点后，采样状态在该 Decode 执行上下文初始化/续跑。故障恢复（F4）重路由时——KV 可从 L2/L3 拉回，**penalty / grammar / stop 游标必须按请求元数据重建或随控制面请求状态迁移**，不能指望「拉 KV 就恢复采样」。

### 4.2 Spec × 采样兼容矩阵

下表以当前 submodule 行为为准（会漂移；以源码校验函数为准）。符号：✓ 支持 · △ 支持但有代价/缺口 · ✗ 硬拒绝或不装路径 · — 未作为一等组合强调。

| 能力 | vLLM + spec | SGLang + spec | 备注 |
|------|-------------|---------------|------|
| temperature / greedy | ✓ | ✓ | rejection 用 target 分布；bonus 可走完整 sampler |
| top_p / top_k | △ | ✓（主路径） | vLLM：docstring 写明 **verify 链上**对 top_p/k 支持弱于 bonus（`RejectionSampler`）；bonus 走普通 `Sampler` |
| min_p | ✗ | ✓（无对等硬禁） | vLLM `_validate_spec_decode`；见深挖 2 |
| logit_bias | ✗ | 视 custom/字段 | vLLM 同上；自定义 logitsprocs 亦 `STR_SPEC_DEC_REJECTS_LOGITSPROCS` |
| presence / frequency / repetition | △ | △ | vLLM：rejection 内 `repeat_interleave` + `apply_all_penalties`；async 下有 sync/D2H（深挖 3）。SGLang：GPU 累计 + `repeat_interleave`；曾出 spec 漏 cumulate 死循环（#28180） |
| bad_words / allowed_token_ids | △ | 近似（custom / 引擎路径） | vLLM：`apply_bad_words_with_drafts` / mask `repeat_interleave` |
| min_tokens | ✓ | ✓ | vLLM：`MinTokensLogitsProcessor.apply_with_spec_decode`（spec 时**唯一**保留的内置 processor） |
| structured / grammar | △ | △ | 两边都能跑，但 **async/overlap 代价大**：SGLang `need_grammar_sync` 关 overlap；vLLM draft 需回 scheduler 做 grammar 校验（`spec_decode/utils.py` D2H）——见 [guided-decoding.md](guided-decoding.md) |
| logprobs | ✓ | ✓ | 额外 gather；不改变 accept 语义，但增带宽/同步 |
| custom logits processor | ✗（vLLM 硬拒） | △ | 必须进 verify 展平行，否则分布不一致 |
| `n` 并行采样 | ✓ | ✓ | 与 spec 正交：先展开再各自 draft/verify |
| beam search | ✗ / 独立路径 | 主路径无 | 勿与 spec 混用假设 |

**读表要点：**

- 「能开」≠「无空泡」。grammar + spec、penalty + async 是正确性已接、性能仍痛的典型格。  
- 任何改 target 分布的算子：要么进入 **draft 展平行** 的同一 apply，要么请求级拒绝（深挖 2 的原则）。  
- lake 若做 spec：先固定一张「支持 / 拒绝 / 降级」表写进接口契约，避免半截 processor。

### 4.3 `n` 与前缀 KV 共享

`n` 在入口把同一 prompt **复制成 n 条独立请求**（深挖 1），但**前缀 token 相同 ⇒ 内容寻址 / radix 前缀可命中同一物理 KV**。

```text
prompt P
    ├─ req_1 (sample path A) ──┐
    ├─ req_2 (sample path B) ──┼─ 共享 prefix blocks（ref++）
    └─ req_n (sample path C) ──┘
         │
         └─ 首个分叉 token 之后：各写各的 decode KV，采样状态始终独立
```

| 层 | 共享？ | 说明 |
|----|--------|------|
| 前缀 KV blocks | ✓（可） | SGLang `RadixCache.match_prefix` / `insert`：相同 `input_ids` 命中同一树节点；`req_to_token` 每请求一份映射，物理页可 ref-count 共享。vLLM prefix caching 同理（block hash） |
| Prefill 计算 | △ | 理想可一次 prefill、n 路复用；实现上常是 n 条调度项各自 match——**命中后跳过重复 prefill**，而非先合并再扇出（取决于调度是否同批） |
| Decode KV | ✗（分叉后） | 各序列 `output_ids` 不同，后续 block 独立分配 |
| 采样状态 | ✗ | 各自 temperature / seed / penalty / stop / grammar |
| Beam 式联合剪枝 | ✗ | `n` 不做跨序列比较（深挖 1） |

对 lake：

| 点 | 含义 |
|----|------|
| 池放置 | `n` 条共享同一 `model_id` 前缀键时，**预放置一次**到目标 HBM，Router 可对 n 路同选 D-direct / 同 Decode 节点 |
| 计费 / 配额 | 前缀块按引用计数，勿按 n 倍全量计 KV 容量 |
| 调度 | 分叉后 n 路 decode 负载 ≈ n 条独立短请求；前缀命中只省 prefill/传输 |
| 与 beam 对比 | beam 在搜索树内共享前缀并剪枝；`n` 是「共享前缀 KV + 独立采样」，语义仍是独立样本 |

---

## 命名与挂载差异速查

1. `max_tokens` ↔ `max_new_tokens`；`seed` ↔ `sampling_seed`；`include_stop_str_in_output` ↔ `no_stop_trim`。  
2. greedy：vLLM `temperature≈0`；SGLang 强制 `top_k=1`。  
3. logprobs：vLLM 在 `SamplingParams`；SGLang 在 `GenerateReqInput`。  
4. 白/黑名单与 thinking 预算：vLLM 一等字段；SGLang 多用 `custom_logit_processor`。  
5. beam：vLLM 独立 `BeamSearchParams`；SGLang 主路径只有 `n` 并行独立采样。  
6. 采样状态跟 Decode 请求走，不进 KV 池；`n` 可共享前缀 KV、不共享采样游标（深挖 4）。

## 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| vLLM SamplingParams | `vllm/sampling_params.py`::`SamplingParams` / `StructuredOutputsParams` / `BeamSearchParams` |
| vLLM beam 打分 / 剪枝 | `vllm/entrypoints/generate/beam_search/utils.py`::`get_beam_search_score`；`offline.py` / `online.py`::`_beam_search_step` |
| vLLM spec 禁 min_p/logit_bias | `sampling_params.py`::`_validate_spec_decode` |
| vLLM spec 时裁剪 logitsprocs | `vllm/v1/sample/logits_processor/__init__.py`::`build_logitsprocs` |
| vLLM MinP / LogitBias | `vllm/v1/sample/logits_processor/builtin.py`::`MinPLogitsProcessor` / `LogitBiasLogitsProcessor` |
| vLLM rejection + 部分约束 | `vllm/v1/sample/rejection_sampler.py`::`RejectionSampler`（`apply_logits_processors` / `apply_penalties` / `apply_bad_words_with_drafts`） |
| vLLM V1 penalty（每步 CPU rebuild） | `vllm/v1/sample/ops/penalties.py`::`apply_all_penalties` |
| vLLM async 填 placeholder | `vllm/v1/worker/gpu_input_batch.py`::`update_async_output_token_ids`（`async_copy_ready_event.synchronize`） |
| vLLM V2 PenaltiesState | `vllm/v1/worker/gpu/sample/penalties.py`::`PenaltiesState` / `_penalties_kernel` / `bincount` |
| vLLM V2 采样后更新 counts | `vllm/v1/worker/gpu/input_batch.py`::`post_update` / `_post_update_kernel` |
| vLLM V2 sampler 挂载 | `vllm/v1/worker/gpu/sample/sampler.py`::`apply_sampling_params` |
| vLLM spec + structured draft 回传 | `vllm/v1/worker/gpu/spec_decode/utils.py`（draft tokens → scheduler grammar） |
| SGLang SamplingParams | `python/sglang/srt/sampling/sampling_params.py`::`SamplingParams` |
| SGLang n 展开 | `python/sglang/srt/managers/io_struct.py`::`GenerateReqInput._handle_parallel_sampling` / `_expand_inputs` |
| SGLang PD prefill 首 token + grammar | `disaggregation/prefill.py`::`process_batch_result_disagg_prefill` |
| SGLang PD decode 重建 sampling | `disaggregation/decode_schedule_batch_mixin.py`::`process_prebuilt` |
| SGLang radix 前缀命中 | `mem_cache/radix_cache.py`::`match_prefix` / `insert` / `cache_unfinished_req` |
| SGLang penalizer | `python/sglang/srt/sampling/penaltylib/`::`BatchedPenalizerOrchestrator` / `BatchedFrequencyPenalizer` / `BatchedPresencePenalizer` / `BatchedRepetitionPenalizer` |
| SGLang overlap 预聚 penalty | `sampling_batch_info.py`::`update_penalties` / `copy_for_forward`；`schedule_batch.py`::`cumulate_penalty_output_tokens` |
| SGLang overlap×spec×grammar | `managers/scheduler.py`::`need_grammar_sync` / `is_disable_overlap_for_batch` |
| SGLang 请求级 logprob / custom processor | `io_struct.py`::`GenerateReqInput`（`return_logprob` / `custom_logit_processor`） |
| SGLang custom processor 示例 | `python/sglang/srt/sampling/custom_logit_processor.py` |
| 官方参数表 | SGLang `docs_new/docs/basic_usage/sampling_params.mdx` |
