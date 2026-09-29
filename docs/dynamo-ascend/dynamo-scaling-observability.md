# Dynamo 扩缩容与可观测性

代码级剖析 — 基于 dynamo-ascend 仓库 · 2026-09-28

## 整体架构

Dynamo 是一个大模型推理调度平台。它解决两个核心问题：流量来了怎么动态加机器（扩缩容），出了问题怎么快速定位（可观测性）。整个系统从上到下分四层，每层各管一件事。

<!-- SVG diagram: K8s Operator 层 — K8s Operator 层 · HPA/KEDA 感知流量 → Adapter CRD 转发 → Pod 增减 · 文件: scale.go · dynamographdeploymentscalingadapter_types.go -->

## 扩缩容 — 大白话

扩缩容就是"自动加/减机器"。Dynamo 的难点在于：大模型推理不只是加个 Pod 就完事 — 每个 worker 上有 KV Cache（类似 GPU 上的缓存数据库），加减机器时要把这些缓存数据迁移过去，还不能丢请求。所以扩缩容分三层：K8s 层负责加 Pod，Planner 层决定"要不要加"，KV Cache 层负责"数据怎么搬"。三层联动，缺一不可。

### 1. K8s Operator 层 Go

想象有一个"总开关"（Adapter CRD），所有想调整副本数的组件（HPA、Planner、运维）都必须通过它来操作。这样做的好处是：防止 HPA 和 Planner 同时改副本数产生冲突。总开关收到指令后，转发给 DGD（Deployment），DGD 再实际调用 K8s API 加/减 Pod。

#### 调用链

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


#### 图解：Pod 扩缩容流程

<!-- SVG diagram: HPA / — HPA / · KEDA · 感知流量 -->

### 2. Planner 决策层 Python

Planner 是一个"大脑"，它每 5 秒看一次各个 worker 的性能指标（FPM），然后做两个判断：
  
  
**Easy 模式（默认）：** 看 queue 和 KV 利用率，像看水温一样 — 水太热（queue 太长）就加机器，水凉了（queue 很短）就减机器。简单粗暴但有效。
  
  
**SLA 模式：** 用线性回归模型预测 — "如果现在加/减一台机器，TTFT（首 token 延迟）会是多少？会不会超过 SLA？" 这是科学决策，需要至少 5 个样本才能开始预测。最关键的是缩容安全校验：缩容前模拟一下"如果把一台机器的活分给其他机器，SLA 还满足吗？"

#### 入口调用链

BuiltinLoadPropose.Propose() (local\_planner.py:331)
→ PlannerScalingState.advance\_load() (state\_machine.py:218)
→ LoadScalingMixin.\_advance\_load(obs) (load\_scaling.py:48)
→ 按 mode 分发: \_advance\_load\_agg / \_advance\_load\_disagg / \_advance\_load\_single

#### 两种模式对比

| 模式 | 决策方式 | 适合场景 |
| --- | --- | --- |
| **Easy 模式** (默认) | 静态阈值：queue/KV util 硬比较 | 快速上手，不需要训练数据 |
| **SLA 模式** | 回归模型预测 TTFT/ITL + 缩容安全校验 | 有 SLA 要求，有足够 FPM 数据 |

#### Easy 模式阈值 (load\_scaling.py:24-35 硬编码)

| 信号 | 扩容阈值 | 缩容阈值 | 大白话 |
| --- | --- | --- | --- |
| Prefill queue / context\_length | ≥ 1.0 | < 0.1 | 排队超过 context 长度 → 加机器 |
| Decode KV util | > 100% | < 60% | KV 缓存超卖 → 加；空闲太多 → 减 |

#### SLA 模式核心公式

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


#### 图解：缩容安全校验 — 为什么不能随便缩？

<!-- SVG diagram: Worker 4 — 当前 4 个 Worker，考虑缩到 3 个 · consolidation = 4/3 = 1.33 → 被移除的 worker 的负载 ×1.33 分给剩余 worker · Worker 1 -->

#### 默认配置 (defaults.py)

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `enable_load_scaling` | False | 需手动开启 |
| `ttft_ms` | 500 | TTFT SLA 目标 |
| `itl_ms` | 50 | ITL SLA 目标 |
| `load_scaling_down_sensitivity` | 80 | 缩容安全系数 0.8 |
| `max_throughput_scaling_replicas` | 8 | 最大副本数 |
| `load_min_observations` | 5 | 回归模型最少样本数 |

### 3. KV Cache 路由层 Rust Go

KV Cache 是 GPU 上存储的前缀缓存。每个 block 属于某个 worker 所有。当新节点加入时，Hash 环重新划分，原来由旧节点管理的 block 需要迁移到新节点。扩容时新节点"立即可用"（不等迁移完成），缩容时先停止新请求（markDraining），迁移完再删节点。防抖机制确保不会因为一瞬间的流量波动就频繁扩缩容。

#### Router 自动扩缩决策 (lake/go/router/autoscale.go)

autoscaleTick() (line 418)
→ reapDraining() — 清理已完成 drain 的节点
→ flushHotHits() — 上报 KV hit 到控制面
→ sched.LoadSnapshot() — 取调度器快照: QueueLen, InFlight
→ scaler.Evaluate() — 决策
→ applyScale() — 执行

#### 防抖动逻辑 (Evaluate(), line 101)

就像一个温度传感器：不是说"超过 4 个请求就扩容"，而是"连续 3 次检查都超过 4，且距离上次操作超过 10 秒"才触发。如果中间恢复正常，计数器立即清零。这防止了"抖动" — 流量一会儿高一会儿低导致反复扩缩容。

| 参数 | 默认 | 大白话 |
| --- | --- | --- |
| `MinNodes` | 1 | 至少 1 个（worker-0 永不移除） |
| `MaxNodes` | 8 | 最多 8 个 |
| `ScaleOutQueueLen` | 4 | 队列 ≥ 4 持续 3 tick → 扩容 |
| `ScaleInQueueLen` | 0 | 队列为空且 inflight ≤ 1 持续 3 tick → 缩容 |
| `SustainPeriods` | 3 | 连续 tick 数 |
| `Cooldown` | 10s | 两次操作间最少间隔 |

#### 扩容 vs 缩容执行步骤

#### 扩容

1. nodes.nextID() → "worker-2"
2. cp.JoinShardNode(RPC) → 迁移列表
3. nodes.add() → 立即可路由
4. syncCapacity() → 总并发 × nodeCount
5. warmup\_plan() → 热数据预热

#### 缩容

1. victim = lastReady() → LIFO 选最新的
2. markDraining() → 停止新请求
3. cp.DrainShardNode(RPC) → 迁移计划
失败 → 回滚
4. reapDraining() → 等待迁移完成

#### 图解：一致性 Hash 环 — 节点加入时的数据迁移

<!-- SVG diagram: Before: 2 节点 — 一致性 Hash 环 (xxh3_64, 64 vnode/节点) · W0 · W1 -->

### 4. 通信域管理 (TP/EP/PP/DP) Go Python

大模型推理不是单个 worker 干活，而是一群 worker 组成了一个"通信域"。比如 TP（张量并行）把一个矩阵拆成几份、每个 GPU 算一份；EP（专家并行）让每个 GPU 负责不同的 MoE 专家。这些 worker 通过 NCCL 集体通信（collective）协同工作。关键问题是：一个 worker 挂了，整个通信域怎么办？

#### 并行组的生命周期

TP 和 PP 组在 Pod 创建时就确定了，运行期间不能变更。只有 EP 组支持"弹性伸缩"（elastic EP）— 可以在运行时动态加入/移除 DP worker。Dynamo Operator 本身不创建 torch.distributed 的 process group，它只负责编排 Pod 和基础设施；真正的 NCCL/torch.distributed 组由后端框架（vLLM、SGLang）自己组建。

#### Worker 故障的三种处理路径

#### ① Inter-Pod 故障 — 全组级联删除

单个 Pod 进入 Failed 状态
→ FailoverCascadeController (failover\_cascade\_controller.go:85)
→ 识别 engine group（相同 Grove 标签的所有 Pod）
→ 全部 force delete（grace=0）
→ Grove 重建整个 cohort
→ 全新的 NCCL 集体通信

**原因：** NCCL 集体通信、torch.distributed TCPStore 成员、CUDA IPC 状态无法原地重启 — 部分销毁后会留下半 torn-down 的残留状态。

#### ② Intra-Pod 故障 — 单 rank 恢复

Pod 内 active 容器崩溃
→ standby 容器获取 flock 锁
→ 单 rank 恢复
→ 其他 rank 不受影响
→ 共享 GPU（DRA ResourceClaims）

**原因：** active/standby 在同一 Pod 内共享 GPU，standby 已经准备好，只需要接管锁文件。无需 NCCL 重新初始化。


<details><summary>▶ 展开 FailoverCascadeController 代码</summary>

```
// failover_cascade_controller.go:104-114
// "the distributed inference group is already broken when we get here"
// leaving partial NCCL/CUDA IPC state would be worse than clean removal
func reconcileTerminalPhase(ctx, c, pod, ns) error {
  enginePods := groupByEngineLabels(getPodsByNamespace(ns))
  for _, group := range enginePods {
    for _, p := range group {
      grace := int64(0)
      c.Delete(ctx, p, &client.DeleteOptions{GracePeriodSeconds: &grace})
    }
  }
}
```

</details>


#### 图解：Worker 故障恢复路径

<!-- SVG diagram: Worker 故障被检测到 — Worker 故障：三种恢复路径 · Worker 故障被检测到 · ▼ -->

### 5. 非 K8s 部署的扩缩容操作

Dynamo 的扩缩容"大脑"（Planner）和 K8s 完全无关 — 它只做一件事：根据实时负载产出 ScalingDecision（目标副本数）。真正把决策变成动作的是"连接器"（PlannerConnector），按部署环境选择不同实现。非 K8s 用的是 VirtualConnector，它通过 etcd 在 Planner 和你的部署脚本之间传递决策 — 像两个人用留言板沟通：Planner 写好目标写在 etcd 上，你的脚本看到后执行，再把"已完成"写回 etcd。

#### 5.1 整体架构：PlannerConnector 抽象层

