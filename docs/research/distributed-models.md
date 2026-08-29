# 分布式模型对比 — 各参考项目怎么处理"大规模"

> 本文汇总对比各 3rdparty 参考项目的**分布式模型**：拓扑形态、元数据权威、节点同步机制、一致性分级、HA 与扩展性。
> 各项目的分布式详节见对应 overview 的「分布式模型」节（本文 §5 有链接）；lake 自身模型见 [`../architecture/consistency.md`](../architecture/consistency.md) 与 [`../architecture/control-plane.md`](../architecture/control-plane.md)。

## 0. 锚点：lake 的分布式模型

一句话：**星型 + 索引强一致 + 数据最终一致**。

- **拓扑**：中心化 CP（存储控制面）+ 每节点 agent（池的本地代理）。agent 管本机物理资源（L0 slot / free-list / 本地引用计数，本地权威）；全局账本（radix / 位置视图 / 配额）权威在 CP。
- **同步**：agent 热路径写本地 overlay，满块注册 / 位置变化**批量异步上报** CP；CP 权威变更**推送镜像**（gRPC stream）给 Router / agent，热路径零 RPC 决策。
- **一致性**：写入传播最终一致，但**提交点之后 CP 单写者线性一致**——任何时刻能花一次同步 RPC 问到底；数据面（KV 字节）写一次读多次、最终一致传播。理论定位 = 目录一致性 + 释放一致 + flat disaggregated memory（[`../architecture/consistency.md`](../architecture/consistency.md) §8）。
- **HA**：CP 内存权威 + etcd 降频 checkpoint 重建；L2 NVMe = F4 恢复点，L3 = SSOT。

## 1. 统一对比维度

| 维度 | 问的是什么 |
|------|-----------|
| 拓扑 | 星型（中心 CP）/ P2P / 事件流 / 无中心？ |
| 元数据权威 | 谁拥有"什么在哪"的确定答案？单点 / 分片 / 无？ |
| 同步机制 | 节点间怎么知道彼此状态？推 / 拉 / 事件流 / 周期快照 / 每次问权威？ |
| 一致性（索引） | 位置/前缀索引：强一致（可问到底）还是最终一致（可能是旧账）？ |
| 一致性（数据） | 字节读写：immutable / 事务 / 可见性窗口？ |
| HA 与故障 | 中心死了怎么办？节点死了丢什么？ |
| 扩展性瓶颈 | 规模上去先撞哪堵墙？ |

## 2. 总表

| 项目 | 拓扑 | 元数据权威 | 同步机制 | 索引一致性 | 数据一致性 | HA | 扩展性瓶颈 |
|------|------|-----------|----------|-----------|-----------|-----|-----------|
| **lake** | 星型（CP+agent） | CP 单写者内存 + etcd 降频 checkpoint | 异步上报 + 推送镜像 + 同步回查 | **强一致**（线性一致查询） | 写一次读多次，最终一致传播 | CP 内存 + etcd 重建；L2 恢复点 / L3 SSOT | CP 单机内存（分片租约为备选） |
| Mooncake store | 星型（Master+Client） | leader 内存 1024 shard | 每次 RPC 问权威 | 强一致（leader 单写者） | 对象 immutable + 两阶段写 + lease 5s | etcd/redis/k8s 选主 + OpLog + fork 快照 | leader 单机内存 |
| MemCache | 星型（Meta+Local，节点贡献 HBM/DRAM） | MetaService 集中 | RPC 问 Meta | 集中但 HA 弱 | exact-key 对象 | K8s 多活 + Lease，尽力而为 | Meta 集中 |
| TensorCast | 星型（GS+Daemon），高基数分片 | 低基数：GS 集中；高基数：分片宿主 + 租约 | GS 查询 + worker 缓存租约 + 心跳对账 | 低基数强 / 高基数分片最终一致 | artifact immutable（MI2 双哈希） | 低基数持久副本；高基数租约过期重建（窗口不可用） | GS 单点（开源版）；分片宿主故障窗口 |
| Dynamo | 编排星型 + 事件流 | 无全局位置权威；discovery 走 etcd | NATS 事件流（best-effort，可丢） | 最终一致（事件副本） | 引擎各自负责 | canary + 在途迁移；etcd lease 收敛 | 索引"大概准"，误判靠重算 |
| SGLang | 实例自治 + 共享 L3 + 旁路索引 | 无（每实例树 + L3 实时查后端） | kv_events（zmq）+ gateway 近似树 | 无全局；L3 最终一致 | 后端决定 | 无（崩溃靠 L3 回填） | 跨实例命中靠猜/RPC；PP×L3 多树发散 |
| vLLM | 单实例引擎 + 事件外发 | 引擎内强一致；集群级无 | KV Events（zmq 单向） | 集群级最终一致（外部索引） | 引擎内保证 | worker 有状态，崩溃丢 KV | 集群语义外挂；#48501 向控制面演进 |
| LMCache | 共享存储 / 中心 controller 两式 | controller RegistryTree（best-effort） | 心跳 + 序列号 + full sync（0.8 阈值） | 弱（可错，周期收敛） | 内容寻址去重；无锁并发写 | controller 无 HA；daemon 无 fate-sharing | O(n²) lookup；全内存 RegistryTree |
| FlexKV | 本机自治 + Redis 快照 | 本机 radix；Redis GMS 快照非权威 | 周期 upload/rebuild + lease | 最终一致（快照可陈旧） | 后端决定 | 无；worker 死索引没 | 快照时效；无回查权威 |
| UCM | 无自有（继承 store 后端） | 后端决定 | 后端决定 | 后端决定 | 后端决定 | 后端决定 | 后端决定 |
| TileRT | 点对点（单槽 PD） | 无（进程内忙闲表） | 一次性握手 | 不适用 | 不适用 | 无（429） | 不适用（专用 bs=1） |
| mooncake-p2p-store | P2P 无中心 | etcd | BitTorrent 式 register/拉取 | etcd 强一致（元数据） | 分片拉取 | etcd | 适合 checkpoint 分发，非热路径 |

