# E 引擎线:任务与进展追踪

> E 线 = 让请求在昇腾上跑起来(引擎接入)。本文档是 E 线的任务表与进展日志,逐任务更新状态;专题结论按日期单独成文并在日志中链接。

## 环境基线(2026-09-18 核实,详见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md))

- 硬件:Atlas A2 / openEuler / Kunpeng 920(aarch64)
- 镜像:`quay.io/ascend/vllm-ascend:v0.26.0rc1`(**Ubuntu 变体**;openEuler 变体容器内 `npu-smi info` 卡住、import 报线程错误,已弃用)。vLLM 0.26.0 + CANN 9.1.0 + torch_npu 2.10.0.post4,Mooncake 0.3.11.post1 已在镜像内
- Dynamo:**ai-dynamo==1.4.2**(1.4.x 全系列 pin `vllm==0.26.0`,与镜像对齐,取最新 patch;main 1.5.0 pin 0.28.0,直接用会顶掉镜像里的 vLLM)
- fork 基线:`ascend-dev` 从上游 v1.4.2 切出,与 vllm-ascend 锁步升级

## 任务表

| 里程碑 | 任务 | 说明 | 状态 |
|--------|------|------|------|
| **E1 单机聚合跑通**(验证:OpenAI 接口出 token,Router 看到注册与 KV 事件) | E1.1 基础镜像 | 拉取 `vllm-ascend:v0.26.0rc1`(Ubuntu 变体);先 `vllm serve` 单独验证引擎出 token——不碰 Dynamo,先证引擎可用 | **完成**(Qwen3.8-27B,DP2×TP4,curl 出 token;实测命令见执行清单) |
| | E1.2 Dynamo 装入 NPU 环境 | `pip install ai-dynamo==1.4.2`(不带 [vllm] extra);aarch64 runtime wheel 已确认存在 | **完成**(ai-dynamo==1.4.2 已装) |
| | E1.3 胶水层兼容性摸底 | `dynamo.vllm` 对 vLLM 私有 API 的 import 链在 vllm-ascend/0.26 下静态走查,列 CUDA 假设点清单 | **完成**(静态 59/61 命中、生产面无 CUDA 符号 + 容器内 import 探针通过,见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md)) |
| | E1.4 拉起 worker | 按 [D001](decisions/D001-overall-approach.md) 差异清单配环境变量与 torch_npu 初始化,`dynamo.vllm` 起 vllm-ascend,注册进 etcd | 未开始 |
| | E1.5 全链路连通 | Frontend → Router → worker 出 token | 未开始 |
| 增强 | EX1 SGLang NPU 第二后端 | SGLang 主干自带 NPU 支持(`hardware_backend/npu`),vllm-ascend 路线跑通后接入(远期) | 未开始 |
| 增强 | EX2 ModelExpress 权重加速 | NPU 间流式传权重;前期共享存储兜底(远期) | 未开始 |

## E1 执行清单(镜像就绪后按序执行)