| 连接器 | 环境 | 执行机制 | 核心文件 |
| --- | --- | --- | --- |
| `KubernetesConnector` | K8s | DGD Scaling Adapter Scale subresource | `connectors/kubernetes.py` |
| `VirtualConnector` | 非 K8s（裸金属/VM） | etcd 协调：Coordinator 发决策 → Client 消费执行 | `connectors/virtual.py` + `lib/bindings/python/rust/planner.rs` |
| `GlobalPlannerConnector` | 多集群 | 集中式 GlobalPlanner RPC（ScaleRequest/ScaleResponse） | `connectors/global_planner.py` |

选择哪个连接器由环境变量 `environment` 决定（`planner_factory.py:31-68`）：

```
# planner_factory.py:31-42
def construct_connector(config: PlannerConfig, runtime=None, worker_info_provider=None) -> PlannerConnector:
    if config.environment == "global-planner":
        return GlobalPlannerConnector(...)
    if config.environment == "kubernetes":
        return KubernetesConnector(...)
    if config.environment == "virtual":
        return VirtualConnector(runtime, config.namespace, worker_info_provider, config.model_name)
    raise ValueError(f"Invalid environment: {config.environment}")
```

#### 5.2 扩缩容策略 — 三种模式

Planner 支持三种扩缩容模式，决定了"怎么算目标副本数"。模式选择通过 `mode` 配置项（默认 disagg）：

| 模式 | 说明 | 代码入口 |
| --- | --- | --- |
| **agg**（聚合） | 单一引擎同时做 prefill + decode，合并信号后统一扩缩 | `_advance_load_agg()` (line 296) |
| **disagg**（非聚合，默认） | Prefill 和 Decode 独立扩缩，各自有独立的决策逻辑 | `_advance_load_disagg()` (line 129) |
| **prefill** / **decode**（单组件） | 只扩缩一个组件类型 | `_advance_load_single()` (line 59) |

#### 两种决策算法：Easy 模式 vs SLA 模式

#### Easy 模式（默认）

就像看水温：设几个固定阈值，超过就扩容、低于就缩容。不需要任何历史数据，开箱即用。

| 信号 | 扩容 | 缩容 |
| --- | --- | --- |
| Prefill queue / context\_length | ≥ 1.0 | < 0.1 |
| Decode KV util | > 100% | < 60% |

硬编码在 `load_scaling.py:24-35`，每次 tick 最多 ±1 副本

#### SLA 模式

用线性回归模型预测"加/减一台机器后 TTFT/ITL 会是多少"，再跟你的 SLA 目标比较。需要至少 5 个 FPM 样本才能开始预测。

配置:

| 参数 | 默认 |
| --- | --- |
| `ttft_ms` | 500 |
| `itl_ms` | 50 |
| `load_min_observations` | 5 |

#### 吞吐量扩缩 vs 负载扩缩

Dynamo 有两套扩缩容算法并行工作：吞吐量扩缩（"慢大脑"，180s 一次，基于 Prometheus 流量预测，提前准备资源）和负载扩缩（"快大脑"，5s 一次，基于实时 FPM 数据，快速响应突发）。当两者同时开启时，吞吐量扩缩只设置一个"下限"（floor），负载扩缩在这个下限之上做微调。

| 维度 | 吞吐量扩缩 | 负载扩缩 |
| --- | --- | --- |
| **触发源** | Prometheus 流量预测（ARIMA/Kalman） | 实时 FPM（GPU 每次 forward pass） |
| **频率** | 180s（慢） | 5s（快） |
| **方向** | 预测未来需求，提前准备 | 纠正当前 SLA 违规 |
| **输入** | 预测 RPS, ISL, OSL, KV hit rate | 每引擎 queue tokens, KV util, wall\_time |
| **粒度** | 单 tick 最多 ±`max_throughput_scaling_replicas`（默认 8） | 每次 ±1 |
| **两者共存时** | 设置下限（floor） | 在 floor 之上运行 |

#### 5.3 扩缩容粒度

扩缩容的最小单位是"一个 worker 进程"（即一个 replica），每次 tick 最多增减 1 个。你可以独立控制 prefill 和 decode 的副本数（在 disagg 模式下），但不能单独扩缩某个 DP rank — DP rank 是 worker 内部的事。

扩缩容目标的结构（`defaults.py:138-141`）：

```
class TargetReplica(BaseModel):
    sub_component_type: SubComponentType   # "prefill" 或 "decode"
    component_name: Optional[str] = None
    desired_replicas: int                  # 目标副本数
```

#### 5.4 VirtualConnector — 完整的 etcd 协调协议

VirtualConnector 的核心设计是：Planner 侧（Coordinator）和你的部署侧（Client）通过 etcd 共享状态。etcd 的前缀是 `v1/{namespace}/planner/`，里面有四个键：

| etcd Key | 写入者 | 含义 |
| --- | --- | --- |
| `v1/{ns}/planner/num_prefill_workers` | Coordinator（Planner 侧） | 期望的 Prefill worker 数 |
| `v1/{ns}/planner/num_decode_workers` | Coordinator（Planner 侧） | 期望的 Decode worker 数 |
| `v1/{ns}/planner/decision_id` | Coordinator（Planner 侧） | 决策版本号，每次递增 |
| `v1/{ns}/planner/scaled_decision_id` | Client（部署侧） | 已完成执行的版本号（确认回执） |

#### Coordinator 写决策流程（Rust 实现，planner.rs:99-211）

update\_scaling\_decision(num\_prefill, num\_decode)
→ 与当前值比较 → 无变化则 skip
→ 检查上一轮是否已完成（is\_scaling\_ready()）
→ 未完成？记录首次跳过时间戳，等 max\_wait\_time（30min）→ 超时则强制继续
→ decision\_id += 1
→ 原子写入 etcd（kv\_put\_many，同一 revision）
→ 更新本地 decision 缓存
→ 重置 skip 时间戳

#### Client 消费决策流程（Rust 实现，planner.rs:397-426）

VirtualConnectorClient（部署环境使用）
→ wait() — 阻塞等待 etcd watch（kv\_watch\_prefix），有新决策才返回
→ get() — 从 etcd prefix read 读取 PlannerDecision(num\_prefill, num\_decode, decision\_id)
→ 执行实际的 worker 启停（由用户实现）
→ complete(event) — 写 `scaled_decision_id = event.decision_id` 到 etcd

#### Coordinator 等待完成（planner.rs:214-263）

Planner 发完决策后调用 `wait_for_scaling_completion()`，它每 10 秒轮询一次 etcd 中的 `scaled_decision_id`，最多等 30 分钟（180 次）。当 `scaled_decision_id ≥ decision_id` 时认为完成。如果超时，Planner 继续下一轮 tick（不会卡死）。

#### 5.5 完整示例代码 — 非 K8s 扩缩容

下面是一段完整的测试代码，展示了 Planner 侧（VirtualConnector）和部署侧（VirtualConnectorClient）如何协作。这就是你实际部署时需要写的逻辑。

```
# ============================================
# 来源: test_virtual_connector.py（真实测试用例）
# ============================================

# --- 前置条件：启动 etcd 和 NATS ---
# docker compose -f dev/docker-compose.yml up -d

# --- PLANNER 侧：创建 VirtualConnector ---
from dynamo._core import DistributedRuntime, VirtualConnectorClient
from dynamo.planner import SubComponentType, TargetReplica, VirtualConnector

runtime = DistributedRuntime(loop, "etcd", "nats")

c = VirtualConnector(
    runtime,
    "my_deployment",              # namespace，Planner 和 Client 必须相同
    worker_info_provider=DefaultWorkerInfoProvider(),
    model_name="sglang",
)
await c.async_init()

# --- 第一次扩缩：prefill=1, decode=2 ---
replicas = [
    TargetReplica(sub_component_type=SubComponentType.PREFILL, desired_replicas=1),
    TargetReplica(sub_component_type=SubComponentType.DECODE, desired_replicas=2),
]
await c.set_component_replicas(replicas, blocking=False)

# --- CLIENT 侧：部署环境消费决策 ---
client = VirtualConnectorClient(runtime, "my_deployment")

event = await client.get()
# 读取到: num_prefill_workers=1, num_decode_workers=2, decision_id=0
assert event.num_prefill_workers == 1
assert event.num_decode_workers == 2

# 在这里执行实际的扩缩容操作：
# - 启动/停止 worker 进程
# - 更新负载均衡器配置
# - 等等（具体实现由用户自己完成）

# 完成后回写确认
await client.complete(event)

# Planner 侧等待完成
await c._wait_for_scaling_completion()

# --- 第二次扩缩：使用 wait() 监听新决策 ---
async def next_scaling_decision(c):
    replicas = [
        TargetReplica(sub_component_type=SubComponentType.PREFILL, desired_replicas=5),
        TargetReplica(sub_component_type=SubComponentType.DECODE, desired_replicas=8),
    ]
    await c.set_component_replicas(replicas, blocking=False)

task = asyncio.create_task(next_scaling_decision(c))
await client.wait()        # 阻塞直到 etcd 有新决策
await task

event = await client.get()
assert event.num_prefill_workers == 5
assert event.num_decode_workers == 8
assert event.decision_id == 1  # decision_id 递增
await client.complete(event)
await c._wait_for_scaling_completion()

# --- 缩容到零 ---
replicas = [
    TargetReplica(sub_component_type=SubComponentType.PREFILL, desired_replicas=0),
    TargetReplica(sub_component_type=SubComponentType.DECODE, desired_replicas=0),
]
await c.set_component_replicas(replicas, blocking=False)
```

#### 5.6 便捷的扩缩接口

除了直接设置目标副本数，VirtualConnector 还提供了三个便捷方法 — `add_component`（+1）、`remove_component`（-1）、`set_component_replicas`（精确设置）。底层都是通过 `update_scaling_decision` 写入 etcd。

```
# 增加一个 Prefill worker
await c.add_component(SubComponentType.PREFILL, blocking=True)

# 减少一个 Decode worker（最少减到 0）
await c.remove_component(SubComponentType.DECODE, blocking=True)

# 精确设置
await c.set_component_replicas([
    TargetReplica(sub_component_type=SubComponentType.PREFILL, desired_replicas=3),
    TargetReplica(sub_component_type=SubComponentType.DECODE, desired_replicas=6),
], blocking=True)
```

#### 5.7 Worker 发现机制（无 K8s）

