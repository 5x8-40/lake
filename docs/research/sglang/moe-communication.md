# SGLang — MoE 路由与跨 rank 通信模式

> 源码：`3rdparty/sglang`（路径前缀默认 `python/sglang/srt/`）。本篇只讨论 MoE 的**执行期 token 路由与 EP 通信**；DP/TP 的调度与 collective 对齐见 [model-runner.md](model-runner.md)，attention/MoE 的 batch 几何仍由 `ForwardBatch` 提供。

## 一句话

SGLang 的 MoE 选择有两条正交轴：

1. **本地路由**：router 对每个 token 选 top-k experts、产生 dispatch metadata；
2. **跨 rank dispatch/combine**：EP 把 token 发到 expert 所在 rank 再回收，按 backend 与 `DeepEPMode` 选择实现。

因此“MoE 用什么模式”不能只看模型或 `ForwardMode`。`--moe-a2a-backend` 决定 dispatcher，`--deepep-mode` 决定 DeepEP normal/low-latency 策略；后者的 `auto` 再读取本 step 的 `is_extend_in_batch`。

## 两轴与配置层次

| 层次 | 决定什么 | 例子 |
|------|----------|------|
| 模型 MoE router | token → top-k expert ids / weights | 模型层的 routed experts、shared experts |
| dispatcher backend | 跨 EP rank 如何 dispatch/combine | DeepEP、Mooncake、NIXL、MoriEP 等 `token_dispatcher` |
| DeepEP mode | 同一 DeepEP dispatcher 的通信策略 | `normal`、`low_latency`、`auto` |
| 本步执行几何 | `auto` 最终如何解析 | eager extend、普通 decode、decode graph capture |

`layers/moe/utils.py::DeepEPMode.resolve` 的规则很直接：

```text
mode != auto                 → 使用显式 normal / low_latency
mode == auto && is_extend    → normal（吞吐优先）
mode == auto && !is_extend   → low_latency（单步时延优先）
```

`token_dispatcher/deepep.py::DeepEPDispatcher.dispatch` 读取 `get_is_extend_in_batch()` 后执行该解析；不同 dispatcher（Mooncake/NIXL/MoriEP）也沿用这类输入/模式分支。选 expert 的算法与这条通信模式选择相互独立。

## speculative target verify 的特殊性

在 **eager** 路径，`ForwardMode.TARGET_VERIFY.is_extend()` 为真。因此 `deepep-mode=auto` 通常解析成 `normal`：target 一次验证 K 个 draft token，SGLang 把它当作可摊薄通信启动成本的吞吐型工作。

但 target verify **可以**走 decode CUDA graph。`eagle_utils.py::eagle_prepare_for_verify` 直接检查并装载 `decode_cuda_graph_runner`；该 runner capture/replay 时把 `is_extend_in_batch=False` 写入 forward context，并让 `DeepEPCudaGraphRunnerAdapter` 以该值 capture。于是同一类 target verify 在 graph 路径可能使用 low-latency DeepEP 形态。

这不是矛盾，而是 SGLang 以“当前实际 runner/capture contract”覆盖枚举的通用分类：

| 路径 | `auto` 的有效信号 | 典型目标 |
|------|-------------------|----------|
| eager `TARGET_VERIFY` | `is_extend_in_batch=True` | K-token verify 的吞吐 |
| decode graph `TARGET_VERIFY` | capture 时 `False` | 固定 shape replay、低启动时延 |
| 普通 eager `DECODE` | `False` | 单 token ITL |
| prefill / `MIXED` prefill graph | 由 prefill runner / batch 几何决定 | prefill 吞吐 |

所以不能从“有 draft token”推出一定走吞吐模式，也不能从 `TARGET_VERIFY` 的枚举名推出一定走 prefill graph。

## 对 lake 的含义

- lake 可以把普通 decode 与 spec verify 统一为 q_len 几何接口，但应另提供**纯派生的通信计划**，至少输入 `(q_len、batch token 数、graph/eager、EP/TP 配置、dispatcher capability)`；不能把存储池模式或请求生命周期混进 MoE 选择。
- 若初期没有 EP/MoE，`SchedulerOutput` 只保留足够的 batch 几何与并行标签；未来 executor 在每个 rank 使用同一个派生结果，避免 collective 分歧。
- SGLang 的 `auto` 是单机执行后端策略，不是路由器的集群选点策略。lake Router 仍只负责三种执行模式与节点选择；MoE A2A 由计算层 executor 决定。
- 性能校准应分别记录 eager/graph 的 verify token 数、dispatch bytes、normal/low-latency 时延与接受率。不能用“prefill/decode”二元标签替代这些数据。

## 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| DeepEP mode 枚举与 `auto` 解析 | `layers/moe/utils.py::DeepEPMode.resolve` |
| DeepEP dispatcher 运行时分支 | `layers/moe/token_dispatcher/deepep.py::DeepEPDispatcher.dispatch` |
| 其他 EP dispatcher 的同类解析 | `layers/moe/token_dispatcher/{mooncake,nixl,moriep}.py` |
| batch 执行分类 | `model_executor/forward_batch_info.py::ForwardMode.is_extend` |
| DP/forward context 信号 | `layers/dp_attention.py::set_is_extend_in_batch` / `get_is_extend_in_batch` |
| target verify 选择 decode graph | `speculative/eagle_utils.py::eagle_prepare_for_verify` |
| decode graph 的 DeepEP capture | `model_executor/runner/decode_cuda_graph_runner.py::DecodeCudaGraphRunner` / `runner_utils/deepep_adapter.py::DeepEPCudaGraphRunnerAdapter` |
