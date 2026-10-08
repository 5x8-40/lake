# K8s 路径：Operator 与 Planner 决策

> 专题索引:[README.md](README.md) · 代码级剖析,基于 `3rdparty/dynamo`

K8s 部署下,扩缩容分两层:Operator 层负责加/减 Pod(唯一入口防冲突),Planner 层负责决策"要不要加"(Easy 阈值 / SLA 回归预测)。

## 1. K8s Operator 层

想象有一个"总开关"（Adapter CRD），所有想调整副本数的组件（HPA、Planner、运维）都必须通过它来操作。这样做的好处是：防止 HPA 和 Planner 同时改副本数产生冲突。总开关收到指令后，转发给 DGD（Deployment），DGD 再实际调用 K8s API 加/减 Pod。

### 调用链

HPA / KEDA / Planner → 写 Adapter .spec.replicas
→ ScalingAdapter Reconcile (scalingadapter\_controller.go:59)
→ 写 DGD .spec.components[\*].replicas
→ DGD Controller Reconcile
→ ScaleResource() (scale.go:32)
→ kubeClient.SubResource("scale").Update()
→ Pod 增减


<details><summary>▶ 展开 ScaleResource 代码</summary>

```
// scale.go:32-86
func ScaleResource(ctx, kubeClient, gvr, ns, name string, replicas int32) error {
  currentScale := autoscalingv1.Scale{}
  kubeClient.SubResource("scale").Get(ctx, resource, &currentScale)
  if currentScale.Spec.Replicas == replicas { return nil }  // 幂等
  kubeClient.SubResource("scale").Update(ctx, resource, scaleObj)
}
```

</details>


### 图解：Pod 扩缩容流程

<!-- SVG diagram: HPA / — HPA / · KEDA · 感知流量 -->

## 2. Planner 决策层

Planner 是一个"大脑"，它每 5 秒看一次各个 worker 的性能指标（FPM），然后做两个判断：
  
  
**Easy 模式（默认）：** 看 queue 和 KV 利用率，像看水温一样 — 水太热（queue 太长）就加机器，水凉了（queue 很短）就减机器。简单粗暴但有效。
  
  
**SLA 模式：** 用线性回归模型预测 — "如果现在加/减一台机器，TTFT（首 token 延迟）会是多少？会不会超过 SLA？" 这是科学决策，需要至少 5 个样本才能开始预测。最关键的是缩容安全校验：缩容前模拟一下"如果把一台机器的活分给其他机器，SLA 还满足吗？"

### 入口调用链

BuiltinLoadPropose.Propose() (local\_planner.py:331)
→ PlannerScalingState.advance\_load() (state\_machine.py:218)
→ LoadScalingMixin.\_advance\_load(obs) (load\_scaling.py:48)
→ 按 mode 分发: \_advance\_load\_agg / \_advance\_load\_disagg / \_advance\_load\_single

### 两种模式对比

| 模式 | 决策方式 | 适合场景 |
| --- | --- | --- |
| **Easy 模式** (默认) | 静态阈值：queue/KV util 硬比较 | 快速上手，不需要训练数据 |
| **SLA 模式** | 回归模型预测 TTFT/ITL + 缩容安全校验 | 有 SLA 要求，有足够 FPM 数据 |

### Easy 模式阈值 (load\_scaling.py:24-35 硬编码)

| 信号 | 扩容阈值 | 缩容阈值 | 大白话 |
| --- | --- | --- | --- |
| Prefill queue / context\_length | ≥ 1.0 | < 0.1 | 排队超过 context 长度 → 加机器 |
| Decode KV util | > 100% | < 60% | KV 缓存超卖 → 加；空闲太多 → 减 |

### SLA 模式核心公式

模型: `wall_time = coef × sum_prefill_tokens + intercept`（sklearn LinearRegression）


<details><summary>▶ 展开 TTFT 预测公式</summary>

```
# prefill.py:66-97
scale = 1.0 - clamp_kv_hit_rate(kv_hit_rate)   # KV 命中 → 减少计算量
total_tokens = (queued_prefill_tokens + avg_isl) * scale
num_iterations = ceil(total_tokens / max_batched_tokens)

ttft = 0.0
remaining = total_tokens
for _ in range(num_iterations):
    chunk = min(remaining, max_batched_tokens)
    ttft += regression.predict(chunk)      # 每个分片做回归预测
    remaining -= chunk
```

</details>


### 图解：缩容安全校验 — 为什么不能随便缩？

<!-- SVG diagram: Worker 4 — 当前 4 个 Worker，考虑缩到 3 个 · consolidation = 4/3 = 1.33 → 被移除的 worker 的负载 ×1.33 分给剩余 worker · Worker 1 -->

### 默认配置 (defaults.py)

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_load_scaling` | False | 需手动开启 |
| `ttft_ms` | 500 | TTFT SLA 目标 |
| `itl_ms` | 50 | ITL SLA 目标 |
| `load_scaling_down_sensitivity` | 80 | 缩容安全系数 0.8 |
| `max_throughput_scaling_replicas` | 8 | 最大副本数 |
| `load_min_observations` | 5 | 回归模型最少样本数 |


## 3. 吞吐量扩缩 vs 负载扩缩

Dynamo 有两套扩缩容算法并行工作：吞吐量扩缩（"慢大脑"，180s 一次，基于 Prometheus 流量预测，提前准备资源）和负载扩缩（"快大脑"，5s 一次，基于实时 FPM 数据，快速响应突发）。当两者同时开启时，吞吐量扩缩只设置一个"下限"（floor），负载扩缩在这个下限之上做微调。

| 维度 | 吞吐量扩缩 | 负载扩缩 |
| --- | --- | --- |
| **触发源** | Prometheus 流量预测（ARIMA/Kalman） | 实时 FPM（GPU 每次 forward pass） |
| **频率** | 180s（慢） | 5s（快） |
| **方向** | 预测未来需求，提前准备 | 纠正当前 SLA 违规 |
| **输入** | 预测 RPS, ISL, OSL, KV hit rate | 每引擎 queue tokens, KV util, wall\_time |
| **粒度** | 单 tick 最多 ±`max_throughput_scaling_replicas`（默认 8） | 每次 ±1 |
| **两者共存时** | 设置下限（floor） | 在 floor 之上运行 |

## 4. 扩缩容粒度

扩缩容的最小单位是"一个 worker 进程"（即一个 replica），每次 tick 最多增减 1 个。你可以独立控制 prefill 和 decode 的副本数（在 disagg 模式下），但不能单独扩缩某个 DP rank — DP rank 是 worker 内部的事。

扩缩容目标的结构（`defaults.py:138-141`）：

```
class TargetReplica(BaseModel):
    sub_component_type: SubComponentType   # "prefill" 或 "decode"
    component_name: Optional[str] = None
    desired_replicas: int                  # 目标副本数
```