非 K8s 环境下，"当前有多少 worker 在运行"不查 K8s API，而是通过 Dynamo runtime 的端点发现。每个 worker 启动时注册自己的端点到 etcd+NATS，Planner 通过查询端点实例 ID 来统计实际数量。

get\_actual\_worker\_counts() (virtual.py:194-220)
→ \_get\_actual\_worker\_count(PREFILL) (virtual.py:222-249)
→ 构建 endpoint 名: "namespace.component.endpoint"
→ runtime.endpoint(name).client() 获取运行时客户端
→ client.instance\_ids() → 返回所有注册的实例
→ len(instance\_ids) = 实际 worker 数量

稳定性判断：不仅需要 etcd 回执确认，还需要"发现的实际数量 == 期望数量"（virtual.py:216-219）。

#### 5.8 Planner 配置

Planner 的行为由 JSON/YAML 配置文件控制（非环境变量）。核心是 `environment`（选择连接器）和 `mode`（选择扩缩策略）。注意：`optimization_target` 的值会自动覆盖 `enable_load_scaling` 和 `enable_throughput_scaling` 的默认值——Easy 模式（非 sla）会强制开启 load scaling、关闭 throughput scaling。

##### 5.8.1 核心配置项

| 配置项 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `environment` | `kubernetes` | 改为 `virtual` 启用非 K8s 模式 | "在哪部署" |
| `mode` | `disagg` | `agg` / `disagg` / `prefill` / `decode` | "怎么扩" |
| `optimization_target` | `throughput` | `throughput`/latency/load(Easy) / `sla`(回归模型) | "扩缩依据" |
| `enable_load_scaling` | `False` → 被验证器覆盖 | Easy 模式强制 `True`，SLA 模式默认 `False` | "实时负载扩缩" |
| `enable_throughput_scaling` | `True` → 被验证器覆盖 | Easy 模式强制 `False`，SLA 模式默认 `True` | "流量预测扩缩" |
| `advisory` | `False` | 设为 `True` 为"只算不执行"（dry-run） | "模拟模式" |
| `load_adjustment_interval_seconds` | 5 | 负载扩缩频率 | "多久看一次负载" |
| `throughput_adjustment_interval_seconds` | 180 | 吞吐量扩缩频率 | "多久预测一次流量" |
| `max_gpu_budget` | 8 | GPU 预算上限 | "最多用多少 GPU" |
| `min_endpoint` | 1 | 最少副本数 | "最少保留几个" |

##### 5.8.2 环境变量（Planner 进程）

Planner 本身不是通过命令行参数配置，而是读取 JSON/YAML 配置文件。但以下环境变量会影响其行为：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DYN_NAMESPACE` | `dynamo` | 命名空间，用于模型发现和端点注册 |
| `DYN_PARENT_DGD_K8S_NAME` | 无（K8s 模式必填） | K8s 中父 DGD 名称，未设置会报错 |
| `PROMETHEUS_ENDPOINT` | K8s 内部地址 | Prometheus 服务器 URL，Planner 用来查询流量数据 |
| `PROMETHEUS_TOKEN` | 无 | Prometheus 认证 Token |
| `PLANNER_PROMETHEUS_PORT` | `0`（关闭） | Planner 自身的指标暴露端口 |
| `SCALING_CHECK_INTERVAL` | 10 | Virtual connector 轮询间隔（秒） |
| `SCALING_MAX_WAIT_TIME` | 1800 | Virtual connector 最大等待时间（秒） |
| `ETCD_ENDPOINTS` | `http://localhost:2379` | etcd 地址（Virtual connector 用） |

##### 5.8.3 扩缩模式自动覆盖规则

Planner 配置文件中的 `enable_load_scaling` 和 `enable_throughput_scaling` 默认值会被验证器自动覆盖。这是代码层面的强制逻辑（planner\_config.py:962-986）：

| optimization\_target | enable\_load\_scaling 最终值 | enable\_throughput\_scaling 最终值 | 大白话 |
| --- | --- | --- | --- |
| `throughput`（默认） | `True`（强制） | `False`（强制） | "Easy 模式：只看实时负载" |
| `latency` | `True`（强制） | `False`（强制） | "Easy 模式：只看延迟阈值" |
| `load` | `True`（强制） | `False`（强制） | "Easy 模式：只看 FPM 负载" |
| `sla` | 尊重配置值（默认 False） | 尊重配置值（默认 True） | "SLA 模式：用回归模型预测" |

两种模式可以同时开启（仅 SLA 模式下可能），此时 throughput scaling 产出"下限"，load scaling 在此之上运行。至少必须开启一个，否则验证器直接报错。

##### 5.8.4 扩缩决策跳过条件

Planner 并非每个 tick 都会产出扩缩决策。以下情况会直接跳过（代码中的 early-return 路径）：

| 跳过条件 | 触发场景 | 大白话 |
| --- | --- | --- |
| `model_not_ready` | 性能模型无法产出容量预估 | "还没摸清性能，不扩不缩" |
| `scaling_in_progress` | 上次扩缩操作还没完成 | "上一轮还没落地，等一等" |
| `no_fpm_data` | 没有收到任何 FPM 数据 | "没数据，瞎猜不如不猜" |
| `worker_count_mismatch` | FPM worker 数 ≠ DGD 配置数 | "对不上数，先等等" |
| `insufficient_data` | 观测数据还不够多 | "数据太少，不敢决策" |
| `scale_down_refused_consolidation` | 缩容后预测违反 SLA 安全边际 | "缩了会超 SLA，不缩" |
| `advisory=True` | 模拟模式 | "只算不做" |
| `already_at_desired_count` | 当前数量 = 目标数量 | "已经在目标了，不用动" |

#### 5.9 裸金属部署完整流程

裸金属部署分两步：先启动基础设施（etcd + NATS），再启动业务组件。扩缩容时，Planner 通过 VirtualConnector 产出决策，你的部署脚本通过 VirtualConnectorClient 消费决策并执行。项目自带的启动脚本可以参考，但生产环境通常需要自己写更健壮的进程管理脚本。

| 步骤 | 命令/脚本 | 说明 |
| --- | --- | --- |
| ① 启动基础设施 | `docker compose -f dev/docker-compose.yml up -d` | 启动 etcd + NATS |
| ② 启动 workers | `python -m dynamo.vllm --model $MODEL &` | 每个 worker 一个进程，设置不同的 DYN\_SYSTEM\_PORT |
| ③ 启动前端路由 | `python -m dynamo.frontend --router-mode kv &` | 自动发现注册的 workers |
| ④ 启动 Planner | `environment=virtual python -m dynamo.planner &` | 通过 VirtualConnector 连接 etcd |
| ⑤ 部署脚本 | 自写脚本监听 VirtualConnectorClient | 消费决策 → 启停 worker 进程 |

```
# 参考: examples/backends/vllm/launch/agg_router.sh

# 启动前端路由 + KV router
python -m dynamo.frontend --router-mode kv --http-port 8000 &

# 启动 worker（每个 GPU 一个）
# 关键: 必须设 DYN_SYSTEM_PORT 才会有 /metrics；设 DYN_FPM_TRACE 才会发 FPM 数据
CUDA_VISIBLE_DEVICES=0 DYN_SYSTEM_PORT=8081 DYN_FPM_TRACE=1 \
  python3 -m dynamo.vllm --model $MODEL &
CUDA_VISIBLE_DEVICES=1 DYN_SYSTEM_PORT=8082 DYN_FPM_TRACE=1 \
  python3 -m dynamo.vllm --model $MODEL &

# 启动 Planner（非 K8s 模式）
environment=virtual \
enable_load_scaling=True \
mode=disagg \
python3 -m dynamo.planner &
```

#### 5.10 Lake Router 自动扩缩（独立于 Planner）

除了 Planner，Lake 路由器还自带了一个轻量级的自动扩缩器。它不依赖 Planner，只看路由器的"队列深度"来决定加不减节点。这个 autoscaler 默认关闭，设置 `LAKE_AUTOSCALE=1` 开启。扩容时通过 CP RPC 通知控制面加入新节点，缩容时先标记 draining 再移除。

LAKE\_AUTOSCALE=1
→ autoscaleTick() 每 2s (server.go:145)
→ reapDraining() — 清理已 drain 完的节点
→ scaler.Evaluate() — 带防抖的决策
→ applyScale()


<details><summary>▶ Router autoscale 完整代码</summary>

```
// lake/go/router/autoscale.go
type AutoscaleConfig struct {
    MinNodes         int           // default 1
    MaxNodes         int           // default 8
    ScaleOutQueueLen int           // default 4
    ScaleInQueueLen  int           // default 0
    SustainPeriods   int           // default 3 (连续 3 tick 确认)
    Cooldown         time.Duration // default 10s
}

// Evaluate: 防抖决策逻辑 (line 99-129)
func (a *Autoscaler) Evaluate(now time.Time, m MetricsSnapshot, nodeCount int) ScaleDecision {
    over := m.QueueLen >= a.cfg.ScaleOutQueueLen
    under := m.QueueLen <= a.cfg.ScaleInQueueLen && m.InFlight <= 1

    // streak counting: 连续 N 次满足条件才触发
    if over { a.overStreak++; a.underStreak = 0 }
    if under { a.underStreak++; a.overStreak = 0 }

    // cooldown: 距离上次操作至少 Cooldown 时间
    if now.Sub(a.lastAction) < a.cfg.Cooldown { return DecideNone }

    if a.overStreak >= a.cfg.SustainPeriods && nodeCount < a.cfg.MaxNodes {
        return DecideScaleOut
    }
    if a.underStreak >= a.cfg.SustainPeriods && nodeCount > a.cfg.MinNodes {
        return DecideScaleIn
    }
    return DecideNone
}

// applyScale: 执行扩容/缩容 (line 339-390)
func (s *Server) applyScale(ctx context.Context, d ScaleDecision) {
    switch d {
    case DecideScaleOut:
        id := s.nodes.nextID()
        migrations := s.cp.JoinShardNode(ctx, id) // RPC → CP
        s.nodes.add(id)  // 立即可路由
        s.syncCapacity() // 总并发 *= nodeCount
    case DecideScaleIn:
        victim := s.nodes.lastReady()  // LIFO: 最新的先进来最后走
        s.nodes.markDraining(victim)    // 停止接收新请求
        if !s.cp.DrainShardNode(ctx, victim) {
            s.nodes.setReady(victim) // 失败则回滚
            return
        }
        // reapDraining 会等待迁移完成后 RemoveShardNode
    }
}
```