```bash
# E1.1 起容器(8 卡已全挂,设备与挂载清单见 2026-09-18-e1-bringup-env.md),先证引擎
# 实测通过(2026-09-18):Qwen3.8-27B,DP2×TP4 用满 8 卡,curl 出 token
export MODEL_PATH=/data/models/Qwen3.8-27B
vllm serve $MODEL_PATH \
    --host 0.0.0.0 --port 19999 \
    --data-parallel-size 2 --tensor-parallel-size 4 \
    --served-model-name qwen \
    --max-num-seqs 64 --max-model-len 256000 --max-num-batched-tokens 16384 \
    --trust-remote-code --enable-prefix-caching --gpu-memory-utilization 0.9 \
    --speculative-config '{"method": "qwen3_next_mtp", "num_speculative_tokens": 3, "enforce_eager": true}' \
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \
    --additional-config '{"enable_cpu_binding":true}'

# E1.2 装 dynamo(容器内)
pip install ai-dynamo==1.4.2                      # 不带 [vllm] extra

# E1.3 终验探针(脚本见 2026-09-18-e13-glue-survey.md;先 import torch_npu 再跑)
python3 e13_probe.py                              # ALL GREEN 则 E1.3 关闭

# E1.4 起 etcd + frontend + worker
# 前置:先杀掉 E1.1 的 vllm serve——它占着 NPU 显存,dynamo worker 要重新加载模型
# etcd 安装:jammy 起 Ubuntu 源已无 etcd-server 包,用静态二进制(国内走华为云镜像)
curl -LO https://mirrors.huaweicloud.com/etcd/v3.5.33/etcd-v3.5.33-linux-arm64.tar.gz
tar xzf etcd-v3.5.33-linux-arm64.tar.gz && cp etcd-v3.5.33-linux-arm64/{etcd,etcdctl,etcdutl} /usr/local/bin/
etcd > /tmp/etcd.log 2>&1 &                          # 服务发现+元数据面:worker 注册/发现、租约保活;默认 localhost:2379(无 K8s 环境的默认后端,K8s 下用 K8s API 替代)
python3 -m dynamo.frontend --router-mode kv > /tmp/frontend.log 2>&1 & # 控制面,纯 CPU,默认 8000 端口;--router-mode kv 启用 KV-aware 选路(默认 round-robin 不消费 KV 事件)
# worker:与 E1.1 相同的 vllm 参数原样透传(去掉 --host/--port,HTTP 入口归 frontend)
# 必须显式传 --kv-events-config:dynamo 1.4.2 不自动创建(args.py::create_kv_events_config,用户未传则返回 None 不发事件);
# publisher 默认 zmq、endpoint 默认 tcp://*:5557,DP 各 rank 端口自动偏移,router 按注册信息订阅
# 观察项:DP=2 与 MTP spec decode 在 dynamo.vllm 下未验证过;若起不来,先降 --data-parallel-size 1 排障
ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python3 -m dynamo.vllm \
    --model /data/models/Qwen3.8-27B \
    --data-parallel-size 2 --tensor-parallel-size 4 \
    --served-model-name qwen \
    --max-num-seqs 64 --max-model-len 256000 --max-num-batched-tokens 16384 \
    --trust-remote-code --enable-prefix-caching --gpu-memory-utilization 0.9 \
    --kv-events-config '{"enable_kv_cache_events": true}' \
    --speculative-config '{"method": "qwen3_next_mtp", "num_speculative_tokens": 3, "enforce_eager": true}' \
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \
    --additional-config '{"enable_cpu_binding":true}' \
    > /tmp/worker.log 2>&1 &
# 若 frontend/worker 报 NATS 连接错误:装 nats-server 静态二进制(arm64)起一下

# E1.5 发请求验证(打 frontend,模型名用 served-model-name)
curl localhost:8000/v1/chat/completions \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}]}'
```

## 进展日志

| 日期 | 事项 |
|------|------|
| 2026-09-17 | E 线任务拆分定稿(E1.1–E1.5 + 增强项) |
| 2026-09-18 | E 线环境核实完成:镜像 v0.26.0rc1-openeuler、ai-dynamo==1.4.0 版本对齐、版本陷阱确认,见 [2026-09-18-e1-bringup-env.md](2026-09-18-e1-bringup-env.md);E 线开工 |
| 2026-09-18 | E1.3 静态阶段完成:import 面 59/61 命中(2 项为双写兜底),生产胶水层无 CUDA 符号;容器内探针终验待 E1.1 镜像就绪,见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md) |
| 2026-09-18 | E1.1 进行中:镜像 `v0.26.0rc1-openeuler` 已拉取,8 卡容器已拉起(启动命令见环境核实文档「实测可用」节),待 `vllm serve` 验证 |
| 2026-09-18 | E1.1 波折:openEuler 变体容器内 `npu-smi info` 卡住、Python import 报 "can't start new thread";换 Ubuntu 变体 `v0.26.0rc1` 后 npu-smi 正常、import 探针通过。环境核实文档与基线已改为 Ubuntu 变体 |
| 2026-09-18 | E1.3 关闭:容器内 import 探针通过(Ubuntu 镜像) |
| 2026-09-18 | E1.1 关闭:`vllm serve` Qwen3.8-27B(DP2×TP4,含 MTP 投机解码 + prefix caching)出 token,curl 验证通过;实测命令已录入执行清单 |
| 2026-09-18 | E1.2 关闭:`pip install ai-dynamo==1.4.2` 完成 |
