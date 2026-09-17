# D002: 组件边界——不动引擎内部的 Scheduler 与 BlockAllocator

- 日期:2026-09-17
- 状态:已定

## 背景

分布式推理系统里三个名字都带"调度"的组件职责完全不同,混为一谈会导致适配范围失控(讨论中实际发生过):

| 组件 | 层级 | 职责 | 昇腾侧现状 |
|------|------|------|-----------|
| **Router** | 集群级控制面 | 收外部请求,按全局 KV 命中/负载/拓扑决定发给哪个实例 | vLLM/SGLang 没有此组件;Dynamo Router 提供 |
| **Scheduler** | 单实例控制面 | 实例内请求队列状态机、continuous batching、抢占 | vllm-ascend 内部已成熟 |
| **BlockAllocator** | 单实例内存底层 | KV Block 分配/回收、PagedAttention 页表 | vllm-ascend 内部已成熟 |

## 决策

适配红线:**不魔改 vllm-ascend / SGLang 内部的 Scheduler 与 BlockAllocator**。

- 跨实例 KV 移动由控制面协调器决策"何时搬、从哪搬到哪",但**不直接操作**目标实例的 BlockAllocator——通过引擎暴露的 connector / kv_transfer 接口或 RPC 触发实例内部分配与接收。
- 集群级路由是新增组件(Router),不下沉进引擎。
- 与 lake 的职责边界原则同构(推理系统只管执行、越界宁可少做),也与此原则在 AIBrix/llm-d 调研中的印证一致。

## 后果

- 好处:引擎侧可跟随 vllm-ascend 上游升级,适配面最小;调度语义出问题时归属清晰。
- 代价:跨实例 KV 搬运的触发点受限于引擎 connector 接口的能力;若接口不足,优先向 vllm-ascend 上游提接口,而不是在 fork 里挖洞。