</details>


## 可观测性 — 大白话

可观测性就是"怎么知道系统在干什么、有没有问题"。Dynamo 用三个手段：
  
  
**Metrics（指标）** — 就像汽车仪表盘：实时看到请求数、延迟、缓存命中率。Prometheus 定时来"看"（Pull）。
  
  
**Tracing（链路追踪）** — 追踪一个请求从进到出的完整路径，每个环节耗时多少。通过 W3C traceparent header 跨进程传播。
  
  
**FPM（性能指标）** — 从 vLLM 引擎每次 forward pass 后采集的真实性能数据，直接驱动扩缩容决策。这是扩缩容的"眼睛"。

### 1. Metrics 指标 Rust

Dynamo 的指标系统有一个四层层次结构：DRT → Namespace → Component → Endpoint。你每创建一个 metric，系统会自动加上 namespace、component、endpoint、worker\_id 四个标签，你不需要手动写。Prometheus 来抓取 /metrics 时，系统先运行回调函数更新 gauge 值，然后把所有子注册表的指标合并去重，输出 Prometheus 文本格式。注意：整个系统 server（/metrics、/health、/live）默认关闭，需设置 `DYN_SYSTEM_PORT >= 0` 才会开启。

#### 四层层次结构 + 自动标签

DRT (DistributedRuntime)
└── Namespace → 注入 dynamo\_namespace
└── Component → 注入 dynamo\_component
└── Endpoint → 注入 dynamo\_endpoint + worker\_id

#### 图解：/metrics 抓取流程

<!-- SVG diagram: Prometheus 抓取 /metrics 的完整流程 (prometheus_expfmt_combined) — Prometheus 抓取 /metrics 的完整流程 (prometheus_expfmt_combined) · Prometheus · Scrape /metrics -->

#### 关键 Metric 前缀 (prometheus\_names.rs, 1238 行)

| 前缀 | 核心指标 | 大白话 |
| --- | --- | --- |
| `dynamo_frontend_` | requests\_total, ttft, itl, kv\_hit\_rate | 前端：请求数、延迟、命中率 |
| `dynamo_router_` | router\_ttft, router\_kv\_hit\_rate | 路由：路由维度延迟和命中率 |
| `dynamo_tokio_` | worker\_busy\_ratio, global\_queue\_depth | 运行时：Tokio worker 忙度、队列深度 |
| `dynamo_routing_overhead_` | block\_hashing\_ms, scheduling\_ms | 路由开销：各阶段耗时 |
| `dynamo_planner_` | num\_prefill\_replicas, observed\_ttft\_ms | Planner：当前副本数、观测到的延迟 |

### 1.5 如何接入 Prometheus — 从零到一的完整步骤 Rust Python

接入 Prometheus 的"大白话"版：Dynamo 每个组件启动时都会自带一个 /metrics HTTP 端点，就像每家店门口摆了个"营业数据牌"。Prometheus 是个"巡逻员"，定时来各家店门口看一眼数据牌，把数据抄下来存到自己的数据库里。Grafana 是个"看板"，从 Prometheus 读取数据画成图表。你只需要做三件事：① 开启并确认 Dynamo 的 /metrics 端口能访问；② 告诉 Prometheus 去哪里看数据牌；③ 在 Grafana 里导入仪表板。

⚠️ **关键前提：/metrics 默认是关闭的！**  
`DYN_SYSTEM_PORT` 的默认值是 `-1`（禁用），所以直接 `python -m dynamo.vllm` 启动时 **不会** 启动 system status server，也就没有 `/metrics`、`/health`、`/live`。  
必须设置 `DYN_SYSTEM_PORT=<端口号>`（>=0）才会开启。例如 `DYN_SYSTEM_PORT=8081`。

#### 第一步：开启 /metrics 端点

每个 Dynamo 组件（frontend、backend worker、router）启动时，如果设置了 `DYN_SYSTEM_PORT >= 0`，就会自动启动一个 axum HTTP 服务器，暴露三个端点：`/metrics`（指标数据）、`/health`（健康检查）、`/live`（存活检查）。worker 之间端口不冲突，因为每个 worker 设了不同的 DYN\_SYSTEM\_PORT。

```
# ✅ 正确：设置 DYN_SYSTEM_PORT 才会开启 /metrics
DYN_SYSTEM_PORT=8081 python -m dynamo.vllm --model $MODEL

# ❌ 错误：不设置 DYN_SYSTEM_PORT，/metrics 不可用
python -m dynamo.vllm --model $MODEL

# Headless 模式例外：即使设了 DYN_SYSTEM_PORT 也不开启（绕过 DistributedRuntime）
DYN_SYSTEM_PORT=8081 python -m dynamo.vllm --model $MODEL --headless  # ❌ /metrics 不可用
```

##### 端点启动链

system\_status\_server.rs (axum HTTP server 初始化)
→ 创建 TcpListener 绑定 `0.0.0.0:{DYN_SYSTEM_PORT}`
→ 注册路由: `GET /metrics`, `GET /health`, `GET /live`
→ 后台 tokio task 持续监听

##### /metrics 请求处理链

`GET /metrics` → metrics\_handler(state) (system\_status\_server.rs:336)
→ state.drt().metrics() 获取 MetricsRegistry 根注册表
→ prometheus\_expfmt() (metrics.rs)
→ ① 收集所有 child registry（Namespace → Component → Endpoint 层次树）
→ ② 运行 UpdateCallback（实时计算 gauge 值，如当前 inflight 请求数）
→ ③ FamilyMerger 去重合并（按 "name|key=value" 构建唯一键，防止重复）
→ ④ prometheus-text-encode 编码为 Prometheus 标准文本格式
→ ⑤ 追加 ExpositionFormatCallback 自定义文本
→ HTTP 200 + `text/plain; charset=utf-8`

##### 验证端点

```
# 验证 frontend 的 /metrics 端点
curl http://localhost:8000/metrics | head -20

# 验证 backend worker 的 /metrics 端点
curl http://localhost:8081/metrics | head -20

# 预期输出示例:
# HELP dynamo_frontend_requests_total Total HTTP requests
# TYPE dynamo_frontend_requests_total counter
dynamo_frontend_requests_total{model="Qwen2.5-7B",worker_id="frontend-0",finish_reason="stop"} 1523
dynamo_frontend_requests_total{model="Qwen2.5-7B",worker_id="frontend-0",finish_reason="length"} 47

# HELP dynamo_frontend_time_to_first_token_seconds Time to first token
# TYPE dynamo_frontend_time_to_first_token_seconds histogram
dynamo_frontend_time_to_first_token_seconds_bucket{le="0.1"} 0
dynamo_frontend_time_to_first_token_seconds_bucket{le="0.25"} 120
...
```

#### 第二步：配置 Prometheus 抓取

Prometheus 需要一个配置文件来知道"去哪里拉数据、多久拉一次"。Dynamo 项目自带了一份开箱即用的配置文件，你只需要修改 targets 地址为你实际的组件地址。

##### 2A. 开发环境 — 静态配置

开发时每个组件的地址固定，直接写死即可。如果你在 Docker 里运行 Prometheus，用 `host.docker.internal` 可以访问宿主机的端口。


<details><summary>▶ 开发环境 prometheus.yml 完整配置</summary>

```
# dynamo/dev/observability/prometheus.yml
global:
  scrape_interval: 15s       # 全局默认 15s 拉一次
  evaluation_interval: 15s   # 规则评估间隔

scrape_configs:
  - job_name: "dynamo-frontend"
    metrics_path: /metrics
    static_configs:
      - targets: ["host.docker.internal:8000"]

  - job_name: "dynamo-backend"
    static_configs:
      - targets: ["host.docker.internal:8081", "host.docker.internal:8082"]
        labels:
          worker_type: "backend"

  - job_name: "dynamo-nixl"        # NIXL 数据传输指标
    static_configs:
      - targets: ["host.docker.internal:19090"]

  - job_name: "kvbm-metrics"        # KV Block Manager 指标
    static_configs:
      - targets: ["host.docker.internal:6880"]

  - job_name: "nats-prometheus-exporter"  # NATS 消息中间件指标
    static_configs:
      - targets: ["host.docker.internal:7777"]

  - job_name: "etcd-server"         # etcd 存储指标
    static_configs:
      - targets: ["host.docker.internal:2379"]

  - job_name: "dcgm-exporter"       # GPU 硬件指标
    static_configs:
      - targets: ["host.docker.internal:9401"]
```

</details>


##### 2B. 生产环境 K8s — ServiceMonitor

在 K8s 上不需要手动写每个 Pod 的地址 — 用 Prometheus Operator 的 ServiceMonitor，Prometheus 会自动发现所有带标签的 Pod。原理是：ServiceMonitor 通过 label selector 匹配 Service，Service 匹配 Pod，Prometheus Operator 自动把这些 Pod 的 IP:Port 生成 scrape 配置。

```
# 生产环境使用 Prometheus Operator ServiceMonitor
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: dynamo-monitor
  labels:
    release: prometheus    # 必须与 Prometheus Operator 的 release name 匹配
spec:
  selector:
    matchLabels:
      app: dynamo
  endpoints:
    - port: metrics       # 对应 Pod 的 DYN_SYSTEM_PORT
      interval: 10s
      path: /metrics
  namespaceSelector:
    matchNames: [dynamo]

# 对应的 Service 需要这样定义:
apiVersion: v1
kind: Service
metadata:
  name: dynamo-backend-svc
  labels:
    app: dynamo
spec:
  ports:
    - name: metrics
      port: 8081
      targetPort: 8081
  selector:
    app: dynamo-backend
```

##### 2C. 裸金属/虚拟机 — 服务发现脚本

如果不是 K8s 也不想手动写配置文件，可以用"文件服务发现"：写一个脚本定期生成 JSON 文件列出所有 target，Prometheus 会自动读取。这样新增/移除 worker 时只需更新文件。

