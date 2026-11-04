# KVCM（KV Centric Manager）

KV 管理控制面：独立于推理引擎与 KV 池的第三进程，管 KV 全生命周期调度——八项操作 **copy / move / keepalive / pin / demote / promote / retain / remove**。使命两条：**让 KV 到位**（预热/保活/降层/驻留）+ **告诉 Router KV 在哪**。Mooncake 为首个接入后端；能力按版本显式声明，后端可替换。**当前目标版本：Mooncake v0.3.13.post1**（master / client 同版本）。

## 文档导航

| 层 | 文档 | 内容 |
|----|------|------|
| **全局设计** | [design/D1-router-scheduling.md](design/D1-router-scheduling.md) | Router 集成（选路面的外部接口；调度中心见 `../scheduling/`） |
| | [design/D2-kv-operations.md](design/D2-kv-operations.md) | KV 调度操作集（八项操作语义 + mooncake 机制事实） |
| | [design/D9-kv-services.md](design/D9-kv-services.md) | **基于 kvcm 的服务地图（目标形态 + 收益）**：会话 KV / 预热 / 反熵 / 账本 / 容量 / 迁移（不含调度） |
| | [design/code-architecture.md](design/code-architecture.md) | 服务化代码架构（五层；E10/E11/E15 设计依据） |
| **专项设计** | [design/kv-prefetch.md](design/kv-prefetch.md) | copy/prefetch 详细方案（A 壳请求 / B 显式副本复制权衡与时序） |
| **流程文档** | [design/flows/prefetch.md](design/flows/prefetch.md) | 预热流程步骤与数据流（每流程一篇，新增流程同目录另立） |
| **demo 记录** | [demo/](demo/README.md) | precopy 预热验证：仅覆盖八项中 **copy** 一项的端到端链路（vllm-ascend + Mooncake 实测通过） |
| **登记处** | [summary.md](summary.md) | 设计决策 D / 潜在问题 P / 工程项 E 全状态总表 |

> **调度中心（SC）不属于 kvcm**：见 [../scheduling/README.md](../scheduling/README.md)。kvcm 只执行 intent（copy/move/keepalive/pin/...），不做选路、不做扩缩决策、不持有前缀索引。

## 目录结构

```text
kvcm/
├── design/     # 生产化设计文档（八项操作全集）
│   └── flows/  # 流程文档（每流程一篇）
├── demo/       # precopy demo：只验证 copy 一项；文档+代码+测试床自包含
└── summary.md  # 登记处（D/P/E 编号总表）
```

未来生产代码（E10 kvcm 服务）落地时新建顶层代码目录，与 `demo/` 平级；`demo/` 保持自包含、可独立交付。
