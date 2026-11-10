# D10: KV Fabric 路线图（规划草案）

- 状态: 规划草案。不承诺排期，用于讨论方向取舍。
- 版本基线: Mooncake v0.3.13.post1；vLLM-Ascend 0.26 系
- 上游: [D8](D8-joint-scheduling.md)（管理面）、[D9](D9-kv-services.md)（控制面）
- 一句话: 把 KV 变成**系统可放置、可复制、可迁移、可计价**的资源；应用仍然只发请求。

---

## 0. 北极星：KV Fabric 是什么

> **KV Fabric**：系统统一决定 KV 放在哪、复制几份、保留多久、什么时候迁移/降层；应用不指定实例，也不持有 KV 句柄。

三个转变：

1. **从 cache 到 state**：KV 不只是请求内缓存，而是 Agent 会话的长期状态；
2. **从 routing 到 placement**：不只“谁有 KV 就发给谁”，而是“把 KV 放到该去的地方”；
3. **从 cost 到 economics**：不只省 GPU，而是把字节、带宽、保留时间变成可预算、可归因的资源。

### 0.1 什么是 Fabric

Fabric 不是某个协议或产品，而是一层把分散资源统一访问与调度的抽象：

- 应用不用关心资源在哪、怎么连、有几份；
- Fabric 负责连接、寻址、放置、复制、迁移、隔离、QoS、可观测；
- 资源可以横向扩、异构混部、跨域，但对上层表现为一个池。

| Fabric | 把什么织在一起 | 典型场景 |
|---|---|---|
| **Network Fabric** | 交换机/网卡/链路 | leaf-spine、IB/RoCE、any-to-any 低延迟 |
| **Storage Fabric** | 主机与存储阵列/卷 | SAN、NVMe-oF、多路径、QoS |
| **Data Fabric** | 多个数据源/湖/仓 | 数据虚拟化、目录、治理、跨域查询 |
| **Memory Fabric** | 多机内存 | CXL 内存池、disaggregated memory |
| **Service Fabric** | 微服务实例 | service mesh：流量、mTLS、重试、可观测 |
| **GPU Fabric** | 多机 GPU/NPU | NVLink/NVSwitch、NCCL、跨节点集合通信 |
| **KV Fabric** | 多实例/多介质/多集群的 KV | 会话记忆、热点多副本、跨区复制、KV FinOps |

判断标准：

- 只是单机/单实例的缓存优化 → 叫 cache 就好；
- 资源**分布、可池化、需要移动/复制/隔离/计价** → 才配叫 Fabric；
- Fabric 的价值在“统一控制面 + 数据面 + 策略”，不是多画一层架构图。

本文的 KV Fabric 指：**全局前缀视图 + 放置/副本控制 + 数据面 copy/move + 生命周期/配额/账本**。它是内部资源层，不是应用 API。

### 0.2 KV Fabric 不是什么

**它既不是一个组件，也不是第三个平面。**

它是「管理面 + 控制面」合起来的终局形态：管理面把请求/策略变成放置决策，控制面执行。它没有自己的代码，能力全在管理面（决策）和控制面（执行）里。

它也不是：

- 应用级 KV 数据库；
- 应用可寻址的 `kv://` 对象存储；
- 应用订阅 KV 事件的系统；
- 跨租户的 raw KV 市场。

### 0.3 应用契约：文字/消息进，系统内部 tokenize

应用不需要 token，也不需要 KV 句柄：

```text
应用：messages / text，可选 session_id，可选 hook
后端：tokenizer + chat template → token IDs → block hash → APC/KVT
```

正常路径：

```text
Request(messages)
→ 后端 tokenize
→ 引擎算 hash
→ 命中 APC/KVT
→ 没有则重算/从池读
```

Agent 的分支就是消息：

```text
[history] + "方案 A"
[history] + "方案 B"
[history] + "方案 C"
```

共享前缀由引擎按 hash 命中；分支身份在应用/会话层，不需要 KV branch handle。

**注意：同样的文字不一定产生同样的 token。** 下面任何一项变化都会导致前缀不同：

- tokenizer 版本；
- chat template；
- system prompt 的顺序/格式；
- tool schema 的序列化；
- special tokens；
- 模型 revision / LoRA / adapter；
- 多模态输入的 hash；
- cache salt / 租户隔离。

所以 KV 的 namespace 至少要带：