```
# prometheus.yml - 文件服务发现
scrape_configs:
  - job_name: "dynamo-backend"
    file_sd_configs:
      - files:
          - "/etc/prometheus/dynamo-targets.json"
      refresh_interval: 30s

# /etc/prometheus/dynamo-targets.json - 由脚本定期生成
[
  {
    "targets": ["192.168.1.10:8081", "192.168.1.11:8081"],
    "labels": {"worker_type": "prefill", "env": "production"}
  },
  {
    "targets": ["192.168.1.12:8081", "192.168.1.13:8081"],
    "labels": {"worker_type": "decode", "env": "production"}
  }
]

# 生成脚本示例 (bash)
#!/bin/bash
# 通过 etcd 发现所有注册的 worker
ETCD_ENDPOINTS="http://localhost:2379"
PREFIX="v1/default/planner/"
targets=$(etcdctl --endpoints=$ETCD_ENDPOINTS get $PREFIX --prefix --print-value-only)
# 解析为 JSON 写入 /etc/prometheus/dynamo-targets.json
```

#### 第三步：启动可观测性栈

##### 3A. 一键启动（推荐）

Dynamo 提供了一键启动完整可观测性栈的 Docker Compose 文件。一条命令就能启动 Prometheus + Grafana + Tempo + Loki + OTel Collector + DCGM Exporter + NATS Exporter，所有组件预配置好数据源连接。

```
# 进入 dynamo/dev/observability/ 目录
cd dynamo/dev/observability/

# 一键启动
docker compose -f docker-observability.yml up -d

# 查看各服务状态
docker compose ps

# 预期输出:
# NAME                          STATUS
# observability-prometheus-1    Up (healthy)
# observability-grafana-1       Up (healthy)
# observability-tempo-1         Up
# observability-loki-1          Up
# observability-otel-collector-1 Up
# observability-dcgm-exporter-1 Up
# observability-nats-exporter-1 Up
```

##### 3B. 可观测性栈各组件说明

| 组件 | 端口 | 作用 | 大白话 | 访问地址 |
| --- | --- | --- | --- | --- |
| Prometheus | 9090 | 时序指标数据库，定时拉取 /metrics | "数据采集员" — 定时来抄表 | http://localhost:9090 |
| Grafana | 3000 | 可视化仪表板，展示 Prometheus/Loki/Tempo 数据 | "看板" — 把数据画成图 | http://localhost:3000 (admin/admin) |
| Tempo | 3200 | 分布式链路追踪存储，接收 OTLP 数据 | "追踪数据库" — 记录请求全链路 | Grafana → Explore → Tempo |
| Loki | 3100 | 日志聚合，通过 label 索引日志 | "日志仓库" — 收集所有日志 | Grafana → Explore → Loki |
| OTel Collector | 4317/4318 | 接收 OTLP 格式 metrics/traces/logs，转发到对应后端 | "数据中转站" — 收数据分发给各系统 | 无需直接访问 |
| DCGM Exporter | 9401 | NVIDIA DCGM 的 Prometheus 指标导出器 | "GPU 监控探头" — 采集 GPU 硬件指标 | Prometheus 自动抓取 |
| NATS Exporter | 7777 | NATS 消息中间件的 Prometheus 指标 | "消息队列探头" — 监控 NATS 健康 | Prometheus 自动抓取 |

##### 3C. 最小启动（只接 Prometheus）

如果你只想看指标、暂时不关心链路追踪和日志，可以只启动 Prometheus 和 Grafana。

```
# 只启动 Prometheus
docker run -d --name prometheus \
  -p 9090:9090 \
  -v $(pwd)/dynamo/dev/observability/prometheus.yml:/etc/prometheus/prometheus.yml \
  prom/prometheus:latest

# 只启动 Grafana
docker run -d --name grafana \
  -p 3000:3000 \
  -v grafana-storage:/var/lib/grafana \
  grafana/grafana:latest

# 验证 Prometheus 是否抓取成功
curl http://localhost:9090/api/v1/targets | python3 -m json.tool

# 预期: states 包含 UP 的 target
```

##### 3D. Grafana 配置数据源

Grafana 启动后需要配置数据源。如果使用 docker-observability.yml 一键启动，数据源已经预配置好了。如果手动启动，需要在 Grafana UI 中手动添加。

| 步骤 | 操作 | 配置值 |
| --- | --- | --- |
| ① | 打开 Grafana → Configuration → Data Sources → Add data source | — |
| ② | 选择 Prometheus | — |
| ③ | URL 设置为 | `http://prometheus:9090`（Docker 网络内）或 `http://localhost:9090`（宿主机） |
| ④ | 点击 "Save & Test" | 应显示 "Data source is working" |
| ⑤ | 如需 Tempo：选择 Tempo → URL `http://tempo:3200` | — |
| ⑥ | 如需 Loki：选择 Loki → URL `http://loki:3100` | — |

##### 3E. 导入 Grafana 仪表板

Dynamo 自带了多个 Grafana 仪表板 JSON 文件，位于 `dynamo/dev/observability/grafana_dashboards/`。导入方式：Grafana → Dashboards → Import → Upload JSON file。如果使用了 docker-observability.yml 一键启动，仪表板通常会自动加载。

| 仪表板 | 文件 | 内容 | 大白话 |
| --- | --- | --- | --- |
| 主仪表板 | `dynamo.json` | 请求数、TTFT/ITL、KV 命中率、队列深度 | "总览看板" — 系统整体健康一目了然 |
| K8s Operator | `dynamo-operator.json` | DGD 状态、副本数、Reconcile 延迟 | "运维看板" — 看看 K8s 控制面在干什么 |
| 非聚合推理 | `disagg-dashboard.json` | Prefill/Decode 分离的延迟和资源使用 | "分拆推理看板" — 看 prefill 和 decode 各自表现 |
| KV Block Manager | `kvbm.json` | KV block 生命周期、L0/L1/L2 命中率 | "缓存看板" — 看看 KV cache 各层命中率 |
| SGLang | `sglang.json` | SGLang 后端特有指标 | "SGLang 专用看板" |
| GPU (DCGM) | `dcgm-metrics.json` | GPU 利用率、温度、显存、ECC 错误 | "GPU 硬件看板" — 显存、温度、利用率 |
| 故障恢复 | `failover.json` | 故障次数、恢复时间、级联删除 | "故障看板" — 看系统挂了几次、恢复多快 |
| 本地资源 | `dynamo_local_resource_monitor.json` | 裸金属部署的资源监控 | "裸金属看板" — CPU/内存/磁盘使用 |

```
# 如果仪表板未自动加载，手动导入:
# 方式 1: 通过 Grafana UI
# Grafana → Dashboards → Import → 上传 JSON 文件

# 方式 2: 通过 Docker 卷挂载（自动加载）
docker run -d --name grafana \
  -p 3000:3000 \
  -v grafana-storage:/var/lib/grafana \
  -v $(pwd)/dynamo/dev/observability/grafana_dashboards:/var/lib/grafana/dashboards \
  grafana/grafana:latest

# 方式 3: 通过 curl API 导入
curl -s -X POST http://admin:admin@localhost:3000/api/dashboards/import \
  -H "Content-Type: application/json" \
  --data-binary @dynamo/dev/observability/grafana_dashboards/dynamo.json
```

#### 第四步：验证端到端数据流

启动一切之后，你需要验证数据是否正确流动。按照以下 checklist 逐项检查：

| 检查项 | 验证命令 | 预期结果 |
| --- | --- | --- |
| Dynamo /metrics 可访问 | `curl http://localhost:8081/metrics` | 返回 Prometheus 文本格式指标 |
| Prometheus 抓取成功 | 打开 http://localhost:9090 → Status → Targets | 所有 target 状态为 UP |
| Prometheus 有数据 | http://localhost:9090 → 搜索 `dynamo_frontend_requests_total` | 显示时间序列数据 |
| Grafana 数据源连通 | Grafana → Data Sources → Prometheus → Save & Test | "Data source is working" |
| Grafana 仪表板显示 | 打开导入的仪表板 | 图表有数据、非空白 |
| DCGM GPU 指标 | Prometheus → 搜索 `DCGM_FI_DEV_GPU_UTIL` | GPU 利用率数据 |

#### Dynamo 双通道指标架构

Dynamo 的指标系统有两个"面"：一个是 expfmt（文本格式），用于 Prometheus HTTP 拉取；另一个是 typed（结构化数据），用于 OTLP gRPC 推送。两者底层数据源相同，但避免了把 Prometheus 文本再解析回结构化数据的反模式。你可以只接 Prometheus（用 Pull），或者同时接 OTLP（用 Push）到任意支持 OTLP 的后端。

<!-- SVG diagram: 接入 Prometheus 的两种方式 — 接入 Prometheus 的两种方式 · 方式一: Prometheus Pull（推荐） · Prometheus -->

#### Python 端的指标注册

vLLM/SGLang 引擎启动时，会自动通过 PyO3 桥将 Python 的 prometheus\_client 指标注册到 Rust 端的 MetricsRegistry。一个指标同时注册到两个通道：expfmt（Prometheus 文本拉取）和 typed（OTLP 结构化推送）。

```
# dynamo/components/src/dynamo/common/utils/prometheus.py:55
def register_engine_metrics_callback(endpoint, registry, auto_labels=None, prefix=None):
    """注册引擎指标到两个表面。"""
    # 1. expfmt: 用于 Prometheus HTTP 拉取
    endpoint.metrics.register_prometheus_expfmt_callback(
        lambda: get_prometheus_expfmt(registry, prefix))
    # 2. typed: 用于 OTLP gRPC 推送
    endpoint.metrics.register_prometheus_typed_callback(
        lambda: get_prometheus_typed(registry, prefix))
```

#### 可用指标速查

