# 2026-09-18 E1.3 胶水层兼容性摸底

> 结论已被 E1.4/E1.5 实测覆盖(2026-09-18 验通)。本文只留结论与复验工具。

## 结论

- `dynamo.vllm` 对 vLLM 0.26 的 import 面(61 项)无缺口:静态命中 59,未命中 2 项均有双写兜底。
- 生产胶水层**无 CUDA 符号引用**(`torch.cuda`/`pynvml`/`CUDA_VISIBLE_DEVICES` 零使用;CUDA 命名 import 全在 tests/)→ fork 在 E1 阶段零代码改动,已实测成立。
- `ai-dynamo` 的 [vllm] extra 是 CUDA 生态(nixl[cu13]、flashinfer),任何场景禁止安装;NIXL 留到 K1 源码编译 + Mooncake TE 后端。

## 复验探针(vllm-ascend 升级后重跑)

容器内先 `import torch_npu`(完成平台注册,vllm-ascend 插件随之生效)再跑;ALL GREEN 即兼容。

```python
import importlib, sys
CHECKS = {
 "vllm": ["LLM","PoolingParams","SamplingParams"],
 "vllm.config": ["CUDAGraphMode","ECTransferConfig","ModelConfig","VllmConfig"],
 "vllm.distributed.kv_events": ["KVEventsConfig","ZmqEventPublisher"],
 "vllm.distributed.kv_transfer.kv_connector.v1.mooncake.mooncake_connector": [],
 "vllm.distributed.kv_transfer.kv_connector.v1.multi_connector": [],
 "vllm.distributed.ec_transfer.ec_connector.base": [],
 "vllm.engine.arg_utils": ["AsyncEngineArgs"],
 "vllm.entrypoints.cli.serve": ["run_headless"],
 "vllm.entrypoints.openai.models.protocol": ["BaseModelPath"],
 "vllm.entrypoints.openai.models.serving": ["OpenAIServingModels"],
 "vllm.inputs": ["EmbedsPrompt","TextPrompt","TokensPrompt"],
 "vllm.logprobs": ["PromptLogprobs"],
 "vllm.lora.request": ["LoRARequest"],
 "vllm.outputs": ["CompletionOutput","RequestOutput"],
 "vllm.platforms": ["current_platform"],
 "vllm.renderers": ["TokenizeParams"],
 "vllm.renderers.base": ["BaseRenderer"],
 "vllm.renderers.embed_utils": ["safe_load_prompt_embeds"],
 "vllm.sampling_params": ["RequestOutputKind","SamplingParams","StructuredOutputsParams"],
 "vllm.transformers_utils.repo_utils": ["get_model_path"],
 "vllm.usage.usage_lib": ["UsageContext"],
 "vllm.utils.argparse_utils": ["FlexibleArgumentParser"],
 "vllm.utils.hashing": ["get_hash_fn_by_name"],
 "vllm.utils.system_utils": ["update_environment_variables"],
 "vllm.v1.core.kv_cache_manager": ["KVCacheManager"],
 "vllm.v1.core.kv_cache_utils": ["get_request_block_hasher","init_none_hash"],
 "vllm.v1.core.sched.async_scheduler": ["AsyncScheduler"],
 "vllm.v1.core.sched.output": ["CachedRequestData","NewRequestData","SchedulerOutput"],
 "vllm.v1.core.single_type_kv_cache_manager": ["CrossAttentionManager"],
 "vllm.v1.engine.async_llm": ["AsyncLLM"],
 "vllm.v1.engine.exceptions": ["EngineDeadError"],
 "vllm.v1.kv_cache_interface": ["KVCacheConfig"],
 "vllm.v1.metrics.loggers": ["StatLoggerBase"],
 "vllm.v1.metrics.prometheus": ["setup_multiprocess_prometheus"],
 "vllm.v1.metrics.stats": ["IterationStats","SchedulerStats","RequestStateStats"],
 "vllm.v1.outputs": ["ModelRunnerOutput"],
 "vllm.v1.request": ["Request","RequestStatus"],
 "vllm.v1.structured_output": ["StructuredOutputManager"],
}
fail = 0
for mod, syms in CHECKS.items():
    try:
        m = importlib.import_module(mod)
    except Exception as e:
        print(f"[FAIL] import {mod}: {e}"); fail += 1; continue
    for s in syms:
        if not hasattr(m, s):
            print(f"[FAIL] {mod}.{s} 不存在"); fail += 1
print("ALL GREEN" if fail == 0 else f"{fail} 项失败")
sys.exit(1 if fail else 0)
```
