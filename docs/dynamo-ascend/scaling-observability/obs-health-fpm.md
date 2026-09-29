# Health 健康检查与 FPM

> 专题索引:[README.md](README.md) · 代码级剖析,基于 `3rdparty/dynamo`

## 1. Health 健康检查

Dynamo 有两个健康检查端点，大白话解释：`/live` 是"你还活着吗？"——进程还在跑、没在关机就回答"活着"；`/health` 是"你能干活吗？"——不仅要活着，还要能处理请求。Canary（金丝雀）机制：每个 endpoint 有一个后台"巡逻员"，如果一段时间没收到真实请求，它就主动发个"试探包"看看后端能不能响应。如果试探包超时，说明后端有问题，/health 返回 503。

### 端点说明

| 端点 | 类型 | 检查内容 | 返回 | 大白话 |
| --- | --- | --- | --- | --- |
| `/live` | Liveness | 进程是否在 cancel/shutdown | 200 live / 503 shutting\_down | "进程还在吗？" |
| `/health` | Readiness | Canary 通过 + 所有 endpoint 就绪 + 无 ReadinessHold | 200 healthy / 503 not\_ready | "能干活吗？" |

### 如何使用健康检查

#### K8s 环境

在 K8s 的 Deployment 中配置 livenessProbe 和 readinessProbe。liveness 决定重启，readiness 决定流量。Dynamo 的 Helm chart 已经预配置了这些探针。

```
# K8s Deployment 中的健康检查配置（Helm chart 已预配置）
livenessProbe:
  httpGet:
    path: /live
    port: 8081    # DYN_SYSTEM_PORT
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /health
    port: 8081
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 3
  successThreshold: 1
```

#### 裸金属环境

非 K8s 部署可以用简单的 shell 脚本定期检查健康状态，发现问题就重启进程。

```
# 裸金属健康检查脚本
#!/bin/bash
SYSTEM_PORT=8081
MAX_RETRIES=3

for i in $(seq 1 $MAX_RETRIES); do
  status=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:$SYSTEM_PORT/health)
  if [ "$status" = "200" ]; then
    echo "OK - healthy"
    exit 0
  fi
  echo "WARN - health check failed (HTTP $status), attempt $i/$MAX_RETRIES"
  sleep 5
done

# 连续失败 → 重启进程
echo "CRITICAL - restarting worker"
systemctl restart dynamo-worker
```

### Canary 探测流程

每 endpoint 一个 tokio task:
select! {
sleep(CANARY\_WAIT\_TIME) → 空闲超时 → 发送 canary payload → 成功:Ready / 超时:NotReady
notifier.notified() → 检测到请求活动 → 立即标记 Ready，重置计时器
}

### ReadinessHold RAII 守卫

ReadinessHold 是"启动闸门"。模型加载、引擎初始化等耗时操作期间，endpoint 强制处于 NotReady 状态，不会接收任何流量。初始化完成后 drop 这个 guard，endpoint 才能转为 Ready。

```
// system_health.rs — 启动阶段持有此 guard → endpoint 强制 NotReady
let hold = SystemHealth.hold_endpoint_readiness("endpoint-name");
// ... 模型加载、初始化 ...
drop(hold);  // → 释放 hold，endpoint 可转为 Ready，流量可以进来了
```

### 健康检查与扩缩容的关联

健康检查不仅仅是监控，它还直接影响扩缩容行为。当 K8s 检测到某个 Pod 的 /health 返回 503，会将其从 Service 的 Endpoints 列表中移除，流量不再发往该 Pod。如果 /live 返回 503，K8s 会直接重启该 Pod。Planner 的扩缩容决策也依赖于健康状态：不健康的 worker 不会被计入"实际可用数量"。

## 2. FPM 性能指标 — 扩缩容的数据源