| 指标名 | 类型 | 含义 | 关键标签 | 大白话 |
| --- | --- | --- | --- | --- |
| `dynamo_frontend_requests_total` | Counter | 总请求数 | model, worker\_id, finish\_reason | "一共处理了多少请求" |
| `dynamo_frontend_inflight_requests` | Gauge | 当前处理中的请求数 | model, worker\_id | "现在正在处理多少请求" |
| `dynamo_frontend_time_to_first_token_seconds` | Histogram | 首 token 延迟（TTFT） | model, worker\_id | "用户等了多久看到第一个字" |
| `dynamo_frontend_inter_token_latency_seconds` | Histogram | Token 间延迟（ITL） | model, worker\_id | "两个字之间隔了多久" |
| `dynamo_component_kv_cache_hit_rate` | Gauge | KV Cache 命中率 | dp\_rank, worker\_id | "缓存命中了还是得重新算" |
| `dynamo_component_gpu_cache_usage_percent` | Gauge | GPU 缓存使用率 | dp\_rank, worker\_id | "GPU 显存里的缓存占了多少" |
| `dynamo_router_inflight_requests` | Gauge | 路由层 inflight 数 | router\_id | "路由器正在处理多少请求" |
| `dynamo_planner_num_prefill_replicas` | Gauge | 当前 Prefill 副本数 | — | "Planner 决定跑几个 prefill" |
| `dynamo_planner_observed_ttft_ms` | Gauge | 观测到的 TTFT | — | "Planner 看到的实际延迟" |
| `dynamo_tokio_worker_busy_ratio` | Gauge | Tokio worker 忙度 | — | "Rust 异步运行时忙不忙" |
| `dynamo_routing_overhead_scheduling_ms` | Histogram | 路由调度耗时 | — | "路由决策花了多久" |

### 2. Tracing 链路追踪 — 从配置到查看的完整步骤 Rust Python

Tracing 的大白话解释：当一个请求进入系统，前端创建一个"根 span"（就像给这个请求发了一个身份证）。然后每经过一个组件（prefill worker → decode worker），都会创建一个子 span（"我也是这个请求的一部分"）。这些 span 通过 W3C traceparent header 跨进程传播 — 就像接力赛跑，每个跑者手里拿着同一个接力棒。最后所有 span 通过 OTLP 协议推送到 Tempo 存储，你可以在 Grafana 里看到完整的请求链路树，精确到每个环节的耗时。

#### 第一步：配置 Tracing 输出

##### 5A. 环境变量配置

Dynamo 的 tracing 初始化是幂等的（使用 Once 保证只执行一次）。你通过环境变量控制日志级别和 OTel 导出行为。

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_LOG` | `warn` | tracing-subscriber 的 EnvFilter，控制日志级别 | "日志详细程度" |
| `OTEL_EXPORT_ENABLED` | `0` | 设为 `1` 启用 OTel 导出 | "是否启用链路追踪" |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC 端点地址 | "追踪数据发到哪" |
| `OTEL_SERVICE_NAME` | `dynamo` | 服务名称，用于区分不同组件 | "这个组件叫什么名字" |
| `OTEL_RESOURCE_ATTRIBUTES` | 空 | 附加属性，如 `deployment.environment=staging` | "额外标签" |

```
# 启动 Dynamo 组件时设置环境变量
export DYN_LOG=info,dynamo=debug        # 总体 info，dynamo 模块 debug
export OTEL_EXPORT_ENABLED=1           # 启用 OTel 导出
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_SERVICE_NAME=dynamo-frontend  # 或 dynamo-backend

# 启动组件
python -m dynamo.frontend --router-mode kv
```

##### 5B. Tracing 初始化流程

init() (Once 保证幂等 — 多次调用安全)
→ setup\_logging() (logging.rs)
→ 读 `DYN_LOG` 环境变量构建 EnvFilter
→ 创建 Layered Subscriber:
├── fmt layer (stdout 输出)
├── EnvFilter (按 DYN\_LOG 过滤)
→ 若 `OTEL_EXPORT_ENABLED=1`:
├── 创建 SdkTracerProvider (opentelemetry-sdk)
├── 创建 OTLP exporter (gRPC → OTEL\_EXPORTER\_OTLP\_ENDPOINT)
├── 安装 tracing-opentelemetry layer (Rust tracing span → OTel span 桥接)
→ 安装 DistributedTraceIdLayer:
└── 每个 span 自动注入 trace\_id / span\_id 到日志行

#### 第二步：跨进程 Trace 传播

W3C Trace Context 是业界标准。Dynamo 使用 `traceparent` 和 `tracestate` HTTP headers 在组件间传播 trace context。当 frontend 收到一个请求，它检查请求是否已携带 traceparent：如果有，说明上游（如 API Gateway）已经创建了 trace，Dynamo 作为子 span 加入；如果没有，Dynamo 创建一个新的根 trace。

请求到达 Frontend:
→ 检查 `traceparent` header
├── 有 → 提取 trace\_id + parent\_span\_id，创建子 span
└── 无 → 生成新 trace\_id，创建根 span "http-request"
→ Frontend 路由请求到 Worker:
├── 将当前 span 的 traceparent 写入 NATS message header
└── Worker 从 header 提取 context，创建子 span "handle\_payload"
→ Prefill → Decode (Disagg 模式):
└── 同样通过 header 传播，保持同一 trace\_id

#### 第三步：启动 Tracing 后端

##### 3A. 使用 Docker 一键启动（包含 Tempo）

docker-observability.yml 已经包含了 Tempo（追踪存储）和 OTel Collector（数据中转）。一键启动即可。

```
# 启动完整可观测性栈（含 Tempo + OTel Collector）
docker compose -f dynamo/dev/observability/docker-observability.yml up -d

# 验证 Tempo 运行
curl http://localhost:3200/health

# 验证 OTel Collector 运行
curl http://localhost:13133
```

##### 3B. OTel Collector 配置

OTel Collector 是"数据中转站"。Dynamo 组件把 tracing data 发给 Collector，Collector 再转发给 Tempo 存储。你不需要每个 Dynamo 组件直连 Tempo。

```
# otel-collector-config.yaml (docker-observability.yml 中内嵌)
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s
    send_batch_size: 1000

exporters:
  otlp/tempo:
    endpoint: tempo:4317
    tls:
      insecure: true
  prometheus:
    endpoint: 0.0.0.0:8889

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheus]
```

##### 3C. Grafana 中查看 Traces

| 步骤 | 操作 | 说明 |
| --- | --- | --- |
| ① | 打开 Grafana → Explore | — |
| ② | 数据源选择 Tempo | — |
| ③ | 点击 "Search" 标签 | 按时间/服务名筛选 traces |
| ④ | 点击任一 trace 条目 | 进入 "Trace Graph" 查看完整 span 树 |
| ⑤ | 展开 span 查看 details | 每个 span 的耗时、标签、日志 |

#### 初始化链

init() (Once 保证幂等)
→ setup\_logging()
→ 读 DYN\_LOG 环境变量构建 EnvFilter
→ 若 OTEL\_EXPORT\_ENABLED=1 → 创建 SdkTracerProvider + OTLP exporter
→ 安装 tracing-opentelemetry layer
→ 安装 DistributedTraceIdLayer — 每 span 自动注入 trace\_id/span\_id

#### 图解：跨进程 Trace 传播

<!-- SVG diagram: 一个请求的完整 Trace 链路 — 一个请求的完整 Trace 链路 · 客户端 · HTTP Request -->

### 3. Health 健康检查 Rust

Dynamo 有两个健康检查端点，大白话解释：`/live` 是"你还活着吗？"——进程还在跑、没在关机就回答"活着"；`/health` 是"你能干活吗？"——不仅要活着，还要能处理请求。Canary（金丝雀）机制：每个 endpoint 有一个后台"巡逻员"，如果一段时间没收到真实请求，它就主动发个"试探包"看看后端能不能响应。如果试探包超时，说明后端有问题，/health 返回 503。

#### 端点说明

| 端点 | 类型 | 检查内容 | 返回 | 大白话 |
| --- | --- | --- | --- | --- |
| `/live` | Liveness | 进程是否在 cancel/shutdown | 200 live / 503 shutting\_down | "进程还在吗？" |
| `/health` | Readiness | Canary 通过 + 所有 endpoint 就绪 + 无 ReadinessHold | 200 healthy / 503 not\_ready | "能干活吗？" |

#### 如何使用健康检查

##### K8s 环境

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

##### 裸金属环境

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

#### Canary 探测流程

每 endpoint 一个 tokio task:
select! {
sleep(CANARY\_WAIT\_TIME) → 空闲超时 → 发送 canary payload → 成功:Ready / 超时:NotReady
notifier.notified() → 检测到请求活动 → 立即标记 Ready，重置计时器
}

#### ReadinessHold RAII 守卫

ReadinessHold 是"启动闸门"。模型加载、引擎初始化等耗时操作期间，endpoint 强制处于 NotReady 状态，不会接收任何流量。初始化完成后 drop 这个 guard，endpoint 才能转为 Ready。

```
// system_health.rs — 启动阶段持有此 guard → endpoint 强制 NotReady
let hold = SystemHealth.hold_endpoint_readiness("endpoint-name");
// ... 模型加载、初始化 ...
drop(hold);  // → 释放 hold，endpoint 可转为 Ready，流量可以进来了
```

#### 健康检查与扩缩容的关联

健康检查不仅仅是监控，它还直接影响扩缩容行为。当 K8s 检测到某个 Pod 的 /health 返回 503，会将其从 Service 的 Endpoints 列表中移除，流量不再发往该 Pod。如果 /live 返回 503，K8s 会直接重启该 Pod。Planner 的扩缩容决策也依赖于健康状态：不健康的 worker 不会被计入"实际可用数量"。

### 4. FPM 性能指标 — 扩缩容的数据源 Python

FPM（Forward Pass Metrics）是扩缩容决策的"眼睛"。大白话：每次 GPU 完成一次推理（forward pass），vLLM 就会"量"一下这次推理的数据 — 处理了多少 token、花了多少时间、队列里还剩多少请求等着。这些量化数据通过 ZMQ 发送给 Planner。Planner 用这些数据训练回归模型，预测"如果加一台机器，TTFT 会改善多少"。没有 FPM，Planner 就是瞎子 — 它不知道当前集群的性能状况，只能瞎猜。

⚠️ **FPM 默认也是关闭的！**  
需要通过 `--fpm-trace` CLI 参数或 `DYN_FPM_TRACE=1` 环境变量显式启用。不启用则不会注入 `InstrumentedScheduler`，Planner 收不到 FPM 数据，负载扩缩（load scaling）不会生效。

#### FPM 采集与配置

##### 启用方式

```
# 方式 1: CLI 参数
DYN_SYSTEM_PORT=8081 python -m dynamo.vllm --model $MODEL --fpm-trace