## 3. 四类归纳

### A. 中心权威星型（lake / Mooncake / MemCache / TensorCast 低基数）

共同：一个中心管"什么在哪"，节点贡献内存/存字节，数据面旁路中心直传。
分野在**读者怎么读**：Mooncake / MemCache 每次 RPC 问权威（简单，中心 QPS 压力）；lake 推送镜像 + 误判回查（热路径零 RPC，实现重）。
共同瓶颈：中心单机内存装全量元数据。Mooncake 的 1024 shard leader 与 lake CP 同构；TensorCast 用高低基数分治绕开（见 B）。

### B. 分片租约（TensorCast 高基数）

全局账本切片外包给分片宿主，GS 只记 N 条租约；HRW 排名 + fencing token 防脑裂。
换来元数据扩展性，代价：读者可能拿旧账（租约缓存未刷新），故障分片在租约过期前事实不可用。
对 lake：极致规模（百万级 KV 页打爆 CP 内存）时的备选路径；当前 lake 选单点权威换"随时能问到底"（守 D-direct 5ms 预算）。

### C. 事件流 / 快照最终一致（Dynamo / vLLM / SGLang / FlexKV）

共同：没有能问到底的权威；靠事件推送（NATS / zmq）或周期快照（Redis GMS）维持"大概准"的索引，误判靠重算 / 重试 / 回填兜底。
共同理由：高频位置写不该进强一致存储（Dynamo 把 KV 事件踢出 etcd 走 NATS 是明示）。
共同代价：索引陈旧是常态——SGLang PP×L3 多树发散（#22607）展示弱一致多副本的组合成本；FlexKV 展示"worker 死则索引没"的恢复缺口。
lake 的立场：同样不把高频写压给 etcd，但把权威放 CP 内存而非放弃权威——事件流只作镜像传播，不作权威本身。

### D. 弱协调 / 无（LMCache / UCM / TileRT）

- LMCache：有中心 controller 但刻意 best-effort（心跳 + full sync 0.8 阈值），O(n²) lookup 自承瓶颈——是"协调器"而非"权威"。
- UCM：框架自身零分布式语义，全部继承后端——证明"插件层"可以完全不碰一致性问题，代价是语义强弱取决于挂什么。
- TileRT：bs=1 专用引擎，无共享状态故无一致性问题——证明专用场景可以不付协调税。

## 4. 对 lake 设计的印证与警示

**印证**：

1. **etcd 不扛高频位置写**——Dynamo（事件走 NATS）与 Mooncake（etcd 只做选主/OpLog）两家独立印证 lake"权威在 CP 内存、etcd 降频 checkpoint"。
2. **中心内存权威可行**——Mooncake leader 1024 shard 内存元数据 + 快照/OpLog HA 是生产验证的同构方案。
3. **节点贡献内存进池**——MemCache LocalService 贡献 HBM/DRAM，证明 lake 的 agent 模型在昇腾生态已量产。
4. **镜像 + 回查的必要性**——SGLang 近似树靠猜、LMCache best-effort 靠周期收敛、FlexKV 快照无权威，三家各自的代价（误判 / 陈旧 / 恢复缺口）正是 lake 留"能问到底的权威"的理由。

**警示**：

1. **中心规模上限**——Mooncake / lake 同构瓶颈：全量元数据在单机内存。TensorCast 分片租约是已验证的备选，但引入旧账与故障窗口；lake 应在 CP 元数据规模逼近单机上限时再评估，不预设分片。
2. **弱一致多副本的组合爆炸**——SGLang PP×L3 多树发散需要 gloo all_reduce + 定向广播补偿；lake 单写者位置视图从根上避开。
3. **事件流可丢**——Dynamo approximate mode 明示丢事件；lake 镜像推送必须带序号 + gap replay（已入设计），且正确性不依赖镜像（回查兜底）。

## 5. 各项目分布式详节

- [sglang/overview.md](sglang/overview.md)「分布式模型」— 实例自治 + 旁路索引 + 双层选路
- [mooncake/overview.md](mooncake/overview.md)「分布式模型」— 星型 Master/Client + 控制流数据流分离
- [lmcache/overview.md](lmcache/overview.md)「分布式模型」— best-effort controller + 共享存储
- [vllm/overview.md](vllm/overview.md)「分布式模型」— 单实例引擎 + KV Events 外发
- [dynamo/overview.md](dynamo/overview.md)「分布式模型」— 事件流编排 + etcd/NATS 分离
- [memcache/overview.md](memcache/overview.md)「分布式模型」— Meta/Local 星型（节点贡献 HBM）
- [ucm/overview.md](ucm/overview.md)「分布式模型」— 无自有控制面，继承后端
- [tensorcast/overview.md](tensorcast/overview.md) — GS+Daemon 星型 + 高低基数分片租约（全文最详，含与 lake 一致性权威深度对照）
- [flexkv/overview.md](flexkv/overview.md)「分布式模型」— 本机 radix + Redis 周期快照
- [tilert/overview.md](tilert/overview.md)「分布式模型」— 无（单槽点对点）
