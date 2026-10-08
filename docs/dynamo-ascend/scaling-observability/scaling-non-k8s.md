# 非 K8s 路径：VirtualConnector 与 etcd 协调

> 专题索引:[README.md](README.md) · 代码级剖析,基于 `3rdparty/dynamo`

Dynamo 的扩缩容"大脑"（Planner）和 K8s 完全无关 — 它只做一件事：根据实时负载产出 ScalingDecision（目标副本数）。真正把决策变成动作的是"连接器"（PlannerConnector），按部署环境选择不同实现。非 K8s 用的是 VirtualConnector，它通过 etcd 在 Planner 和你的部署脚本之间传递决策 — 像两个人用留言板沟通：Planner 写好目标写在 etcd 上，你的脚本看到后执行，再把"已完成"写回 etcd。

## 1. 整体架构：PlannerConnector 抽象层

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

## 2. 扩缩容策略 — 三种模式

Planner 支持三种扩缩容模式，决定了"怎么算目标副本数"。模式选择通过 `mode` 配置项（默认 disagg）：

| 模式 | 说明 | 代码入口 |
| --- | --- | --- |
| **agg**（聚合） | 单一引擎同时做 prefill + decode，合并信号后统一扩缩 | `_advance_load_agg()` (line 296) |
| **disagg**（非聚合，默认） | Prefill 和 Decode 独立扩缩，各自有独立的决策逻辑 | `_advance_load_disagg()` (line 129) |
| **prefill** / **decode**（单组件） | 只扩缩一个组件类型 | `_advance_load_single()` (line 59) |

决策算法（Easy 静态阈值 / SLA 回归预测）与 K8s 路径共用同一套 Planner 代码，见 [scaling-k8s.md](scaling-k8s.md) §2；吞吐量扩缩 vs 负载扩缩见该文 §3。

## 3. VirtualConnector — 完整的 etcd 协调协议

VirtualConnector 的核心设计是：Planner 侧（Coordinator）和你的部署侧（Client）通过 etcd 共享状态。etcd 的前缀是 `v1/{namespace}/planner/`，里面有四个键：

| etcd Key | 写入者 | 含义 |
| --- | --- | --- |
| `v1/{ns}/planner/num_prefill_workers` | Coordinator（Planner 侧） | 期望的 Prefill worker 数 |
| `v1/{ns}/planner/num_decode_workers` | Coordinator（Planner 侧） | 期望的 Decode worker 数 |
| `v1/{ns}/planner/decision_id` | Coordinator（Planner 侧） | 决策版本号，每次递增 |
| `v1/{ns}/planner/scaled_decision_id` | Client（部署侧） | 已完成执行的版本号（确认回执） |

### Coordinator 写决策流程（Rust 实现，planner.rs:99-211）

update\_scaling\_decision(num\_prefill, num\_decode)
→ 与当前值比较 → 无变化则 skip
→ 检查上一轮是否已完成（is\_scaling\_ready()）
→ 未完成？记录首次跳过时间戳，等 max\_wait\_time（30min）→ 超时则强制继续
→ decision\_id += 1
→ 原子写入 etcd（kv\_put\_many，同一 revision）
→ 更新本地 decision 缓存
→ 重置 skip 时间戳

### Client 消费决策流程（Rust 实现，planner.rs:397-426）

VirtualConnectorClient（部署环境使用）
→ wait() — 阻塞等待 etcd watch（kv\_watch\_prefix），有新决策才返回
→ get() — 从 etcd prefix read 读取 PlannerDecision(num\_prefill, num\_decode, decision\_id)
→ 执行实际的 worker 启停（由用户实现）
→ complete(event) — 写 `scaled_decision_id = event.decision_id` 到 etcd

### Coordinator 等待完成（planner.rs:214-263）

Planner 发完决策后调用 `wait_for_scaling_completion()`，它每 10 秒轮询一次 etcd 中的 `scaled_decision_id`，最多等 30 分钟（180 次）。当 `scaled_decision_id ≥ decision_id` 时认为完成。如果超时，Planner 继续下一轮 tick（不会卡死）。

## 4. 完整示例代码 — 非 K8s 扩缩容

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

## 5. 便捷的扩缩接口

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

## 6. Worker 发现机制（无 K8s）

非 K8s 环境下，"当前有多少 worker 在运行"不查 K8s API，而是通过 Dynamo runtime 的端点发现。每个 worker 启动时注册自己的端点到 etcd+NATS，Planner 通过查询端点实例 ID 来统计实际数量。

get\_actual\_worker\_counts() (virtual.py:194-220)
→ \_get\_actual\_worker\_count(PREFILL) (virtual.py:222-249)
→ 构建 endpoint 名: "namespace.component.endpoint"
→ runtime.endpoint(name).client() 获取运行时客户端
→ client.instance\_ids() → 返回所有注册的实例
→ len(instance\_ids) = 实际 worker 数量

稳定性判断：不仅需要 etcd 回执确认，还需要"发现的实际数量 == 期望数量"（virtual.py:216-219）。

## 7. Planner 配置

Planner 的行为由 JSON/YAML 配置文件控制（非环境变量）。核心是 `environment`（选择连接器）和 `mode`（选择扩缩策略）。注意：`optimization_target` 的值会自动覆盖 `enable_load_scaling` 和 `enable_throughput_scaling` 的默认值——Easy 模式（非 sla）会强制开启 load scaling、关闭 throughput scaling。

#### 7.1 核心配置项

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

#### 7.2 环境变量（Planner 进程）

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

#### 7.3 扩缩模式自动覆盖规则

Planner 配置文件中的 `enable_load_scaling` 和 `enable_throughput_scaling` 默认值会被验证器自动覆盖。这是代码层面的强制逻辑（planner\_config.py:962-986）：

| optimization\_target | enable\_load\_scaling 最终值 | enable\_throughput\_scaling 最终值 | 大白话 |
| --- | --- | --- | --- |
| `throughput`（默认） | `True`（强制） | `False`（强制） | "Easy 模式：只看实时负载" |
| `latency` | `True`（强制） | `False`（强制） | "Easy 模式：只看延迟阈值" |
| `load` | `True`（强制） | `False`（强制） | "Easy 模式：只看 FPM 负载" |
| `sla` | 尊重配置值（默认 False） | 尊重配置值（默认 True） | "SLA 模式：用回归模型预测" |

两种模式可以同时开启（仅 SLA 模式下可能），此时 throughput scaling 产出"下限"，load scaling 在此之上运行。至少必须开启一个，否则验证器直接报错。

#### 7.4 扩缩决策跳过条件

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

## 8. 裸金属部署完整流程

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

