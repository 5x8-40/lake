# KVCM（KV Centric Manager）

KV 管理控制面：独立于推理引擎与 KV 池的第三进程。使命两条：**让 KV 到位**（预热/保活/降层/驻留）+ **告诉 Router KV 在哪**。Mooncake 为首个接入后端；能力按版本显式声明，后端可替换。**当前目标版本：Mooncake v0.3.13.post1**（master / client 同版本）。

## kvcm 分两个平面

像 Kubernetes 的 kube-controller-manager 和 kube-scheduler 是两个进程、但都属于 K8s 控制面体系——kvcm 也分两半，**归属同一体系，职责不同**：

| 平面 | 职责 | 状态 | 文档 |
|---|---|---|---|
| **控制面**（Control Plane） | **执行**：八项操作 copy / move / keepalive / pin / demote / promote / retain / remove、核验、记账、事件发布 | 已实现（copy 通，其余随能力接线） | [D1](design/D1-router-scheduling.md) / [D2](design/D2-kv-operations.md) / [D9](design/D9-kv-services.md) |
| **管理面**（Management Plane） | **决策**：请求选路、会话保留、KV 放置、扩缩容 | 设计稿（D8 系列） | [D8](design/D8-joint-scheduling.md) / [D8a](design/D8a-scheduling-algorithms.md) / [D8b](design/D8b-scheduling-sequences.md) |

**部署形态是另一层**，不影响归属：管理面可以独立进程、可以嵌入 Router、也可以和控制面同进程。控制面不做选路、不做扩缩、不持有前缀索引——那些是管理面的事。

**KV Fabric** 是这两个平面**合起来的终局形态**：用户只声明需求（"我要这段记忆、这个延迟、这个预算"），系统决定放置。它不是第三个平面，也没有自己的代码——见 [D10](design/D10-kv-fabric-roadmap.md) §0。

## 文档导航

| 层 | 文档 | 内容 |
|----|------|------|
| **全局设计** | [design/D1-router-scheduling.md](design/D1-router-scheduling.md) | 控制面：Router 集成（选路面的外部接口） |
| | [design/D2-kv-operations.md](design/D2-kv-operations.md) | 控制面：KV 操作集（八项操作语义 + mooncake 机制事实） |
| | [design/D8-joint-scheduling.md](design/D8-joint-scheduling.md) | **管理面：统一调度中心（形态与边界）**：hook + KVT + L0/L1/L2 三环、收益量级、能力前提 |
| | [design/D8a-scheduling-algorithms.md](design/D8a-scheduling-algorithms.md) | **管理面：调度算法**：全部公式、推导、参数与收益模型（D8 的公式都在这） |
| | [design/D9-kv-services.md](design/D9-kv-services.md) | **控制面：执行与运营能力**：KV 对账 / 用量账本 / 容量建议 / 下线搬迁 / 保留规则 / 回放 / 分支（会话/预热属 D8，不重复） |
| | [design/D10-kv-fabric-roadmap.md](design/D10-kv-fabric-roadmap.md) | **规划：KV Fabric 路线图**：价值金字塔、值得做的方向、扩展轴（同层/垂直/横向/时间）、三阶段、目标场景 |
| | [design/code-architecture.md](design/code-architecture.md) | 服务化代码架构（五层；E10/E11/E15 设计依据） |
| **专项设计** | [design/kv-prefetch.md](design/kv-prefetch.md) | copy/prefetch 详细方案（A 壳请求 / B 显式副本复制权衡与时序） |
| | [design/D8b-scheduling-sequences.md](design/D8b-scheduling-sequences.md) | **管理面时序**：L0 选路 / L1 预取与保留 / L2 扩缩与 drain 的全部时序图 |
| **流程文档** | [design/flows/prefetch.md](design/flows/prefetch.md) | 预热流程步骤与数据流（每流程一篇，新增流程同目录另立） |
| **demo 记录** | [demo/](demo/README.md) | precopy 预热验证：仅覆盖八项中 **copy** 一项的端到端链路（vllm-ascend + Mooncake 实测通过） |
| **登记处** | [summary.md](summary.md) | 设计决策 D / 潜在问题 P / 工程项 E 全状态总表 |

> **控制面 vs 管理面**：kvcm 当前只有**控制面**（D1/D2/D9：KV 执行、核验、记账、发布）；**管理面**是调度（D8），负责请求/KV/扩缩决策。管理面可以是独立组件，也可以嵌入 Router；控制面只执行 intent，不做选路、不做扩缩、不持有前缀索引。

## 目录结构

```text
kvcm/
├── design/     # 生产化设计文档（控制面 D1/D2/D9 + 管理面 D8）
│   └── flows/  # kvcm 自己的流程（目前只有 prefetch；调度时序在 design/D8b-scheduling-sequences.md）
├── demo/       # precopy demo：只验证 copy 一项；文档+代码+测试床自包含
├── kvcm/       # 服务代码（daemon、后端、流程、Ledger）
├── tests/      # 服务代码的 pytest
└── summary.md  # 登记处（D/P/E 编号总表）
```

`demo/` 保持自包含、可独立交付。服务代码在 `kvcm/kvcm/`。