```text
model_id + revision + tokenizer/template version
```

应用不需要知道这些；后端必须确定、版本化。

### 0.4 做什么 / 不做什么

| 做 | 不做什么 |
|---|---|
| 放置：copy/move/多副本/迁移 | 应用级 KV branch handle |
| 生命周期：lease/TTL/demote/promote/evict | 应用级 `kv://` 名字寻址 |
| 配额/隔离/审计 | 应用级 KV 订阅 |
| 账本/ROI/FinOps | 应用级 `Probe` 作为核心 API |
| 容量规划/HA/跨集群（后续） | raw KV 市场 |

为什么不做这些：

- **分支**：真实表示就是 messages；应用发不同消息即可，不需要 KV 句柄；
- **名字寻址**：应用发文字，后端 tokenize 后自然有 hash；`kv://` 只对“应用不想重发历史/共享 prompt”的产品有意义，不是 Fabric 核心；
- **Probe**：`present` 到请求到达之间可能被驱逐，应用拿到也不能做确定性决策；只适合内部诊断；
- **订阅**：系统内部已有 kv event，SC/索引消费即可；暴露给应用只会增加耦合；
- **raw KV 市场**：KV 是模型相关、易失的，不适合作为跨租户商品；共享 prompt 是另一个产品层；
- **Probe/订阅**：只保留内部诊断/SC 消费，不作为应用 API。

如果未来做“服务端会话状态”产品，ID 是 **conversation/response ID**，映射到 messages/状态，不是 raw KV 句柄。

### 0.5 有没有做成：三件判据

不看有没有一个叫 Fabric 的进程，只看三件事：

| 判据 | 现在 |
|---|---|
| 应用不指定实例：只发请求/约束，系统决定放哪 | 部分：SC 能选实例，但放置（copy/move/retain）大多还要人工/策略触发 |
| 决策是自动的：放哪、几份、保多久、何时撤，由系统算 | 只有 D8 设计稿，无完整实现 |
| 执行是完备的：八项操作都能通过 intent 触发 | 只有 copy 一条路走通 |

三件都到位之前，说的都是“KV 缓存 + 一个聪明的控制面”，不是 Fabric。

---

## 1. 为什么是现在

- **Agent workload 把 KV 变成长期状态**：多轮复用高、工具调用停顿长；长上下文模型（256K–1M）下 resume 一次可能要重算数十万 token，DeepSeek V3.2 这类胖 KV 单请求就是几十 GB。
- **GPU 贵、带宽/内存相对便宜**：用 KV 放置换 GPU 时间，ROI 通常成立；但“无限复制/无限保留”会反噬，所以需要 economics。
- **已有控制面**：kvcm 已能 copy/move/keepalive/pin/demote/promote/retain/remove；D8 已有管理面决策；缺的是**产品化/价值闭环**，不是再写一个 RPC。
- **开源组件只覆盖零件**：Conductor/Dynamo indexer 做索引，Mooncake 做池/副本，LMCache/Prometheus 做指标；**跨层放置、生命周期、计价治理**没有成品。

---

## 2. 价值金字塔

```mermaid
flowchart TB
    L4["L4 新商业<br/>KV FinOps / QoS"] --> L3["L3 新体验<br/>Agent 会话保留/恢复"]
    L3 --> L2["L2 省传输/负载<br/>热点多副本 / 迁移 / 负载整形"]
    L2 --> L1["L1 省算力<br/>命中 / 保留 / 预取"]
    L1 --> V["价值底座<br/>TTFT ↓ / GPU 利用率 ↑ / 成本 ↓"]
```

- **L1** 是“别重算”：命中/保留/预取；
- **L2** 是“别挤在一个点”：热点前缀多副本、迁移、会话分散；
- **L3** 是“以前做不到的体验”：Agent 会话保留/恢复、跨 Agent 共享前缀（不是分支句柄）；
- **L4** 是“能卖钱/能管钱”：KV FinOps、QoS 等级、按记忆/会话计费。

---

## 3. 能做的方向