# 方式 2: 环境变量
DYN_SYSTEM_PORT=8081 DYN_FPM_TRACE=1 python -m dynamo.vllm --model $MODEL
```

##### 采集器：InstrumentedScheduler

FPM 数据采集器是 vLLM 调度器的一个"插桩"版本。它在标准调度器的每次调度循环后，提取本次 forward pass 的统计数据。实现上使用了 WelfordAccumulator 来计算 token 长度的方差（在线算法，不需要存储所有历史值）。

InstrumentedScheduler (instrumented\_scheduler.py)
→ 继承 vLLM 标准 Scheduler
→ 每次调度循环后调用 \_update\_from\_output()
→ 提取: 处理的请求数、token 数、forward pass 耗时、队列深度
→ WelfordAccumulator 计算均值和方差
→ 组装 ForwardPassMetrics 结构体
→ msgpack 序列化 → FpmPublisherThread 后台发送

##### ZMQ 传输

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

#### FPM 数据结构

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

#### FPM 数据结构

```
# msgspec.Struct + msgpack 序列化
class ForwardPassMetrics:
    version, worker_id, dp_rank, counter_id
    wall_time: float              # 这次 forward pass 花了多久
    scheduled_requests: ScheduledRequestMetrics  # 本次调度了多少请求
    queued_requests:    QueuedRequestMetrics     # 队列里还剩多少

class ScheduledRequestMetrics:
    num_prefill_requests, sum_prefill_tokens, var_prefill_length
    sum_prefill_kv_tokens, num_decode_requests
    sum_decode_kv_tokens, var_decode_kv_tokens
```

#### 图解：FPM 数据流 — 从 GPU 到扩缩容决策

<!-- SVG diagram: FPM 数据流: GPU → vLLM → ZMQ → Planner → 扩缩容决策 — FPM 数据流: GPU → vLLM → ZMQ → Planner → 扩缩容决策 · GPU · Forward Pass -->

### 5. Request Trace 请求追踪 — 审计日志 Rust

Request Trace 和分布式 Tracing 不同。Tracing 回答"这个请求经过了哪些组件、每个组件花了多久"，Request Trace 回答"这个请求的输入输出是什么、性能如何"。大白话：Tracing 是"请求路径图"，Request Trace 是"请求成绩单"。它记录每个请求的完整元数据快照 — 输入输出 token 数、TTFT、ITL、KV 命中率、队列深度等。数据通过广播 bus 分发到多个 sink（文件、NATS、OTLP、S3），每个 sink 一个独立 tokio 任务，互不阻塞。

#### 配置与使用

##### 5A. 环境变量

| 环境变量 | 默认 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_REQUEST_TRACE` | `0` | 设为 `1` 开启追踪 | "要不要记录请求成绩单" |
| `DYN_REQUEST_TRACE_SINKS` | `file` | `file,stderr,nats,otel,s3`，逗号分隔 | "记录存到哪" |
| `DYN_REQUEST_TRACE_RECORDS` | `request_end,tool` | 记录类型 | "记录哪些事件" |
| `DYN_REQUEST_TRACE_FILE_FORMAT` | `jsonl_gz` | 可选 `jsonl` / `jsonl_gz` | "文件格式：纯文本还是压缩" |
| `DYN_REQUEST_TRACE_FILE_PATH` | `/tmp/request_trace.jsonl.gz` | 文件输出路径 | "文件存哪" |

##### 5B. 开启方法

```
# 最小配置：开启追踪，输出到本地文件
export DYN_REQUEST_TRACE=1
export DYN_REQUEST_TRACE_SINKS=file
export DYN_REQUEST_TRACE_FILE_PATH=/var/log/dynamo/request_trace.jsonl.gz

# 完整配置：同时输出到文件 + NATS + OTLP
export DYN_REQUEST_TRACE=1
export DYN_REQUEST_TRACE_SINKS=file,nats,otel
export DYN_REQUEST_TRACE_RECORDS=request_end,tool,request_payload
export DYN_REQUEST_TRACE_FILE_FORMAT=jsonl_gz

# 启动 Dynamo
python -m dynamo.frontend --router-mode kv
```

#### 数据模型

```
struct RequestTraceRecord {
    event_type: RequestTraceEventType,  // RequestEnd | ToolStart | ToolEnd | RequestPayload
    request:   Option<RequestTraceMetrics>,
    tool:      Option<RequestTraceToolEvent>,
    payload:   Option<RequestTracePayload>,
}
// 核心字段: request_id, model, input/output_tokens, ttft_ms, itl_ms,
//           kv_hit_rate, queue_depth, worker, finish_reason
```

#### 架构 — 广播 Bus + 独立 Sink 任务

TelemetryBus<RequestTraceRecord> // broadcast, capacity=1024
├──→ tokio task → JsonlGzipSink // 1MB buffer, 1s flush, 256MB roll
├──→ tokio task → NatsSink // 发送到 NATS 主题
├──→ tokio task → OtelSink // OTLP 导出到追踪后端
└──→ tokio task → S3Sink // 上传到对象存储

#### Request Trace vs Tracing vs Metrics

|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 维度 | Metrics || Tracing | Request Trace |
| 大白话 | "仪表盘" — 聚合统计 | "路径图" — 请求链路 | "成绩单" — 逐请求审计 |
| 粒度 | 聚合（直方图/计数器） | 单请求（span 树） | 单请求（完整元数据） |
| 存储 | Prometheus（时序 DB） | Tempo（追踪 DB） | 文件/NATS/S3（原始数据） |
| 用途 | 实时监控、告警、扩缩容 | 调试延迟、定位瓶颈 | 审计、数据分析、计费 |
| 开销 | 极低（计数/打桶） | 中等（span 创建/导出） | 较高（逐请求记录） |

## 可观测性配置速查

下面是一张"抄作业"表格 — 列出可观测性相关的所有关键环境变量及其默认值。大部分可观测功能默认关闭，需要手动开启。

#### System Server（/metrics、/health、/live）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_SYSTEM_PORT` | `-1`（关闭） | 设为 >=0 开启，0 为随机端口 | "指标服务器端口，默认关着" |
| `DYN_SYSTEM_HOST` | `0.0.0.0` | 绑定地址 | "监听哪个 IP" |
| `DYN_SYSTEM_HEALTH_PATH` | `/health` | 健康检查路径 | — |
| `DYN_SYSTEM_LIVE_PATH` | `/live` | 存活检查路径 | — |
| `DYN_SYSTEM_STARTING_HEALTH_STATUS` | `NotReady` | 初始状态：Ready / NotReady | "启动时说不说能干活" |

#### Health Check（金丝雀探测）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_HEALTH_CHECK_ENABLED` | `false` | 启用主动健康检查 | "要不要主动试探后端，默认关着" |
| `DYN_CANARY_WAIT_TIME` | 10s | 空闲等待时间 | "多久没请求就试探一次" |
| `DYN_HEALTH_CHECK_REQUEST_TIMEOUT` | 3s | 单次探测超时 | "试探包等多久算超时" |

#### 日志（Log）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_LOG` | `info` | 日志级别，支持 `target=level` 语法 | "日志详细程度" |
| `DYN_LOGGING_CONSOLE_FORMAT` | `readable` | 设为 `jsonl` 输出结构化 JSON 日志 | "日志格式：人看还是机器看" |
| `DYN_LOG_USE_LOCAL_TZ` | false（UTC） | 使用本地时区 | "日志时间戳用哪个时区" |

#### OTLP 导出（Traces + Logs + Metrics）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `OTEL_EXPORT_ENABLED` | 关闭 | 设为 `1` 启用 traces+logs 导出 | "追踪和日志导出，默认关着" |
| `OTEL_METRICS_EXPORTER` | 关闭 | 设为 `otlp` 启用 metrics 导出 | "指标 OTLP 推送，默认关着" |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC 端点 | "数据发到哪" |
| `OTEL_SERVICE_NAME` | `dynamo` | 服务名称 | "这个组件叫什么" |
| `OTEL_METRIC_EXPORT_INTERVAL` | 60000ms（60s） | 指标推送间隔（毫秒） | "多久推一次指标" |
| `OTEL_TRACES_SAMPLE_RATIO` | 1.0（全采） | 采样率 0.0-1.0 | "抓多少追踪数据" |

#### Request Trace（请求审计日志）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_REQUEST_TRACE` | 关闭 | 设为 `1` 开启 | "逐请求审计，默认关着" |
| `DYN_REQUEST_TRACE_SINKS` | `file` | `file,stderr,nats,otel,s3` | "记录存到哪" |
| `DYN_REQUEST_TRACE_RECORDS` | `request_end,tool` | 记录类型 | "记哪些事件" |
| `DYN_REQUEST_TRACE_FILE_PATH` | `/tmp/dynamo-request-trace` | 文件输出路径 | "文件存哪" |
| `DYN_REQUEST_TRACE_FILE_FORMAT` | `jsonl_gz` | `jsonl` / `jsonl_gz` | "纯文本还是压缩" |
| `DYN_REQUEST_TRACE_CAPACITY` | 1024 | 内存队列容量，满了会丢数据 | "排队 buffer 多大" |

#### Frontend 指标直方图桶

Frontend 的直方图指标（TTFT、ITL、请求时长等）支持自定义桶边界。通过 `DYN_METRICS_TTFT_MIN`、`DYN_METRICS_TTFT_MAX`、`DYN_METRICS_TTFT_COUNT` 等环境变量控制。类似地还有 `DYN_METRICS_ITL_*`、`DYN_METRICS_REQUEST_DURATION_*`、`DYN_METRICS_INPUT_SEQUENCE_*`、`DYN_METRICS_OUTPUT_SEQUENCE_*`。

#### 一键开启所有可观测性（推荐开发环境）

```
# 开启 /metrics + /health + /live
export DYN_SYSTEM_PORT=8081

# 开启 OTel Traces + Logs 导出
export OTEL_EXPORT_ENABLED=1
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_SERVICE_NAME=dynamo-backend

# 开启 OTel Metrics 推送（可选，Prometheus Pull 已够用的话不需要）
export OTEL_METRICS_EXPORTER=otlp

# 开启结构化 JSON 日志（可选，方便 Loki 采集）
export DYN_LOGGING_CONSOLE_FORMAT=jsonl

# 开启金丝雀健康检查（可选）
export DYN_HEALTH_CHECK_ENABLED=1

# 启动 Dynamo worker
DYN_SYSTEM_PORT=8081 python -m dynamo.vllm --model $MODEL
```

**总结:**

