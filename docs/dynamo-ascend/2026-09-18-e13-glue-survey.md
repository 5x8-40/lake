# 2026-09-18 E1.3 胶水层兼容性摸底(静态阶段)

> E1.3 分两步:① 静态走查(本文,不需要 NPU);② 目标镜像内探针终验(待 E1.1 镜像就绪后在容器里跑末节脚本,全绿则 E1.3 完成)。

## 对齐基线

- Dynamo 侧:`f5802d355b`(main 上 vllm 0.26.0 pin 的最后一个状态,即 0.27.1 bump #13059 的父提交;≈ ai-dynamo 1.4.x 发布态,import 面在 1.4.0–1.4.2 间无差异)。
- vLLM 侧:本地 `3rdparty/vllm` 检出 2026-07-16(0.26 开发期,近似;**最终以容器内 vLLM 0.26.0 为准**)。

## import 面总览与静态命中

`dynamo.vllm` 对 vLLM 的 import 共 61 项(去重后),静态命中 **59 项**:

| 分类 | 项数 | 命中 | 说明 |
|------|------|------|------|
| 公开/半公开 API(LLM、SamplingParams、AsyncEngineArgs、outputs、inputs 等) | ~20 | 全中 | 跨版本稳定 |
| `vllm.v1.*` 内部接口(AsyncScheduler、SchedulerOutput、KVCacheManager、kv_cache_utils 等) | ~20 | 全中 | 版本敏感区,本次 0.26 对齐无缺口 |
| KV 事件与 connector(kv_events、mooncake_connector、multi_connector、ec_transfer) | 5 | 全中 | mooncake_connector 在 vLLM 0.26 树内,K 线可直接用 |
| 多模态 / vllm_omni | ~10 | 全中 | omni 是独立包,bring-up 不装、不用 |
| 平台与 CUDA 命名(CUDAGraphMode、current_platform、CpuPlatform) | 3 | 全中 | **只出现在 tests/ 里,生产胶水层无 CUDA 符号引用** |

未命中 2 项,均为 dynamo 双写兜底中的一处,另一处命中:

- `vllm.multimodal.inputs.MultiModalUUIDDict` —— `vllm.inputs.MultiModalUUIDDict` 命中(多模态路径,bring-up 不用);
- `vllm.utils.FlexibleArgumentParser` —— `vllm.utils.argparse_utils.FlexibleArgumentParser` 命中(参数解析,必用路径有兜底)。

## 结论(静态阶段)

1. **import 面在 vLLM 0.26 对齐下基本无缺口**——版本对齐策略(ai-dynamo 1.4.x ↔ vllm-ascend v0.26.0rc1)成立。
2. **生产胶水层无 CUDA 符号引用**;CUDA 命名 import 全部在测试代码。运行时风险不在 import,而在行为层:vllm-ascend 的 platform patch 对 `SchedulerOutput` 等结构语义的影响、KV 事件发布路径,需容器内实测。
3. `[vllm]` extra 的 CUDA 依赖(`nixl[cu13]==1.3.2`、flashinfer)不装;NIXL 留到 K1 源码编译 + Mooncake TE 后端。

## 硬件假设扫描(2026-09-18 补,回答"fork 有没有代码要改")

对生产代码(排除 tests 与 trtllm/sglang/omni 后端)全量扫 `torch.cuda` / `pynvml` / `CUDA_VISIBLE_DEVICES` / `nvtx` / cudagraph:

| 检查项 | 结果 |
|--------|------|
| `torch.cuda` / `pynvml` / `CUDA_VISIBLE_DEVICES` | **零使用** |
| `CUDAGraphMode` / `current_platform` / `CpuPlatform` | 只在 `tests/`(测试用 CpuPlatform 兜底无卡主机) |
| `nvtx_utils`(handlers.py) | 默认关闭(`DYN_NVTX=0`,零开销),不开即空操作 |
| `instrumented_scheduler` 的 cudagraph 读取 | 全是 `getattr(…, "NONE")` 防御式读法,字段缺失不炸;且属 benchmark 模式 |
| `backend_args.py` 的 "CUDA graph" 字样 | 仅 help 文本 |

**结论:E1 bring-up 阶段 fork 大概率零代码改动**,要新增的是部署物(Dockerfile:FROM vllm-ascend 镜像 + `pip install ai-dynamo==1.4.2`;启动脚本/环境变量模板)。可能的补丁点(KV 事件 publisher 配置、健康检查与 vllm-ascend platform 插件的交互)只能在 E1.4 实测暴露。

## 容器内终验探针(E1.3 完成判据:全绿)

在 vllm-ascend 容器内执行(覆盖了生产路径全部 import 项;omni/多模态按需):

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

注意:在容器里跑前需 `import torch_npu` 完成平台注册(vllm-ascend 插件随之生效),再跑探针,才能反映真实生效环境。