| 方向 | 做什么 | 谁买单 | 价值指标 | 难度 | 时机 |
|---|---|---|---|---|---|
| **A. KV 负载整形** | 热点前缀 copy 多副本；按负载/会话分流；副本数按收益定 | 平台/推理团队 | p99 TTFT、吞吐、扩容次数 | 中 | **现在** |
| **B. Agent 会话保留/恢复** | hook 提前预取/保留；恢复时本机读；不做 branch handle | Agent 产品/平台 | resume 时延、任务成功率、单任务成本 | 中 | **现在–1 年** |
| **C. KV FinOps / QoS** | 按模型/租户/会话记账；预算/配额；QoS 等级 | 云/平台/财务 | 成本可见性、预算达成、毛利 | 中 | **现在–1 年** |
| **D. 容量数字孪生** | 用历史 trace 模拟容量/配比/硬件；采购前 what-if | 基础设施/采购 | 少买/少闲置、容量预测误差 | 中 | 6–18 月 |
| **E. KV HA / 热备 / DR** | 预复制热前缀到备用实例；故障快速接管；跨 AZ | SRE/平台 | MTTR、可用性、恢复命中率 | 中高 | 6–18 月 |
| **F. 跨集群 KV Fabric** | 跨集群/地域复制热前缀；follow-the-sun；数据驻留 | 全球化平台 | 跨区 TTFT、合规、DR | 高 | 1–3 年 |
| **G. KV 存储效率** | 去重/压缩/量化/分层；共享 system prompt/tools | 平台/基础设施 | 每 GB 有效 KV、HBM 占用 | 高 | 1–3 年 |

明确不做：

- 不把 branch/checkpoint 做成 KV 句柄；分支用 messages，checkpoint 用消息历史 + APC；
- 不做应用级 `kv://`；
- 不做应用级订阅/Probe；
- 不做 raw KV 市场。

如果未来做“共享 prompt registry”，名字是 prompt 版本，不是 raw KV；那是另一个产品层。

---

## 4. 三阶段与三个下注

```mermaid
flowchart LR
    H1["H1（0–6 月）<br/>省：负载整形 + 会话保留 + FinOps 最小集"] --> H2["H2（6–18 月）<br/>稳/省：容量孪生 + HA/DR + QoS"]
    H2 --> H3["H3（18 月+）<br/>新：跨集群 + 存储效率"]
```

### H1：把“省”做成可证明的 ROI

- 热点前缀多副本 + 会话分散；
- Agent 会话保留/恢复（hook 预取 + retain）；
- 账本/ROI：每会话、每前缀、每模型；
- 目标：**TTFT p99 降、GPU 时间省、扩容次数降**，用 A/B 证明。

### H2：把“稳/省”做成平台能力

- 容量数字孪生：采购/配比 what-if；
- KV HA/热备：故障快速接管、跨 AZ；
- QoS：租户/模型等级、预算、抢占；
- 目标：**可用性、容量利用率、成本可预测**。

### H3：把新体验做出来

- 跨集群 KV Fabric：地域跟随、数据驻留、DR；
- KV 存储效率：去重/压缩/分层；
- 目标：**跨区/大规模场景**，而不是再省一点本地成本。

### 如果只选三个

1. **KV 负载整形**：直接打高负载 TTFT/吞吐，最容易证明 ROI；
2. **Agent 会话保留/恢复**：真实 Agent 需求，不需要发明 branch handle；
3. **KV FinOps / QoS**：让平台能卖、能管、能扩。

合起来就是：**“KV 既能省，又能管，还能扩。”**

---

## 5. 收益模型

省算力、省容量、负载整形的详细公式见 [D8a §8](D8a-scheduling-algorithms.md)。这里只写 D10 特有的三条。

### 5.1 FinOps

FinOps = 把 KV 的字节、带宽、保留时间做成**可见、可归因、可预算、可计价**的单位经济效益；不是账单系统。

```text
单会话成本 = Σ(bytes × T × c_mem) + Σ(transfer × c_bw) + prewarm 成本
毛利 = 收入 − 单会话成本
```

价值不在算得准，在**能按会话/租户/模型定价、预算、追责**。

### 5.2 可用性

```text
MTTR 降低 = 故障恢复时间（冷启动/重建） − 热备接管时间
```

热备 = 预复制 + 保留 + 路由切换；不是备份文件，是可读 KV。

### 5.3 负载整形 / 会话 resume 的一阶判断

```text
负载整形值得 ⟺ P_reuse × T_prefill × c_gpu > transfer + 副本驻留成本

resume 值得 ⟺ P_resume × T_prefill × c_gpu > retain + transfer
```

