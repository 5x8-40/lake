# 分布式模型对比

> 本文汇总对比各 3rdparty 参考项目的分布式模型：拓扑形态、元数据权威、同步机制、一致性分级、HA 与扩展性。
> 各项目详节见对应 overview 的「分布式模型」节（§5 有链接）；lake 自身模型见 [`../architecture/consistency.md`](../architecture/consistency.md) 与 [`../architecture/control-plane.md`](../architecture/control-plane.md)。

## 0. 锚点：lake 的分布式模型

一句话：星型拓扑，索引强一致，数据最终一致。

- **拓扑**：中心化 CP（存储控制面）+ 每节点 agent。agent 管本机物理资源（L0 slot、free-list、本地引用计数），本地权威；全局账本（radix、位置视图、配额）权威在 CP。
- **同步**：agent 热路径写本地 overlay，满块注册与位置变化批量异步上报 CP；CP 权威变更经 gRPC stream 推送镜像给 Router/agent，热路径零 RPC。
- **一致性**：写入传播最终一致；提交点之后 CP 单写者线性一致，任何时刻可同步查询得到确定结果。数据面 KV 字节写一次读多次。理论定位见 [`../architecture/consistency.md`](../architecture/consistency.md) §8（目录一致性 + 释放一致 + flat disaggregated memory）。
- **HA**：CP 内存权威 + etcd 降频 checkpoint 重建；L2 NVMe 为 F4 恢复点，L3 为 SSOT。

## 1. 统一对比维度

| 维度 | 含义 |
|------|------|
| 拓扑 | 星型（中心 CP）/ P2P / 事件流 / 无中心 |
| 元数据权威 | 谁维护"什么在哪"的确定答案；单点 / 分片 / 无 |
| 同步机制 | 节点间状态如何传播：推送 / 拉取 / 事件流 / 周期快照 / 每次查询权威 |
| 一致性（索引） | 位置与前缀索引是否可同步查询到确定结果 |
| 一致性（数据） | 字节读写的不可变性、事务、可见性窗口 |
| HA 与故障 | 中心故障与节点故障各自的语义 |
| 扩展性瓶颈 | 规模增长的首要限制因素 |

## 2. 总表

| 项目 | 拓扑 | 元数据权威 | 同步机制 | 索引一致性 | 数据一致性 | HA | 扩展性瓶颈 |
|------|------|-----------|----------|-----------|-----------|-----|-----------|
| **lake** | 星型（CP+agent） | CP 单写者内存 + etcd 降频 checkpoint | 异步上报 + 推送镜像 + 同步回查 | **强一致**（线性一致查询） | 写一次读多次，最终一致传播 | CP 内存 + etcd 重建；L2 恢复点 / L3 SSOT | CP 单机内存（分片租约为备选） |
| Mooncake store | 星型（Master+Client） | leader 内存 1024 shard | 每次 RPC 查询权威 | 强一致（leader 单写者） | 对象 immutable + 两阶段写 + lease 5s | etcd/redis/k8s 选主 + OpLog + fork 快照 | leader 单机内存 |
| MemCache | 星型（Meta+Local，节点贡献 HBM/DRAM） | MetaService 集中 | RPC 查询 Meta | 集中但 HA 弱 | exact-key 对象 | K8s 多活 + Lease，尽力而为 | Meta 集中 |
| TensorCast | 星型（GS+Daemon），高基数分片 | 低基数：GS 集中；高基数：分片宿主 + 租约 | GS 查询 + worker 缓存租约 + 心跳对账 | 低基数强 / 高基数分片最终一致 | artifact immutable（MI2 双哈希） | 低基数持久副本；高基数租约过期重建（窗口不可用） | GS 单点（开源版）；分片宿主故障窗口 |
| Dynamo | 编排星型 + 事件流 | 无全局位置权威；discovery 走 etcd | NATS 事件流（best-effort，可丢） | 最终一致（事件副本） | 引擎各自负责 | canary + 在途迁移；etcd lease 收敛 | 索引最终一致，误判以重算兜底 |
| SGLang | 实例自治 + 共享 L3 + 旁路索引 | 无（每实例树 + L3 实时查后端） | kv_events（zmq）+ gateway 近似树 | 无全局；L3 最终一致 | 后端决定 | 无（崩溃由 L3 回填） | 跨实例命中依赖近似树或实时查询 |
| vLLM | 单实例引擎 + 事件外发 | 引擎内强一致；集群级无 | KV Events（zmq 单向） | 集群级最终一致（外部索引） | 引擎内保证 | worker 有状态，崩溃丢 KV | 集群语义外挂；#48501 向控制面演进 |
| LMCache | 共享存储 / 中心 controller 两式 | controller RegistryTree（best-effort） | 心跳 + 序列号 + full sync（0.8 阈值） | 弱（允许错误，周期收敛） | 内容寻址去重；无锁并发写 | controller 无 HA；daemon 无 fate-sharing | O(n²) lookup；全内存 RegistryTree |
| FlexKV | 本机自治 + Redis 快照 | 本机 radix；Redis GMS 快照非权威 | 周期 upload/rebuild + lease | 最终一致（快照可陈旧） | 后端决定 | 无；worker 退出索引失效 | 快照时效；无回查权威 |
| UCM | 无自有（继承 store 后端） | 后端决定 | 后端决定 | 后端决定 | 后端决定 | 后端决定 | 后端决定 |
| TileRT | 点对点（单槽 PD） | 无（进程内忙闲表） | 一次性握手 | 不适用 | 不适用 | 无（429） | 不适用（专用 bs=1） |
| mooncake-p2p-store | P2P 无中心 | etcd | BitTorrent 式 register/拉取 | etcd 强一致（元数据） | 分片拉取 | etcd | 适合 checkpoint 分发，非热路径 |

