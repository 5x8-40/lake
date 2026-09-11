# AIBrix — 痛点与 lake 对照

> 调研快照:2026-09-11;`3rdparty/aibrix` @ `fe7db93e`。  
> [overview.md](overview.md) · [architecture.md](architecture.md)。  
> 对照:[`../model-routing.md`](../model-routing.md) §5、[`../flexkv/pain-points.md`](../flexkv/pain-points.md)、[`../../architecture/kv-cache-pool.md`](../../architecture/kv-cache-pool.md)。

## 1. 状态权威

| 现象 | 证据 | lake |
|------|------|------|
| 路由亲和索引在网关进程内,默认不同步 | `PrefixHashTable`;`AIBRIX_STATESYNC_ENABLED` 默认关 | 位置视图只有存储控制面一份权威,Router 读镜像 |
| 可选同步是 Redis 周期 pull/push,最终一致 | `statesync/redissync.go::RedisSync` | 镜像由权威变更触发推送,非周期对账 |
| KV 事件索引各副本各自订阅各自收敛 | `pkg/kvevent/manager.go`;`kv-event-sync.rst` | 事件流在存储控制面汇聚成权威视图后再分发 |
| least-request 计数跨副本不共享 | `least_request.go` 注释;issue #761 | 调度输入(in-flight/队列)由执行侧上报,控制面汇总 |
| VTC token tracker 只在本副本 | `vtc/token_tracker.go` TODO | 公平性归 gateway,但若做也必须基于共享状态 |

## 2. 前缀与索引

| 现象 | 证据 | lake |
|------|------|------|
| 哈希表版是"见过才记下"的估计:固定 20 万槽、4 token/块 | `prefix_cache.go` 常量 | 位置视图记录真实块位置,不靠网关记忆 |
| 考虑一致性哈希+LSH 牺牲精度换扩展 | issue #672 | 强一致视图不需要用精度换扩展 |
| 事件同步版依赖 remote tokenizer 复刻引擎切块 | `kv-event-sync.rst` 要求 | 存储池按不透明字节块存取,不解释张量,切块归引擎与池的注册协议 |
| 索引只记"前缀→pod",不记块在哪层介质 | 两条路线均如此 | 位置视图统一编址 L0–L3,层是介质不是位置 |

## 3. KV 卸载与分层

| 现象 | 证据 | lake |
|------|------|------|
| L1 在引擎进程内,pod 重启全丢 | `l1/l1_cache.py` | L1 是池化 DRAM,与算力节点生命周期解耦 |
| L2 成员元数据走 Redis | `cmd/kvcache-watcher`;`RedisMetaService` | 池元数据在强一致控制面(etcd 降频 checkpoint) |
| 卸哪些块由引擎侧逐出策略局部决定 | LRU/FIFO/S3FIFO 逐出层 | 池按全局热度 + 引用计数冻结统一决策 |
| 仅支持 FlashAttention/XFormers 布局 | `kvcache-offloading.rst` warning | 池按不透明字节块存取,不绑定 attention 实现 |
| 选择性卸载的动机是低配网卡带宽 | offload 框架 README | 借鉴:带宽约束应进入池的迁移/预放置代价模型 |

## 4. 编排与扩缩

| 现象 | 证据 | lake |
|------|------|------|
| PD 是 StormService 静态角色拓扑 | `pkg/controller/stormservice/` | PD 是逐请求运行时模式,角色不固化 |
| Autoscaler 只支持单一指标源 | `GetPaMetricSources` 注释 | 不涉及(lake 不做扩缩);指标选择经验可采 |
| Preble 成本模型系数按"模型×GPU"硬编码 | issue #677 | lake 模式选择的开销模型需可校准(P7),不写死 |

## 5. 工程完成度

| 现象 | 证据 | lake |
|------|------|------|
| PD + Mooncake 传输多处 TODO 未实现 | `algorithms/pd/transfer/mooncake.go` | Transfer Bus 抽象先行,后端可换 |
| ModelAdapter Scaled 相位未实现 | `modeladapter_types.go` TODO | 不涉及(LoRA 编排在外部) |
| 部分策略变体只有名字没有实现 | `vtc_router.go` TODO | 代价函数组合演化时引以为戒:先接口后策略 |
| GPU 故障检测只见宣传未见独立实现 | 仓内无对应核心包 | F4 故障恢复是 lake 一等公民,不贴标签了事 |

## 可直接借鉴

1. **加权 scorer 组合的形态**:`Register` + `ParseMultiRouterConfig("a:2,b:1")`,每种策略独立灰度。lake Router 代价函数演化路径上最现实的参照。
2. **KV 事件管线工程**:ZMQ 订阅 + msgpack 编解码 + 事件到索引的转换层分层(`zmq_client` / `kvevent.Manager` / sync indexer),lake 存储控制面消费引擎事件时可对照分层。
3. **TP 感知对齐**:prefill 前各 TP rank 对齐已取回 KV 长度(`GroupAwareKVCacheManager`)。lake P5 对接引擎做 Pool 命中续推时必须处理同一个问题。
4. **过载拒绝位置的证据**:RPM/TPM 限流、鉴权全在 Envoy 回调链里,引擎不操心——lake"过载控制归 gateway"原则的生产级先例。
5. **KV 感知扩缩指标**:`gpu_cache_usage_perc`、`num_requests_waiting` 进扩缩决策;lake 定义上报信号清单时直接收录。

## 明确不照搬

1. 网关侧"估计索引 + 副本间同步"的路线(无论哈希表还是事件订阅)——lake 用单写者权威替代副本收敛。
2. L1 进程内、L2 外部集群 + Redis 元数据的卸载栈——lake L0–L3 统一归池。
3. StormService 式静态 PD 角色——lake PD 是逐请求模式。
4. 引擎侧局部逐出策略决定卸载——lake 由池按全局热度决定。
