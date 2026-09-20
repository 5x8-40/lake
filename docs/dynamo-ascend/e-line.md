# E 引擎线:任务与进展追踪

> E 线 = 让请求在昇腾上跑起来(引擎接入)。任务表 + 进展日志;运行手册见 [native-bringup.md](native-bringup.md)。

## 环境基线

- 硬件:Atlas A2 / openEuler / Kunpeng 920(aarch64),8 卡
- 镜像:`quay.io/ascend/vllm-ascend:v0.26.0rc1`(Ubuntu 变体;**不要用 openEuler 变体**,容器内 npu-smi 卡死)。镜像内容与挂载清单见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md)
- Dynamo:宿主机源码编译 fork `ascend-dev`(跟踪 main)+ `.pth` 注入容器,见 [native-bringup.md](native-bringup.md)(PyPI aarch64 wheel 在鲲鹏 Illegal instruction,不可用)

## 任务表

| 里程碑 | 任务 | 说明 | 状态 |
|--------|------|------|------|
| **E1 单机聚合跑通** | E1.1 基础镜像 | `vllm serve` 单独验证引擎出 token | **完成**(Qwen3.8-27B,DP2×TP4) |
| | E1.2 Dynamo 装入 NPU 环境 | 宿主机源码编译 + `.pth` 注入 | **完成**([native-bringup.md](native-bringup.md) §2) |
| | E1.3 胶水层兼容性摸底 | import 面走查 + 容器探针 | **完成**(见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md)) |
| | E1.4 拉起 worker | frontend + `dynamo.vllm` 同容器,注册进 etcd | **已验通**(2026-09-18,910B3;本机复现待做) |
| | E1.5 全链路连通 | `/v1/models` 注册,chat 出 token | **已验通**(同上) |
| | E1.6 KV 事件链补验 | worker `--kv-events-config` + frontend `--router-mode kv`;MTP/cudagraph 配置在 dynamo 路径亦未验 | 未开始 |
| 增强 | EX1 SGLang NPU 第二后端 | SGLang 主干自带 NPU 支持,vllm-ascend 路线跑通后接入(远期) | 未开始 |
| 增强 | EX2 ModelExpress 权重加速 | NPU 间流式传权重;前期共享存储兜底(远期) | 未开始 |

## 本机复现(最短路径)

```bash
export WM_ROOT=/data/wm
# 首次:宿主机源码编译 Dynamo,见 native-bringup.md §2
bash scripts/ascend/start_docker_va.sh        # 常驻容器
bash scripts/ascend/start_etcd.sh             # 常驻 etcd
bash scripts/ascend/start_dynamo_va_native.sh # 容器内 FE + worker,自动等注册
curl -s localhost:8000/v1/models              # 出现 qwen 即通
```

前置:本机若已有占满 NPU 的 `vllm serve` 先停掉;孤儿 `VLLM::*` 进程需 `pkill -9 -f 'VLLM::'`(脚本 `stop` 子命令已含)。

## E1.6 增量(KV 事件链)

已验通路线未开 KV 事件。补验时改 `scripts/ascend/start_dynamo_va_native.sh`:

- worker 加 `--kv-events-config '{"enable_kv_cache_events": true}'`(publisher 默认 zmq、endpoint 默认 `tcp://*:5557`,DP 各 rank 端口自动偏移);
- frontend 加 `--router-mode kv`(默认 round-robin 不消费事件);
- 验证:同一长前缀请求发两次,第二次应命中前缀 KV(TTFT 明显下降)。

## 进展日志

| 日期 | 事项 |
|------|------|
| 2026-09-17 | E 线任务拆分定稿(E1.1–E1.5 + 增强项) |
| 2026-09-18 | 环境定稿(Ubuntu 镜像);E1.1 出 token;E1.3 探针通过;E1.4/E1.5 验通(源码编译路线,910B3) |
| 2026-09-20 | 文档重组:runbook 与脚本定稿于本工作区(native-bringup.md + scripts/ascend/),dynamo-ascend 仓只留代码改动;新增 E1.6 |