**扩缩容:** 四层联动 — K8s Adapter 管 Pod（唯一入口防冲突），Planner 管决策（SLA 回归预测 + 缩容安全校验），KV Hash Ring 管数据迁移（两阶段 drain + 热数据预热），FPM 管数据源（GPU → ZMQ → 回归模型）。扩缩策略分 agg（组合）/ disagg（独立 prefill/decode）/ single（单一组件），算法分 Easy（静态阈值）和 SLA（回归模型预测）两档，粒度为每 5s ±1 副本。

**通信域:** TP/PP 固定（固定生命周期），EP 弹性（支持 live resize）。故障时 inter-pod 级联删除全组（NCCL 无法部分恢复）、intra-pod 单 rank 恢复（standby 接管 + GMS 加速）。

**非 K8s 扩缩容:** 通过 PlannerConnector 抽象层切换到 VirtualConnector，etcd 协调（Coordinator 写决策 → Client 消费决策 → 执行启停），完整协议包含 decision\_id/scaled\_decision\_id 握手机制。

**可观测性:** 三管齐下 — Metrics（Prometheus Pull / OTLP Push 双通道，四层层次结构自动标签，10+ Grafana 仪表板）、Tracing（W3C traceparent 跨进程传播，Tempo 存储，Grafana 查看）、Request Trace（逐请求审计日志，广播 Bus 多 Sink）。FPM 是扩缩容的数据源，从 GPU 到决策的完整链路：InstrumentedScheduler → ZMQ → Planner → 回归模型 → 扩缩容决策。

---

# Ascend 迁移约束审视

Dynamo 原生于 NVIDIA GPU 平台。迁移到 Ascend NPU 后，扩缩容和可观测性的关键模块存在大量 NVIDIA 硬依赖。以下是代码级排查结果。

## 总览

| 模块 | NVIDIA 依赖 | Ascend 可用性 | 改动工作量 |
|------|------------|--------------|-----------|
| FPM 指标采集 | 部分（设备身份识别走 CUDA） | 部分可用 | 中 |
| Prometheus 可观测栈 | 全部（DCGM exporter、NVML/DCGM actuator） | 不可用 | 大 |
| K8s Operator 扩缩容 | 全部（`nvidia.com/gpu` 资源名硬编码 110+ 处） | 不可用 | 大 |
| Worker 健康检查 | 无 | 可用 | 无 |
| 通信域 (TP/EP) | 全部（NCCL 直接调用 543+ 处，无 HCCL） | 不可用 | 大 |
| VirtualConnector (非 K8s) | 无 | 可用 | 无 |

## 1. FPM 指标采集

### 可用部分

FPM 的数据管线（Schema + 消息中继）是纯 Python/Rust，不涉及 GPU 硬件：

- Schema: `components/src/dynamo/common/forward_pass_metrics.py` — 纯 msgspec 结构体
- 发布器: `lib/llm/src/fpm_publisher.rs` — 纯 ZMQ 消息转发
- 采集: `InstrumentedScheduler` 从调度器读取 `sum_prefill_tokens`、`kv_utilization` 等调度层指标，不直接读 GPU 内存

### 不可用部分

- **设备身份识别**: `gpu_memory_service/v1/device.py` 使用 `cuda.bindings.driver` (cuda-python) 调用 `cuDeviceGet()` 和 `cuDeviceGetUuid()` 获取设备标识。Ascend 上没有 cuda-python，会直接报错。
- **CUDA Graph 字段**: `InstrumentedScheduler` 记录了 `cudagraph_mode`、`cudagraph_capture_sizes` 等配置值。这些来自 vLLM 编译配置对象而非硬件读取，字段名在 NPU 上有误导。

### 适配要点

```
需要做的事情:
1. gpu_memory_service 的设备识别层需要适配 Ascend（使用 CANN/npu-smi 替代 cuda.bindings.driver）
2. FPM Schema 中的 CUDA Graph 相关字段在 Ascend 上置 None 或重命名
3. FPM 的 ZMQ 传输层和 Planner 消费逻辑本身不需要改
```

## 2. Prometheus 可观测栈

### 问题

整个可观测性部署脚本 `deploy/observability/setup-monitoring.sh` 依赖 NVIDIA 生态：

- 部署 `nvidia-dcgm-exporter` 通过 NVIDIA GPU Operator Helm chart
- 自定义指标文件 `dcgm-metrics-with-nvlink.csv` 全部是 DCGM field ID（`DCGM_FI_DEV_SM_CLOCK`、`DCGM_FI_DEV_GPU_TEMP`、`DCGM_FI_DEV_POWER_USAGE` 等）
- 功耗代理 `deploy/power-agent/actuator.py` 两个实现 `NvmlActuator`（`pynvml`）和 `DcgmActuator`（`pydcgm`）全部是 NVIDIA 专有

### 适配要点

```
需要做的事情:
1. 替换 DCGM exporter → Ascend NPU 指标导出器（npu-smi 或 Ascend Manager）
2. 重写 Prometheus 抓取配置，指向 NPU 指标端点
3. 功耗管理: 用 Ascend 功耗 API 替换 pynvml/pydcgm
4. Grafana 仪表板需要重新映射指标名（dynamo_gpu_* → dynamo_npu_*）
```

## 3. K8s Operator 扩缩容

### 问题

这是约束最重的模块。`nvidia.com/gpu` 在 operator 代码中被硬编码了 110 次，分布在 27 个文件中。

关键硬编码点：

| 文件 | 硬编码内容 |
|------|-----------|
| `internal/consts/consts.go:149` | `KubeResourceGPUNvidia = "nvidia.com/gpu"` 常量 |
| `internal/consts/consts.go:68` | `dynamo.nvidia.com/gpu-power-limit` 注解 |
| `internal/gpu/discovery.go` | GPU 发现全走 DCGM exporter pod (`:9400/metrics`)，解析 DCGM 字段；GPU 型号推断只识别 NVIDIA 型号（GB200/H100/A100）和 AMD MI300，没有 Ascend 型号 |
| `internal/dynamo/failover.go:265` | failover toleration 硬编码 `Key: "nvidia.com/gpu"` |
| `internal/dra/dra.go` | GPU 节点 taint `nvidia.com/gpu=NoSchedule`，MIG 形状 `nvidia.com/mig-3g.20gb` |

### 适配要点

```
需要做的事情:
1. KubeResourceGPUNvidia 常量改为可配置（通过 Helm values 或环境变量注入），
   Ascend 对应资源名: "ascend.npu" 或 "huawei.com/ascend"（取决于设备插件）
2. GPU 发现层: 替换 DCGM exporter 查询 → Ascend NPU 发现机制
   （使用 Ascend device plugin 标注的节点标签）
3. GPU 型号推断表: 加入 Ascend 910B/910C 型号
4. failover toleration: 使用配置的资源名而非硬编码
5. 功耗注解: 将 dynamo.nvidia.com/ 改为通用命名空间
6. CRD 示例文件中 nvidia.com/gpu 全部替换
```

## 4. Worker 健康检查

**可直接使用，无需改动。** 健康检查 (`components/src/dynamo/vllm/health_check.py`) 通过发送测试 prompt 验证引擎响应，不依赖 GPU 特定 API。

## 5. 通信域 (TP/EP) — NCCL vs HCCL

### 问题

这是最核心的依赖。Dynamo 的集体通信层 100% 绑定 NCCL：

- `lib/kvbm-engine/src/collectives/nccl.rs`: 直接导入 `cudarc::nccl::sys`（`ncclBcast`, `ncclComm_t`, `ncclCommDestroy`, `ncclDataType_t`, `ncclGroupEnd`, `ncclGroupStart`），无任何 trait 抽象
- `lib/llm/src/block_manager/distributed/nccl_bootstrap.rs`: NCCL bootstrap，62 处 NCCL 引用
- `lib/llm/src/block_manager/block/transfer/nccl.rs`: NCCL 数据传输
- `lib/memory/src/numa/nvml.rs`: GPU 枚举走 NVML FFI (`libnvidia-ml.so.1`)
- Rust 代码总计 543+ 处 NCCL 引用，**零处 HCCL 引用**
- `dynamo-ascend` fork 中同样零处 HCCL 适配

### 影响

NCCL 是 NVIDIA 专有的多 GPU 集体通信库。Ascend 的等价物是 HCCL（Huawei Collective Communication Library），两者 API 不兼容。

```
需要做的事情:
1. 集体通信层需要 trait 抽象（CollectiveOps），然后实现 NCCL 和 HCCL 两个后端
2. 或用 Cargo feature 门控条件编译（#[cfg(feature = "hccl")]）
3. 设备枚举层（nvml.rs）替换为 Ascend NPU 管理库
4. CUDA stream/event（cudarc）替换为 Ascend stream/event
5. 这是一个底层运行时改造，影响范围广，建议作为独立 milestone
```

## 6. VirtualConnector (非 K8s 部署)

**可直接使用，无需改动。** `components/src/dynamo/planner/connectors/virtual.py` 是纯平台无关代码：

- `get_gpu_counts()` 返回 `(None, None)` — 明确标注 "Virtual deployments do not expose GPU shape"
- `get_gpu_shapes()` 同理
- 所有协调通过 etcd 进行，不依赖 K8s GPU 资源 API

## 7. 实际部署建议

基于以上分析，Ascend 平台的可运行路径如下：

### 近期可运行的部分

| 能力 | 状态 |
|------|------|
| 单卡推理 | 可用（vLLM Ascend 后端直接工作，不依赖 Dynamo 扩缩容/可观测） |
| 健康检查端点 | 可用（`/health`、`/live` 纯引擎响应检查） |
| 非 K8s 扩缩容框架 | 可用（VirtualConnector + etcd 协调，但 GPU 发现需要补实现） |
| Tracing/Request Trace | 可用（OTLP 导出和请求审计不依赖 GPU 硬件） |

### 需要适配才能运行的部分

| 能力 | 阻塞点 |
|------|--------|
| K8s 扩缩容 | `nvidia.com/gpu` 硬编码 — 需要配置化资源名 + NPU 发现 |
| Prometheus 监控 | DCGM 依赖 — 需要 NPU 指标导出器 |
| FPM 驱动的自动扩缩 | 设备身份识别 — 需要 Ascend 设备层 |
| 多卡通信域 (TP/EP) | NCCL 硬编码 — 需要 HCCL 后端（最大工作量） |