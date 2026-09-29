# Dynamo 扩缩容与可观测性

> 代码级剖析 — 基于 `3rdparty/dynamo` · 2026-09-28 · 重构自 [#46](https://github.com/5x8-40/lake/pull/46) 的单文件版本

Dynamo 是一个大模型推理调度平台。它解决两个核心问题：流量来了怎么动态加机器（**扩缩容**），出了问题怎么快速定位（**可观测性**）。整个系统从上到下分四层，每层各管一件事。

<!-- SVG diagram: K8s Operator 层 — K8s Operator 层 · HPA/KEDA 感知流量 → Adapter CRD 转发 → Pod 增减 · 文件: scale.go · dynamographdeploymentscalingadapter_types.go -->

## 文档索引

扩缩容（按部署形态分路径，外加两个横切主题）：

| 文档 | 内容 |
| --- | --- |
| [scaling-k8s.md](scaling-k8s.md) | K8s 路径：Operator 层（Adapter CRD 唯一入口）+ Planner 决策层（Easy 阈值 / SLA 回归预测、缩容安全校验、吞吐量 vs 负载双算法、扩缩粒度） |
| [scaling-non-k8s.md](scaling-non-k8s.md) | 非 K8s 路径：PlannerConnector 抽象、VirtualConnector etcd 协调协议（decision_id 握手）、完整示例、Worker 发现、Planner 配置、裸金属流程 |
| [scaling-router.md](scaling-router.md) | KV Cache 路由层：一致性 Hash 环数据迁移 + Lake Router 自带轻量 autoscaler（本仓 `go/router/autoscale.go`） |
| [scaling-comm-domain.md](scaling-comm-domain.md) | 通信域管理：TP/PP 固定、EP 弹性；Worker 故障三种恢复路径（inter-pod 级联删除 / intra-pod 单 rank 恢复） |

可观测性（Metrics / Tracing / Health / FPM / Request Trace）：

| 文档 | 内容 |
| --- | --- |
| [obs-metrics.md](obs-metrics.md) | Metrics 四层层次结构与自动标签 + 接入 Prometheus 从零到一（端点开启、抓取配置、可观测性栈、端到端验证） |
| [obs-tracing.md](obs-tracing.md) | Tracing 链路追踪（W3C traceparent 跨进程传播、Tempo 存储）+ Request Trace 逐请求审计（广播 Bus 多 Sink） |
| [obs-health-fpm.md](obs-health-fpm.md) | Health 健康检查（/live vs /health、Canary 探测）+ FPM 性能指标（**扩缩容的数据源**：GPU → ZMQ → Planner 回归模型） |
| [obs-cheatsheet.md](obs-cheatsheet.md) | 可观测性配置速查：全部关键环境变量、默认值、一键开启脚本 |

迁移约束：

| 文档 | 内容 |
| --- | --- |
| [ascend-migration.md](ascend-migration.md) | Ascend 迁移约束审视：逐模块代码级排查 NVIDIA 硬依赖（DCGM、`nvidia.com/gpu` 硬编码、NCCL→HCCL 映射），标注直接可用 / 需适配 |

两部分的关系：**FPM 是扩缩容与可观测性的交汇点** — 它是可观测体系采集的性能数据，同时直接驱动 Planner 的扩缩容决策。

## 总结

**扩缩容:** 四层联动 — K8s Adapter 管 Pod（唯一入口防冲突），Planner 管决策（SLA 回归预测 + 缩容安全校验），KV Hash Ring 管数据迁移（两阶段 drain + 热数据预热），FPM 管数据源（GPU → ZMQ → 回归模型）。扩缩策略分 agg（组合）/ disagg（独立 prefill/decode）/ single（单一组件），算法分 Easy（静态阈值）和 SLA（回归模型预测）两档，粒度为每 5s ±1 副本。

**通信域:** TP/PP 固定（固定生命周期），EP 弹性（支持 live resize）。故障时 inter-pod 级联删除全组、intra-pod 单 rank 恢复（standby 接管 + GMS 加速）。TP/EP 集体通信由推理引擎负责（vLLM Ascend 已适配 HCCL），Dynamo 仅透传配置参数。

**非 K8s 扩缩容:** 通过 PlannerConnector 抽象层切换到 VirtualConnector，etcd 协调（Coordinator 写决策 → Client 消费决策 → 执行启停），完整协议包含 decision\_id/scaled\_decision\_id 握手机制。

**可观测性:** 三管齐下 — Metrics（Prometheus Pull / OTLP Push 双通道，四层层次结构自动标签，10+ Grafana 仪表板）、Tracing（W3C traceparent 跨进程传播，Tempo 存储，Grafana 查看）、Request Trace（逐请求审计日志，广播 Bus 多 Sink）。FPM 是扩缩容的数据源，从 GPU 到决策的完整链路：InstrumentedScheduler → ZMQ → Planner → 回归模型 → 扩缩容决策。
