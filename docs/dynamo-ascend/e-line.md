# E 引擎线:任务与进展追踪

> E 线 = 让请求在昇腾上跑起来(引擎接入)。本文档是 E 线的任务表与进展日志,逐任务更新状态;专题结论按日期单独成文并在日志中链接。

## 环境基线(2026-09-18 核实,详见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md))

- 硬件:Atlas A2 / openEuler / Kunpeng 920(aarch64)
- 镜像:`quay.io/ascend/vllm-ascend:v0.26.0rc1-openeuler`(vLLM 0.26.0 + CANN 9.1.0 + torch_npu 2.10.0.post4,Mooncake 0.3.11.post1 已在镜像内)
- Dynamo:**ai-dynamo==1.4.0**(其 [vllm] extra pin `vllm==0.26.0`,与镜像对齐;main 1.5.0 pin 0.28.0,直接用会顶掉镜像里的 vLLM)
- fork 基线:`ascend-dev` 从上游 v1.4.0 切出,与 vllm-ascend 锁步升级

## 任务表

| 里程碑 | 任务 | 说明 | 状态 |
|--------|------|------|------|
| **E1 单机聚合跑通**(验证:OpenAI 接口出 token,Router 看到注册与 KV 事件) | E1.1 基础镜像 | 拉取 `vllm-ascend:v0.26.0rc1-openeuler`;先 `vllm serve` 单独验证引擎出 token——不碰 Dynamo,先证引擎可用 | 未开始(需 NPU 机器) |
| | E1.2 Dynamo 装入 NPU 环境 | `pip install ai-dynamo==1.4.0`(不带 [vllm] extra);aarch64 runtime wheel 已确认存在(≥1.2.0) | 未开始 |
| | E1.3 胶水层兼容性摸底 | `dynamo.vllm` 对 vLLM 私有 API 的 import 链在 vllm-ascend/0.26 下静态走查,列 CUDA 假设点清单 | **进行中**(静态阶段完成:59/61 命中、生产面无 CUDA 符号,见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md);待容器内探针终验) |
| | E1.4 拉起 worker | 按 [D001](decisions/D001-overall-approach.md) 差异清单配环境变量与 torch_npu 初始化,`dynamo.vllm` 起 vllm-ascend,注册进 etcd | 未开始 |
| | E1.5 全链路连通 | Frontend → Router → worker 出 token | 未开始 |
| 增强 | EX1 SGLang NPU 第二后端 | SGLang 主干自带 NPU 支持(`hardware_backend/npu`),vllm-ascend 路线跑通后接入(远期) | 未开始 |
| 增强 | EX2 ModelExpress 权重加速 | NPU 间流式传权重;前期共享存储兜底(远期) | 未开始 |

## 进展日志

| 日期 | 事项 |
|------|------|
| 2026-09-17 | E 线任务拆分定稿(E1.1–E1.5 + 增强项) |
| 2026-09-18 | E 线环境核实完成:镜像 v0.26.0rc1-openeuler、ai-dynamo==1.4.0 版本对齐、版本陷阱确认,见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md);E 线开工 |
| 2026-09-18 | E1.3 静态阶段完成:import 面 59/61 命中(2 项为双写兜底),生产胶水层无 CUDA 符号;容器内探针终验待 E1.1 镜像就绪,见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md) |
