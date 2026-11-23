# Tair KVCM 与 lake kvcm：架构对照

> Tair 源码:`3rdparty/tair-kvcache` @ `934c03dc`。lake 侧:D8/D9 目标形态 + 当前 kvcm 实现。
> 上游文章:[Tair KVCache Manager](https://mp.weixin.qq.com/s/KyNYH03PfamPcAJY7BRMwg)；系统细节见 [`overview.md`](overview.md)。

## 0. 先回答那个直觉

“Tair KVCM + Mooncake ≈ lake kvcm + Mooncake，lake 多路由和扩缩容”这个判断，**在产品层大致成立，在架构层不成立**。

- **成立的部分**：两者都是“中心元数据 + 共享 KVCache 存储 + 客户端直连数据面”；都可以把 Mooncake 当存储后端；都不在数据路径上搬字节。
- **不成立的部分**：
  - Tair KVCM 的 `MetaIndexer` 是**自己的位置/状态权威**；
  - lake 的副本权威在 **Mooncake master**，kvcm 是 client/facade；
  - Tair 的复用边界是 **Instance**，不是跨 Instance 的全局池；
  - lake 的增量不只是“路由和扩缩容”，而是**请求 + KV + 扩缩联合管理面**；
  - 两者与引擎的耦合方式不同。

所以更准确的说法是：

> **Tair KVCM 是一个完整的、自己持有全局索引的 KV 元数据服务；lake kvcm 是 Mooncake master 之上的 client/facade，外加一层 D8 管理面设计。**

## 1. 权威在哪

| | Tair KVCM | lake kvcm |
|---|---|---|
| 位置/状态权威 | Tair KVCM `MetaIndexer`：Block → CacheLocation → 状态 | Mooncake master：副本在哪、是否可读；kvcm 不拥有副本权威 |
| 写确认 | Tair 自己维护 `writing → serving` 两阶段状态 | Mooncake `PutStart/PutEnd` + kvcm READY/核验 |
| 删除/驱逐 | Tair `CacheReclaimer` / GC 决定元数据回收；存储后端执行 | master/池执行；kvcm 发 intent、核验、记账 |
| KVT/目录 | Tair 的索引就是服务端权威索引 | lake KVT 是派生视图；master 才是副本权威 |

这是最根本的区别：**Tair 的“目录”是权威，lake 的 KVT 不是。**

## 2. 全局视野覆盖到哪

| | Tair KVCM | lake kvcm |
|---|---|---|
| 复用边界 | **同一 Instance 内，跨节点**；跨 Instance 不复用 | 目标是一个池内跨模型/租户的全局 KV 池 |
| 能看到什么 | Block 的前缀关系、CacheLocation、L1.5/L2 上报、存储水位、配额 | KVT：块 → 实例 × 介质 × 置信度；master 副本事实；pool 事件 |
| 看不到什么 | 请求队列、SLO、实例角色负载、扩缩决策 | 引擎 HBM/APC 细节（只能靠事件/壳请求）；不能直接读 HBM |

Tair 的“全局”是 **Instance 级全局**；lake 目标是 **池级全局**。这两个“全局”不是同一个范围。

## 3. 全局管理管什么

| | Tair KVCM | lake kvcm |
|---|---|---|
| KV 匹配 | prefix、reverse rolling sliding-window、hybrid attention、batch query | KVT 前缀链匹配 + 候选过滤；不做存储侧 hybrid attention 匹配 |
| 写入 | 两阶段写、Location 状态机 | Mooncake 两阶段写 + kvcm READY/核验 |
| 容量 | Instance Group 配额、DRAM/SSD 水位、GC、迁移 | D9 配额/账本/容量建议；D8 预取/保留/迁移 |
| 多租户 | Instance Group 配额、fair reclaim | 目标在 D8/D9；当前 kvcm 更薄 |
| HA | 一主多备、Leader 选举、客户端自动发现 | master 权威；kvcm 当前单实例，HA 未定 |
| 观测 | Prometheus/KMonitor、RequestContext、crash handler、optimizer | D9 对账/账本/ROI；当前在补 |

Tair 在 **KV 元数据服务本身**上更完整；lake 在 **请求/KV/扩缩联合管理**上有目标设计，但当前实现更薄。

## 4. 是否做请求 + KV + 扩缩联合调度

| | Tair KVCM | lake kvcm |
|---|---|---|
| 请求路由 | 不做；Client 的 SCHEDULER 只做元数据匹配/写地址申请，不是模型 router | D8 L0 目标：请求选路 |
| PD 调度 | 不做；没有在线 PD 配比规划 | D8 L2 目标：P/D 扩缩与角色规划 |
| 扩缩 | 不做在线 autoscaling；optimizer 做容量/策略分析 | D8 目标：容量环 + 外部编排执行 |
| 联合控制 | 无：KV 元数据与请求调度解耦 | 目标：请求/KV/容量共享 KVT、预算、账本 |

所以“lake 多路由和扩缩容”只说对了一半：**lake 的差异是管理面把请求、KV、容量放进同一套决策**，不是“多两个功能”。

## 5. 耦合方式

| | Tair KVCM | lake kvcm |
|---|---|---|
| 引擎接入 | 提供 vLLM/SGLang/RTP-LLM/TRT-LLM connector + C++/Python/Java client | 目标是 backend-coupled；引擎通过 connector 接入池，kvcm 自身不绑某个引擎 |
| 存储接入 | 多后端（TairMemPool / HF3FS / NFS / Mooncake） | Mooncake 为主；kvcm 是 master 的 client |
| 耦合点 | 引擎 connector + 客户端 SDK | 池/backend + D8 管理面 |
| 是否要求引擎改 | 需要接入 connector；不是透明旁路 | 需要引擎接入池 connector；kvcm 不要求引擎为它改协议 |

Tair 不是“纯 backend-coupled”；它在引擎侧有明确的 connector/client 形态。  
lake kvcm 的“backend-coupled”是指不把自己绑到某个引擎插件上，但不等于引擎零接入。

## 6. 数据面

| | Tair KVCM | lake kvcm |
|---|---|---|
| 谁搬字节 | Client `TransferClient` 直连存储后端 | Mooncake transfer / client；kvcm 不搬 |
| KVCM 在数据路径上吗 | 不在 | 不在 |
| 位置表达 | CacheLocation + LocationSpec URI | master 副本 + kvcm intent/locate |
| 写路径 | StartWrite → 写 → FinishWrite | PutStart/PutEnd + READY/核验 |
| 数据面调度 | 引擎/TransferClient 自行选择；KVCM 只给位置 | D8 可做预取/放置；执行仍归池 |

两者都做到了“元数据/数据分离”，但 Tair 的元数据服务自己就是位置权威，lake 的 kvcm 不是。

## 7. 和 LMCache coordinator 的关系

LMCache coordinator 是另一条线：

- 事件派生的 L1/L2 目录；
- 可选、off request path；
- 不做请求调度；
- 当前 P2P 取数仍遍历 MP server。

Tair KVCM 比 LMCache coordinator 更“服务端权威”：自己持有索引、两阶段写、配额、HA。  
lake kvcm 则更接近“Mooncake master 的 facade + 管理面”，不是 Tair 那种独立权威服务。

## 8. 结论

1. **Tair KVCM 更像一个完整的 KV 元数据服务**：索引、匹配、两阶段写、配额、分层、GC、HA 都在服务内。
2. **lake kvcm 更像 Mooncake master 之上的 client/facade + D8 管理面**：副本权威不在自己手里，增量在请求/KV/扩缩联合。
3. **“Tair KVCM + Mooncake ≈ lake kvcm + Mooncake”只在“中心元数据 + 共享池 + 客户端直连”这一层成立**；权威、复用边界、耦合方式、管理范围都不同。
4. 如果只比较“谁更像一个 KV 目录服务”，Tair 更完整；如果比较“谁在往 KV Fabric 走”，lake 的目标更彻底。

一句话：

> **Tair KVCM 是“KV 元数据服务 + 多后端数据面”；lake kvcm 是“池 client/facade + 请求/KV/扩缩管理面”。两者不是同一层的替代品。**