FPM（Forward Pass Metrics）是扩缩容决策的"眼睛"。大白话：每次 GPU 完成一次推理（forward pass），vLLM 就会"量"一下这次推理的数据 — 处理了多少 token、花了多少时间、队列里还剩多少请求等着。这些量化数据通过 ZMQ 发送给 Planner。Planner 用这些数据训练回归模型，预测"如果加一台机器，TTFT 会改善多少"。没有 FPM，Planner 就是瞎子 — 它不知道当前集群的性能状况，只能瞎猜。

⚠️ **FPM 默认也是关闭的！**  
需要通过 `--fpm-trace` CLI 参数或 `DYN_FPM_TRACE=1` 环境变量显式启用。不启用则不会注入 `InstrumentedScheduler`，Planner 收不到 FPM 数据，负载扩缩（load scaling）不会生效。

### FPM 采集与配置

#### 启用方式

```
# 方式 1: CLI 参数
DYN_SYSTEM_PORT=8081 python -m dynamo.vllm --model $MODEL --fpm-trace

# 方式 2: 环境变量
DYN_SYSTEM_PORT=8081 DYN_FPM_TRACE=1 python -m dynamo.vllm --model $MODEL
```

#### 采集器：InstrumentedScheduler

FPM 数据采集器是 vLLM 调度器的一个"插桩"版本。它在标准调度器的每次调度循环后，提取本次 forward pass 的统计数据。实现上使用了 WelfordAccumulator 来计算 token 长度的方差（在线算法，不需要存储所有历史值）。

InstrumentedScheduler (instrumented\_scheduler.py)
→ 继承 vLLM 标准 Scheduler
→ 每次调度循环后调用 \_update\_from\_output()
→ 提取: 处理的请求数、token 数、forward pass 耗时、队列深度
→ WelfordAccumulator 计算均值和方差
→ 组装 ForwardPassMetrics 结构体
→ msgpack 序列化 → FpmPublisherThread 后台发送

#### ZMQ 传输

FPM 数据通过 ZeroMQ 的 PUB/SUB 模式传输。每个 dp\_rank 一个 PUB socket（端口从 20380 开始递增），Planner 作为 SUB 订阅者连接所有 PUB socket。如果 worker 空闲（没有 forward pass），FpmPublisherThread 每秒发一次心跳，让 Planner 知道 worker 还活着。

```
# FPM 传输端口配置
# 每个 worker 的每个 dp_rank 一个端口
# 格式: tcp://*:{DYN_FORWARDPASS_METRIC_PORT + dp_rank}
# DYN_FORWARDPASS_METRIC_PORT 默认 20380 (envs.py:20)
# 例: dp_rank=0 → 20380, dp_rank=1 → 20381

# Planner 端订阅配置
# FpmEventSubscriber 自动连接所有 worker 的 FPM 端口
# 按 (worker_id, dp_rank) 分组，喂给回归模型
```

### FPM 数据结构

```
# msgspec.Struct + msgpack 序列化
class ForwardPassMetrics:
    version, worker_id, dp_rank, counter_id
    wall_time: float              # 这次 forward pass 花了多久（秒）
    scheduled_requests: ScheduledRequestMetrics  # 本次调度了多少请求
    queued_requests:    QueuedRequestMetrics     # 队列里还剩多少

class ScheduledRequestMetrics:
    num_prefill_requests        # Prefill 请求数
    sum_prefill_tokens          # Prefill 总 token 数
    var_prefill_length          # Prefill 长度方差
    sum_prefill_kv_tokens       # Prefill KV token 数
    num_decode_requests         # Decode 请求数
    sum_decode_kv_tokens        # Decode KV token 数
    var_decode_kv_tokens        # Decode KV token 方差
```

### 图解：FPM 数据流 — 从 GPU 到扩缩容决策

<!-- SVG diagram: FPM 数据流: GPU → vLLM → ZMQ → Planner → 扩缩容决策 — FPM 数据流: GPU → vLLM → ZMQ → Planner → 扩缩容决策 · GPU · Forward Pass -->

