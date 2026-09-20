# E 引擎线:任务与进展追踪

> E 线 = 让请求在昇腾上跑起来(引擎接入)。本文档是 E 线的任务表与进展日志,逐任务更新状态;专题结论按日期单独成文并在日志中链接。

## 环境基线(2026-09-18 核实;安装路线 2026-09-20 定稿)

- 硬件:Atlas A2 / openEuler / Kunpeng 920(aarch64)
- 镜像:`quay.io/ascend/vllm-ascend:v0.26.0rc1`(**Ubuntu 变体**;openEuler 变体容器内 `npu-smi info` 卡住、import 报线程错误,已弃用)。vLLM 0.26.0 + CANN 9.1.0 + torch_npu 2.10.0.post4,Mooncake 0.3.11.post1 已在镜像内
- Dynamo 安装(**定稿**):宿主机源码编译 fork `ascend-dev` + `.pth` 注入容器,运行手册见 [native-bringup.md](native-bringup.md);**pip wheel 路线已弃**(PyPI aarch64 wheel 在部分鲲鹏主机 `import dynamo._core` Illegal instruction)
- fork 基线:`ascend-dev` 跟踪上游 main;源码装绕过 pip 的 vllm pin,main 胶水在 agg 路径对 vllm 0.26 运行时兼容(2026-09-18 实测)

## 任务表

| 里程碑 | 任务 | 说明 | 状态 |
|--------|------|------|------|
| **E1 单机聚合跑通**(验证:OpenAI 接口出 token,Router 看到注册与 KV 事件) | E1.1 基础镜像 | 拉取 `vllm-ascend:v0.26.0rc1`(Ubuntu 变体);先 `vllm serve` 单独验证引擎出 token——不碰 Dynamo,先证引擎可用 | **完成**(Qwen3.8-27B,DP2×TP4,curl 出 token) |
| | E1.2 Dynamo 装入 NPU 环境 | 宿主机源码编译 ascend-dev(rustc 1.96 + maturin + Python 3.12 + protoc 28.3,`target-cpu=generic`),`.pth` 注入容器 site-packages | **完成**(2026-09-18 验通,步骤见 [native-bringup.md](native-bringup.md) §2) |
| | E1.3 胶水层兼容性摸底 | `dynamo.vllm` 对 vLLM 私有 API 的 import 链在 vllm-ascend/0.26 下静态走查,列 CUDA 假设点清单 | **完成**(静态 59/61 命中、生产面无 CUDA 符号 + 容器内 import 探针通过,见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md)) |
| | E1.4 拉起 worker | 常驻容器 + etcd(Docker 常驻)+ `dynamo.frontend` + `dynamo.vllm` 同容器;脚本 [scripts/ascend/](scripts/ascend/) | **已验通**(2026-09-18,910B3;本机复现待做) |
| | E1.5 全链路连通 | `/v1/models` 注册 `qwen`,`/v1/chat/completions` 出 token | **已验通**(2026-09-18,910B3;本机复现待做) |
| | E1.6 KV 事件链补验 | worker 显式 `--kv-events-config '{"enable_kv_cache_events": true}'` + frontend `--router-mode kv`(dynamo 不自动创建 kv_events_config;frontend 默认 round-robin 不消费事件)。已验通路线未覆盖此项;MTP 投机解码 + cudagraph 配置在 dynamo 路径亦未验 | 未开始 |
| 增强 | EX1 SGLang NPU 第二后端 | SGLang 主干自带 NPU 支持(`hardware_backend/npu`),vllm-ascend 路线跑通后接入(远期) | 未开始 |
| 增强 | EX2 ModelExpress 权重加速 | NPU 间流式传权重;前期共享存储兜底(远期) | 未开始 |

## 本机复现(最短路径)

按 [native-bringup.md](native-bringup.md) 执行,脚本在 [scripts/ascend/](scripts/ascend/):

```bash
export WM_ROOT=/data/wm
# 首次:宿主机源码编译 Dynamo,见 native-bringup.md §2
bash scripts/ascend/start_docker_va.sh        # 常驻容器(8 卡 + --net=host + /data)
bash scripts/ascend/start_etcd.sh             # 常驻 etcd(Docker)
bash scripts/ascend/start_dynamo_va_native.sh # 容器内 FE + worker,自动等注册
curl -s localhost:8000/v1/models              # 出现 qwen 即通
```

前置注意:

- 本机若已有占满 NPU 的 `vllm serve` 先停掉;孤儿 `VLLM::*` 进程需 `pkill -9 -f 'VLLM::'`(脚本 `stop` 子命令已含)。
- 早前 pip 装的 `ai-dynamo==1.4.2` 建议卸载(`pip uninstall -y ai-dynamo ai-dynamo-runtime`):site-packages 已装包优先于 `.pth` 注入路径,不卸会 shadow 源码版本。

## E1.6 增量(KV 事件链)

已验通路线的 worker/frontend 均未开 KV 事件。补验时改 `scripts/ascend/start_dynamo_va_native.sh`:

- worker 加 `--kv-events-config '{"enable_kv_cache_events": true}'`(publisher 默认 zmq、endpoint 默认 `tcp://*:5557`,DP 各 rank 端口自动偏移,router 按注册信息订阅);
- frontend 加 `--router-mode kv`;
- 验证:同一长前缀请求发两次,第二次应命中前缀 KV(TTFT 明显下降),router 日志可见 overlap 信息。

## 进展日志

| 日期 | 事项 |
|------|------|
| 2026-09-17 | E 线任务拆分定稿(E1.1–E1.5 + 增强项) |
| 2026-09-18 | E 线环境核实完成:镜像、版本对齐、版本陷阱确认,见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md);E 线开工 |
| 2026-09-18 | E1.3 静态阶段完成:import 面 59/61 命中(2 项为双写兜底),生产胶水层无 CUDA 符号,见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md) |
| 2026-09-18 | E1.1 波折:openEuler 变体容器内 `npu-smi info` 卡住、Python import 报 "can't start new thread";换 Ubuntu 变体 `v0.26.0rc1` 后 npu-smi 正常、import 探针通过 |
| 2026-09-18 | E1.3 关闭:容器内 import 探针通过(Ubuntu 镜像) |
| 2026-09-18 | E1.1 关闭:`vllm serve` Qwen3.8-27B(DP2×TP4,含 MTP 投机解码 + prefix caching)出 token,curl 验证通过 |
| 2026-09-18 | E1.2(pip 路线)完成:`pip install ai-dynamo==1.4.2`;后被源码编译路线取代 |
| 2026-09-18 | 并行线(dearsunlight,910B3)验通 E1.4/E1.5:源码编译 ascend-dev + `.pth` 注入,FE+worker 同容器 + etcd,curl 出 token |
| 2026-09-20 | **路线定稿**:采用源码编译路线,pip wheel 路线弃用;并行线 runbook 与脚本迁入本工作区([native-bringup.md](native-bringup.md) + [scripts/ascend/](scripts/ascend/),源 [dearsunlight/dynamo-ascend#1](https://github.com/dearsunlight/dynamo-ascend/pull/1)),dynamo-ascend 仓只留代码改动;新增 E1.6(KV 事件链补验) |
