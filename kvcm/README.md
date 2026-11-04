# KVCM（KV Centric Manager）

KV 管理控制面：独立于推理引擎与 KV 池的第三进程。当前版本只有**控制面**——八项操作 **copy / move / keepalive / pin / demote / promote / retain / remove**、核验、记账、事件发布；使命两条：**让 KV 到位**（预热/保活/降层/驻留）+ **告诉 Router KV 在哪**。**管理面（调度）**是 D8 的目标形态，可独立部署或嵌入 Router。Mooncake 为首个接入后端；能力按版本显式声明，后端可替换。**当前目标版本：Mooncake v0.3.13.post1**（master / client 同版本）。

## 文档导航

| 层 | 文档 | 内容 |
|----|------|------|
| **全局设计** | [design/D1-router-scheduling.md](design/D1-router-scheduling.md) | 控制面：Router 集成（选路面的外部接口） |
| | [design/D2-kv-operations.md](design/D2-kv-operations.md) | 控制面：KV 操作集（八项操作语义 + mooncake 机制事实） |
| | [design/D8-joint-scheduling.md](design/D8-joint-scheduling.md) | **管理面：统一调度中心（目标形态 + 性能模型）**：hook + KVT + L0/L1/L2，请求 × KV × 扩缩容 |
| | [design/D8a-scheduling-algorithms.md](design/D8a-scheduling-algorithms.md) | **管理面：调度算法**：KVT/打分/预取/保留/扩缩的公式、图和收益模型 |
| | [design/D9-kv-services.md](design/D9-kv-services.md) | **控制面：执行与运营能力**：KV 对账 / 用量账本 / 容量建议 / 下线搬迁 / 保留规则 / 回放 / 分支（会话/预热属 D8，不重复） |
| | [design/D10-kv-fabric-roadmap.md](design/D10-kv-fabric-roadmap.md) | **规划：KV Fabric 路线图**：价值金字塔、值得做的方向、三阶段、画饼场景 |
| | [design/code-architecture.md](design/code-architecture.md) | 服务化代码架构（五层；E10/E11/E15 设计依据） |
| **专项设计** | [design/kv-prefetch.md](design/kv-prefetch.md) | copy/prefetch 详细方案（A 壳请求 / B 显式副本复制权衡与时序） |
| | [design/scheduling.md](design/scheduling.md) | 管理面调度时序（L0 选路 / L1 预取与保留 / L2 扩缩与 drain；D8 的配套） |
| **流程文档** | [design/flows/prefetch.md](design/flows/prefetch.md) | 预热流程步骤与数据流（每流程一篇，新增流程同目录另立） |
| **demo 记录** | [demo/](demo/README.md) | precopy 预热验证：仅覆盖八项中 **copy** 一项的端到端链路（vllm-ascend + Mooncake 实测通过） |
| **登记处** | [summary.md](summary.md) | 设计决策 D / 潜在问题 P / 工程项 E 全状态总表 |

> **控制面 vs 管理面**：kvcm 当前只有**控制面**（D1/D2/D9：KV 执行、核验、记账、发布）；**管理面**是调度（D8），负责请求/KV/扩缩决策。管理面可以是独立组件，也可以嵌入 Router；控制面只执行 intent，不做选路、不做扩缩、不持有前缀索引。

## 目录结构

```text
kvcm/
├── design/     # 生产化设计文档（控制面 D1/D2/D9 + 管理面 D8）
│   └── flows/  # kvcm 自己的流程（目前只有 prefetch；调度时序在 design/scheduling.md）
├── demo/       # precopy demo：只验证 copy 一项；文档+代码+测试床自包含
└── summary.md  # 登记处（D/P/E 编号总表）
```

未来生产代码（E10 kvcm 服务）落地时新建顶层代码目录，与 `demo/` 平级；`demo/` 保持自包含、可独立交付。
