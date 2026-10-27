# KVCM（KV Centric Manager）

KV 管理控制面：独立于推理引擎与 KV 池的第三进程，管 KV 全生命周期调度——八项操作 **copy / move / keepalive / pin / demote / promote / retain / remove**。使命两条：**让 KV 到位**（预热/保活/降层/驻留）+ **告诉 Router KV 在哪**。Mooncake 为首个接入后端；能力按版本显式声明，后端可替换。

## 文档导航

| 层 | 文档 | 内容 |
|----|------|------|
| **全局设计** | [design/D1-router-scheduling.md](design/D1-router-scheduling.md) | Router 集成与请求调度（选路面） |
| | [design/D2-kv-operations.md](design/D2-kv-operations.md) | KV 调度操作集（八项操作语义 + mooncake 机制事实） |
| | [design/code-architecture.md](design/code-architecture.md) | 服务化代码架构（五层；E10/E11/E15 设计依据） |
| **专项设计** | [design/kv-prefetch.md](design/kv-prefetch.md) | copy/prefetch 详细方案（A 壳请求 / B 显式副本复制权衡与时序） |
| **流程文档** | [design/flows/prefetch.md](design/flows/prefetch.md) | 预热流程步骤与数据流（每流程一篇，新增流程同目录另立） |
| **demo 记录** | [demo/](demo/README.md) | precopy 预热验证：仅覆盖八项中 **copy** 一项的端到端链路（vllm-ascend + Mooncake 实测通过） |
| **登记处** | [summary.md](summary.md) | 设计决策 D / 潜在问题 P / 工程项 E 全状态总表 |

## 目录结构

```text
kvcm/
├── design/     # 生产化设计文档（八项操作全集）
│   └── flows/  # 流程文档（每流程一篇）
├── demo/       # precopy demo：只验证 copy 一项；文档+代码+测试床自包含
└── summary.md  # 登记处（D/P/E 编号总表）
```

未来生产代码（E10 kvcm 服务）落地时新建顶层代码目录，与 `demo/` 平级；`demo/` 保持自包含、可独立交付。