## 3. 四类归纳

### A. 中心权威星型（lake / Mooncake / MemCache / TensorCast 低基数）

共同特征：单一中心维护"什么在哪"，节点贡献内存或存字节，数据面绕过中心直传。
主要分野在读取路径：Mooncake / MemCache 每次 RPC 查询权威（实现简单，中心承担 QPS 压力）；lake 推送镜像 + 误判回查（热路径零 RPC，实现复杂度高）。
共同瓶颈：中心单机内存需容纳全量元数据。Mooncake 的 1024 shard leader 与 lake CP 同构；TensorCast 以高低基数分治回避（见 B）。

### B. 分片租约（TensorCast 高基数）

全局元数据按分片外包给分片宿主，GS 仅维护 N 条租约；HRW 排名 + fencing token 防脑裂。
优点是元数据扩展性；代价是读者可能读到过期租约缓存，故障分片在租约过期前不可用。
对 lake：CP 元数据规模接近单机上限时的备选路径；当前 lake 选择单点权威，保证任何时刻可同步查询（D-direct 5ms 预算）。

### C. 事件流 / 快照最终一致（Dynamo / vLLM / SGLang / FlexKV）

共同特征：没有可同步查询的权威；通过事件推送（NATS / zmq）或周期快照（Redis GMS）维护最终一致索引，误判以重算、重试或回填兜底。
采用该方案的原因：高频位置写不适合进入强一致存储（Dynamo 将 KV 事件从 etcd 移至 NATS 为一例）。
代价：索引陈旧为常态。SGLang PP×L3 多树发散（#22607）是弱一致多副本协调成本的实例；FlexKV 在 worker 退出后索引失效，存在恢复缺口。
lake 的处理：同样不将高频写压入 etcd，但权威保留在 CP 内存；事件流仅用于镜像传播，不作为权威本身。

### D. 弱协调 / 无（LMCache / UCM / TileRT）

- LMCache：有中心 controller，但设计为 best-effort（心跳 + full sync 0.8 阈值），lookup O(n²) 为已知瓶颈，属协调器而非权威。
- UCM：框架自身不含分布式语义，全部继承后端；语义强弱取决于所挂 store。
- TileRT：bs=1 专用引擎，无共享状态，不涉及一致性问题。

## 4. 对 lake 的参考结论

**支持现有设计的证据**：

1. etcd 不适合承载高频位置写——Dynamo（事件走 NATS）与 Mooncake（etcd 仅用于选主与 OpLog）均采用此分工，与 lake"权威在 CP 内存、etcd 降频 checkpoint"一致。
2. 中心内存权威在生产环境可行——Mooncake leader 以 1024 shard 内存元数据 + 快照/OpLog 做 HA。
3. 节点贡献内存进池已有生产实例——MemCache LocalService 贡献 HBM/DRAM，与 lake agent 模型同形。
4. 镜像 + 回查是必要的——SGLang 近似树、LMCache best-effort 同步、FlexKV 周期快照各自的代价（误判、陈旧、恢复缺口）说明需要保留可同步查询的权威。

**需要关注的风险**：

1. 中心规模上限——Mooncake 与 lake 同构：全量元数据位于单机内存。TensorCast 分片租约是已验证的备选，但引入过期读与故障窗口；建议在 CP 元数据规模接近单机上限时再评估，不预设分片。
2. 弱一致多副本的协调成本——SGLang PP×L3 多树发散需要 gloo all_reduce 与定向广播补偿；lake 单写者位置视图不涉及该问题。
3. 事件流可丢——Dynamo approximate mode 允许丢事件；lake 镜像推送需要序号 + gap replay（已入设计），且正确性不依赖镜像（回查兜底）。

## 5. 各项目分布式详节

- [sglang/overview.md](sglang/overview.md)「分布式模型」：实例自治 + 旁路索引 + 双层选路
- [mooncake/overview.md](mooncake/overview.md)「分布式模型」：星型 Master/Client + 控制流数据流分离
- [lmcache/overview.md](lmcache/overview.md)「分布式模型」：best-effort controller + 共享存储
- [vllm/overview.md](vllm/overview.md)「分布式模型」：单实例引擎 + KV Events 外发
- [dynamo/overview.md](dynamo/overview.md)「分布式模型」：事件流编排 + etcd/NATS 分离
- [memcache/overview.md](memcache/overview.md)「分布式模型」：Meta/Local 星型（节点贡献 HBM）
- [ucm/overview.md](ucm/overview.md)「分布式模型」：无自有控制面，继承后端
- [tensorcast/overview.md](tensorcast/overview.md)：GS+Daemon 星型 + 高低基数分片租约（含与 lake 一致性权威的详细对照）
- [flexkv/overview.md](flexkv/overview.md)「分布式模型」：本机 radix + Redis 周期快照
- [tilert/overview.md](tilert/overview.md)「分布式模型」：无（单槽点对点）