两个式子都只看“省下的重算”是否大于“复制/保留/传输成本”；具体数值用实测代，不要用拍脑袋常数。

---

## 6. 竞争定位

链式 block hash、逐块匹配、事件→索引、推测索引补空窗、介质分层打分，都是 SOTA 的标准做法（llm-d `pkg/kvcache/`、Dynamo `lib/kv-router/`、AIBrix `pkg/kvevent/`）。差异不在这些。

| 维度 | SOTA（llm-d / Dynamo / AIBrix） | 我们 |
|---|---|---|
| **索引与权威** | 引擎事件的派生缓存：靠事件逼近真相，丢事件要 gap replay，过期要 TTL/推测补 | 同样是派生缓存，但以池 master 为权威；正常读缓存，miss 按无命中处理，异步对账修索引，不在 L0 热路径查 master |
| **放置的主动权** | 只做路由：谁有 KV 就发给谁 | 控制面能改变放置：copy 多副本、move 迁移、retain |
| **视图范围** | 只到“哪个 pod 有这个块 + 哪层介质” | 到实例 + 介质，另加生命周期/账本；不碰 seg/rank/字节 |

真正的差异：**不是“缓存 vs 直连”，而是“只路由” vs “能改变放置”。**

| 能力 | SOTA | 我们 |
|---|---|---|
| 前缀索引/路由 | ✅ | ✅ |
| 池内 copy/move | 部分（P2P 补传） | ✅ |
| 热点多副本（指定目标/副本数/预算） | 部分（master 选目标） | **指定目标/数量/预算** |
| 会话保留/恢复 | 部分（引擎内） | ✅ 跨实例 |
| FinOps/QoS | 部分指标 | 目标 |
| 跨集群 | 部分（DFS/跨区） | 远期 |
| 跨引擎 | 部分 | 控制面独立 |

llm-d 自己的 pain-point 说明：只靠事件索引会留幽灵条目、需要推测索引补“决策→确认”空窗。我们要补的是**放置主动权 + 生命周期/账本**，不是在索引精度上再卷一层。

---

## 7. 风险与非目标

| 风险 | 说明 | 对策 |
|---|---|---|
| 复制成本反噬 | 多副本吃 HBM/带宽，可能降低全局命中 | 收益/预算约束；副本 TTL；全局水位 |
| 单会话不可拆 | 复制前缀不能拆 decode | 明确边界；未来做请求迁移 |
| 引擎/池绑定 | 不同引擎 key/事件不同 | 控制面适配层；KeyComputer 隔离 |
| tokenizer/template 漂移 | 同样文字产生不同 token，前缀不命中 | namespace 带 model/revision/tokenizer/template；固定模板 |
| 数据合规 | KV 含 prompt/PII | 保留策略/审计/加密/地域 |
| 开源组件成熟度 | publisher/retain 等能力版本不一 | 能力声明；降级方案 |
| 组织边界 | 谁拥有 KV 预算/QoS | 平台/租户/FinOps 分工 |

非目标：

- 应用级 KV API（branch handle / `kv://` / 订阅 / Probe）；
- raw KV 市场；
- 第三个平面；
- 让应用管理 KV 生命周期。

---

## 8. 参考实现

| 参考 | 锚点 | 借什么 | 差异 |
|---|---|---|---|
| Dynamo | `protocols.rs::WorkerSelectionResult`；Planner | KV-aware 路由；双环扩缩 | 我们改放置，不只路由 |
| llm-d | EPP + P2P KV sharing | 索引/推测/P2P | 我们做主动多副本/会话/计价 |
| AIBrix | `pkg/kvevent/manager.go`；PodAutoscaler | 事件索引；KV 指标扩缩 | 我们跨层放置 |
| Mooncake | `CreateCopyTask`/`CreateMoveTask`/`CreateDrainJob` | 池操作/副本/drain | 我们指定目标/数量/预算 |
| LMCache | `observability.py::LMCStatsMonitor` | 字节/指标 | 我们做归因/FinOps |
| vLLM APC | `block_pool.py::get_computed_blocks`；`hash_block_tokens` | 前缀 hash/命中 | 我们做跨实例放置，不重写引擎 |

**本项无直接参考实现**：应用级 KV 订阅/`Probe`/`kv://` 句柄。我们判断这些不是 Fabric 核心，不做；如果未来做服务端会话状态产品，ID 是 conversation/response ID，不是 raw KV。
