# E 引擎线:任务与进展追踪

> E 线 = 让请求在昇腾上跑起来(引擎接入)。本文档只作任务追踪;环境与操作手册见 [native-bringup.md](native-bringup.md)。
>
> 环境:Atlas A2(8 卡)/ openEuler / Kunpeng 920(aarch64);镜像 `vllm-ascend:v0.26.0rc1`(Ubuntu 变体);Dynamo 宿主机源码编译 `ascend-dev` + `.pth` 注入容器。

## 任务表

| 里程碑 | 任务 | 说明 | 状态 |
|--------|------|------|------|
| **E1 单机聚合跑通**(2026-09-18 完成) | E1.1 基础镜像 | `vllm serve` 单独验证引擎出 token | 完成(Qwen3.8-27B,DP2×TP4) |
| | E1.2 Dynamo 装入 NPU 环境 | 宿主机源码编译 + `.pth` 注入 | 完成 |
| | E1.3 胶水层兼容性摸底 | import 面走查 + 容器探针,结论:零代码改动 | 完成(见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md)) |
| | E1.4 拉起 worker | frontend + `dynamo.vllm` 同容器,注册进 etcd | 完成 |
| | E1.5 全链路连通 | `/v1/models` 注册 `qwen`,chat 出 token | 完成 |
| **E1.6 KV 事件链** | E1.6 KV-aware 选路补验 | 已验通路线未开 KV 事件;补验方式见下节 | 未开始 |
| 增强 | EX1 SGLang NPU 第二后端 | SGLang 主干自带 NPU 支持,vllm-ascend 路线跑通后接入(远期) | 未开始 |
| 增强 | EX2 ModelExpress 权重加速 | NPU 间流式传权重;前期共享存储兜底(远期) | 未开始 |

## E1.6 说明(KV 事件链)

E1 验通路线的 worker/frontend 均未开 KV 事件,Router 实际是 round-robin。补验时改 `scripts/ascend/start_dynamo_va_native.sh`:

- worker 加 `--kv-events-config '{"enable_kv_cache_events": true}'`(publisher 默认 zmq、endpoint 默认 `tcp://*:5557`,DP 各 rank 端口自动偏移);
- frontend 加 `--router-mode kv`(默认 round-robin 不消费事件);
- 验证:同一长前缀请求发两次,第二次应命中前缀 KV(TTFT 明显下降)。

另:MTP 投机解码与 cudagraph 配置(E1.1 裸引擎用过)在 dynamo 路径下未验证,补验时一并带回。

## 进展日志

| 日期 | 事项 |
|------|------|
| 2026-09-17 | E 线任务拆分定稿 |
| 2026-09-18 | E1 完成:Ubuntu 镜像定稿;`vllm serve` 出 token;胶水层探针通过;源码编译路线跑通 frontend + worker + etcd,chat 出 token(910B3) |
| 2026-09-20 | 文档重组:runbook 与脚本定稿于本工作区([native-bringup.md](native-bringup.md) + [scripts/ascend/](scripts/ascend/)),dynamo-ascend 仓只留代码改动;新增 E1.6 |
