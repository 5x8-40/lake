# llm-d Router — 架构深潜

> [overview.md](overview.md) · [pain-points.md](pain-points.md)。调研快照:`3rdparty/llm-d-router` @ `abb404ef`(2026-09-08)。  
> 本文只深挖 router 仓(EPP + 边车 + coordinator);llm-d 项目级组件(WVA 扩缩、FS 卸载、模拟器/基准等)见 [overview.md](overview.md)「项目地图」。  
> §1 请求路径 → §2 块索引 → §3 事件管线 → §4 推测索引 → §5 插件体系 → §6 PD 编排 → §7 P2P KV 共享 → §8 多副本与 HA。每节末尾有小结。

## 1. 请求路径:ext-proc 回调里的完整选路

EPP 是 Envoy 的 External Processing 后端,只实现 `FULL_DUPLEX_STREAMED` 一种 body 模式。一个请求的完整路径:

```mermaid
sequenceDiagram
    autonumber
    participant C as 客户端
    participant G as Envoy 网关
    participant E as EPP
    participant S as decode pod(sidecar)
    C->>G: 推理请求
    G->>E: ext-proc 流
    Note over E: Director.HandleRequest 编排
    E->>E: header 插件 → screener(准入筛查) → data producers → admission
    Note over E: data producers 里 precise-prefix-cache-producer<br/>把 prompt 切成块键、确保对该 pod 的事件订阅存在
    E->>E: Scheduler: Filter 筛掉不合格 pod → Scorer 加权累加 → Picker 定终点
    E-->>G: 写回目标 endpoint;走 PD 时附 x-prefiller-host-port 等头
    G->>S: 转发到 decode pod
    Note over S: sidecar 读头执行多阶段编排(§6)
    S-->>C: 流式输出 token
```

代码锚点:`StreamingServer.Process`(`pkg/epp/handlers/server.go`)→ `Director.HandleRequest`(`pkg/epp/requestcontrol/director.go`)→ `Scheduler.Schedule`(`pkg/epp/scheduling/scheduler.go`);Scorer 打分经 `enforceScoreRange(score) × Weight` 截断加权后**累加**;切块键与订阅见 `preciseprefixcache` 的 `Extract` / `ensureSubscriber`。

**小结**:EPP 把"选路"做成了 Envoy 回调里的一段纯计算——无自有数据面,无自有协议。代价是 Envoy 限制(单 InferencePool 单 EPP、流式 body 模式)直接进入架构假设。

## 2. 块索引:`pkg/kvcache` 的"块 → pod"映射

### 2.1 数据结构

| 件 | 说明 | 锚点 |
|----|------|------|
| `Index` 接口 | `Lookup` / `Add` / `Evict` / `Clear` / `GetRequestKey` | `pkg/kvcache/kvblock/index.go::Index` |
| 键 | `BlockHash`(uint64);引擎块键与请求块键多对一映射(`engineToRequestMapping`) | 同上 |
| 值 | `PodEntry{PodIdentifier, DeviceTier, Speculative, GroupIdx}` | 同上 |
| 默认后端 | `InMemoryIndex`:外层 LRU(块哈希)→ `PodCache`(内层 LRU,pod 条目) | `kvblock/in_memory.go` |
| 可选后端 | Redis / CostAwareMemory | `NewIndex` 分支 |
| 切块 | `TokenProcessor.TokensToKVBlockKeys`:tokenize → 按块切 → xxh64 | `kvblock/token_processor.go` |

### 2.2 打分怎么算

1. 入口 `Indexer.ScoreTokens` → `MatchBlockKeys`(`pkg/kvcache/prefix_match.go`)。
2. `prefixAccumulator` 沿请求的块键序列走,连续命中才累计(前缀断链即停)。
3. 命中介质分权重:默认 gpu=1.0、cpu=0.8(`DefaultKVCacheBackendConfig`);推测条目单独计入 `"speculative"` 层。
4. 输出 `PodMatch{WeightedScore, MatchedBlocks, BlocksByTier}`,交给 scorer 插件折算成调度分。

**小结**:索引本身只回答"哪个 pod 持有这个块(可能在哪层介质)",不含字节级位置、不含生命周期——它是给打分用的摘要,不是存储元数据。这正是它与 lake 位置视图的本质区别:够选路用,但不足以驱动放置、迁移、恢复。

## 3. 事件管线:ZMQ 订阅 → 索引更新

```
引擎 ZMQ PUB → zmqSubscriber.Start → Pool.AddTask → worker
  → EngineAdapter.ParseMessage(vLLM / SGLang 两种适配)
  → processEventBatch(BlockStored / BlockRemoved / AllBlocksCleared)
  → Index.Add / Evict / Clear
```

韧性设计三件套:

1. **序列号 gap 检测**:发现跳号 → 触发重放(`replayTimeout=2m`、`replayCooldown=30s`);重放不全时清掉部分状态重来。
2. **去重过滤器**(`event_dedup_filter.go::eventDedupFilter`):防重复事件把计数搞漂;注释承认 ZMQ 丢 remove 会留幽灵条目。
3. **订阅生命周期**(`subscriber_manager.go::EnsureSubscriber`):producer 首次需要某 pod 数据时确保订阅存在。

**小结**:这是"消费引擎 KV 事件"的完整工程模板——订阅管理、解码适配、gap 重放、去重,四层各司其职。任何要消费同类事件流的系统(包括 lake 存储控制面)都可对照这个分层。

## 4. 推测索引:决策与确认之间的 2 秒空窗

问题:调度决策做完,到引擎真的把块存好、事件传播回来,中间有毫秒到秒级空窗;同前缀的后续请求在此期间到达,会以为没有亲和可打。

解法(`.../preciseprefixcache/prerequest.go`):

1. 调度完成后 `Producer.PreRequest` 立刻写入 `PodEntry{Speculative: true}`(只带请求块键,不带引擎键)。
2. 条目放 `ttlcache`,默认 TTL **2 秒**(`defaultSpeculativeTTL`);到期 `OnEviction` 回调里 `Index.Evict` 清掉。
3. 推测条目只带请求块键(引擎键为 nil,`PreRequest` 里 `index.Add(ctx, nil, promptKeys, ...)`);引擎真实 `BlockStored` 事件到达后由事件管线写入确认条目,确认不依赖推测条目过期。
4. 打分时推测命中计入单独的 `"speculative"` 层(`prefix_match.go::SpeculativeTier`),scorer 可区别对待。

**小结**:推测索引是"最终一致 + 决策先行"系统的标准补窗手法,2 秒 TTL 是经验值。它解决的严格说是**路由器自己造成的问题**(索引是派生的才有窗口)——权威视图系统没有这个窗口,但这个思路对任何带异步确认的控制回路都通用。

## 5. 插件体系:四层接口 + 一份 YAML

### 5.1 接口分层

| 层 | 干什么 | 例 |
|----|--------|----|
| Filter | 筛掉不合格 pod | `prefill-filter` / `decode-filter` / `label-selector-filter` |
| Scorer | 打分,带权重 | 见下表 |
| Picker | 从打分结果定一个 | max-score(默认) |
| ProfileHandler | 整档调度档案(如 PD) | `disagg-profile-handler` |

### 5.2 in-tree scorer(注册于 `cmd/epp/runner/runner.go::registerInTreePlugins`)

| 类别 | scorer |
|------|--------|
| 前缀亲和 | `precise-prefix-cache-scorer`(精确,走事件索引)、`prefix-cache-scorer`(近似)、`no-hit-lru-scorer`、`mm-embeddings-cache-scorer`(多模态) |
| 负载 | `load-aware-scorer`、`queue-scorer`、`kv-cache-utilization-scorer`、`token-load-scorer`、`running-requests-size-scorer`、`active-request-scorer` |
| 亲和/其他 | `session-affinity-scorer`、`lora-affinity-scorer`、`latency-scorer`、`topology-affinity-scorer`、`context-length-aware`(按 pod 上下文长度标签)、`endpoint-attribute-scorer`、`header-label-affinity-scorer`、`multicluster-*` |

共 20 种 scorer(含 3 个 multicluster 变体),全部注册于 `registerInTreePlugins`。

组合方式:每个 scorer 得分截断到值域后乘权重累加;profile 里声明用哪些插件。

### 5.3 配置形态

`EndpointPickerConfig` YAML(`apiVersion: llm-d.ai/v1alpha1`)四块:`plugins`(实例化哪些插件)+ `schedulingProfiles`(filter/scorer 组合)+ `dataLayer` + `featureGates`。加载:`LoadRawConfig` → `InstantiateAndConfigure`(`pkg/epp/config/loader/configloader.go`)。加自定义 filter/scorer 不需要动框架,教程见 `docs/create_new_filter.md`。

**小结**:llm-d 的插件体系是四个项目里最规整的——接口分层少而清楚,配置即组合。lake Router 的代价函数若演化为多 scorer 组合,这套"接口 + YAML profile"是最直接的形态参照。

## 6. PD 编排:sidecar 与 coordinator 两条路

![E/P/D 架构](figures/epd_architecture.png)

(图源:`3rdparty/llm-d-router` `docs/disaggregation.md` 的 Diagram 节,原图 alt 为 "Disaggregated Encode/Prefill/Decode Architecture"。上游该节仅嵌图未配文字说明(原文留有 TODO);各组件事实见本节正文及所引源码。)

### 6.1 路径 A:pd-sidecar(主路径)

sidecar(边车)指与引擎容器跑在同一个 pod 里的配套代理容器——引擎不变,边车替它收发多阶段请求。

1. EPP 侧:`disagg-profile-handler` 管整档调度;`prefix-based-pd-decider`(前缀命中够多就不拆 prefill)或 `always-disagg-pd-decider` 决定是否分离;`prefill-filter` / `decode-filter` / `encode-filter` 按角色标签筛 pod。
2. 决策顺序:**先选 decode**,再按需选 encode,再按需选 prefill(`docs/disaggregation.md` 明确此序;`stageOrder` 默认 `decode-first`,可配 `prefill-first`)。
3. 执行侧:请求先到 decode pod,pod 上的边车代理(`pkg/sidecar/proxy/proxy.go::NewProxy`)读 `x-prefiller-host-port` 等头,先向远端 prefill worker 发请求并接 KV,再本地 decode。

### 6.2 路径 B:coordinator

sidecar 路径把"先 prefill 再 decode"藏在 decode pod 里完成;coordinator 路径换了一个做法——**多阶段编排交给一个独立部署的服务**:

1. 客户端请求不直接进引擎,先到 coordinator(`cmd/coordinator`,独立服务)。
2. coordinator 把请求拆成一条**流水线**逐步执行(`pkg/coordinator/pipeline/pipeline.go::Pipeline.Execute`):`replace-media-urls`(多模态 URL 替换)→ `render` → `encode`(可选)→ `prefill` → `decode`,每一步是 `pkg/coordinator/steps/` 下的一个 `Step`,可注册扩展(`pipeline.Register`)。
3. **每个阶段单独过一遍网关**:step 持 `gateway.Client`(指向推理网关的 HTTP 客户端)把该阶段的子请求 POST 回 Envoy,头上带阶段标记(`EPPProfileHeader: prefill` 等),由 EPP **为这个阶段单独选 pod**——不像 sidecar 路径那样一次把 prefill/decode 两个点都选好。阶段之间的 KV / 多模态缓存搬运走连接器(`pkg/coordinator/connectors/kv|ec`:NIXL 或共享存储)。
4. 任一步失败整条流水线终止;decode 流式输出途中上游故障的错误分类也写死了(`UpstreamStreamedError`:响应已开始就不能再写错误体,只记账)。

与 sidecar 的分工:sidecar 不增部署单元(嵌在 decode pod),但每个引擎 pod 都得挂边车;coordinator 不动引擎 pod,代价是多一个服务、每阶段多一次网关往返。encode 分离(E/P/D)只在这条路上,标 **PoC/experimental**。

### 6.3 官方自认的代价

`docs/disaggregation.md` 的 Drawbacks 节:TTFT 上升、多一跳传输、prefill 崩溃会留下 stranded memory(已算好的 KV 占着显存却没有请求来用,只能等超时回收)、必须有 timeout/retry。

**小结**:llm-d 的 PD 是"**部署时分开、运行时串接**"——prefill/decode 是静态角色,sidecar 把两跳串成一跳的外观。lake 的 PD 是逐请求模式选择,同一集群里分离/混部/D-direct 并存,不需要 sidecar 这个中间人;但 llm-d 把 PD 工程问题(stranded memory、超时重试、先选 decode)摆到明处,这些坑 lake 同样要过。

## 7. P2P KV 共享:locality 破裂时的补传输

2026-08-15 公开([官方博客](https://llm-d.ai/blog/p2p-kv-cache-sharing-llm-d))。要解决的问题:前缀 KV 已在集群里,但持有它的实例在排队——路由到持有者要排队,路由到空闲节点要重算,两个答案都不对。P2P 给第三条路:**按负载选最优节点,把 KV 拷过去**。

### 7.1 机制

- 每个参与的 vLLM 实例按请求扮两种角色:**consumer**(从对等节点拉匹配块,代替本地重算)与 **producer**(从自己的 CPU 卸载层供块)。传输是 CPU↔CPU,两侧 GPU 都不参与拷贝——供块的代价是 producer 的 CPU 内存带宽与网卡,不是 GPU 算力;producer 保留副本(拷贝,不是移动)。
- 握手:consumer 发所需块的哈希,producer 回报哪些块还在,然后经 NIXL(UCX/RDMA)把匹配块**写**过去。**索引只作 hint,真实可用性以握手为准**。
- EPP 侧:`p2p-source-producer` 数据插件(`pkg/epp/framework/plugins/requestcontrol/dataproducer/p2psource/producer.go`)从前缀索引选 source——"持有最多前缀的对等节点"比"选定目的地"多持有超过 `minCachedTokenDelta`(默认 1;生产应设在实测交叉点之上)才指定 source,打平或自命中保持本地。多个近似持有者按**等待队列深度反比**采样(`waitingQueueSize`),把并发拉取摊开。
- 执行侧:EPP 把 source 写进请求头,decode pod 的 sidecar 转成引擎的 P2P 参数(`pkg/sidecar/proxy/connector_p2p.go::handleP2P` / `addP2PPullToPrefill` / `decodeWithP2PSource`;NIXL v2 连接器见 `connector_nixlv2.go`)。
- 与 PD 分离组合:prefill worker 可以拉 decode 产生的历史 KV,只算增量部分,再走正常 PD 流程,应用无感。

### 7.2 边界(官方自认)

- **默认关闭**:交叉点(传输 vs 重算)随模型/KV 格式/硬件/网络而变,必须先实测校准。gpt-oss-120b @ H200 上 2K token 就赚(35ms vs 78ms),GLM-5.2(KV 约 93KB/token)上约 8.7K token 才回本。
- **静默前提**:所有对等节点必须用相同的 block-size 与 hash-seed,否则块哈希对不上,P2P 静默退化为零命中。
- 本地命中时 P2P 正确地不动作;索引刚重启时没有 source 可用;冷前缀首次仍需重算——它消除的是重复劳动,不是首次计算。

### 7.3 效果(官方数据)

- GLM-5.2-FP8(753B MoE,32×H200,PD 分离,并发 64):精确路由 + P2P 成功吞吐 +11.1%(对近似路由基线),三次重复平均 +9.6%,TTFT 中位 −25%。
- PD 多轮会话历史跨角色搬运:TTFT 中位 6.83s → 1.09s,吞吐 +50%。
- 文档问答(192 篇 48K token 文档 × 128 会话):负载感知放置 + P2P 对比精确亲和,p99 TTFT 25.2s → 16.6s,吞吐 +35%,冷集群客户端超时 48 → 0。

**小结**:P2P 把 EPP 的派生索引从"选路依据"升级为"传输指令"——与 Dynamo KVCR 的"router hint 驱动 NIXL P2P"同构,"路由器指路 + 引擎间直传"正在收敛成公共模式。对 lake 的对照:lake 由存储池统一放置/预置,本地命中走 D-direct 零传输;llm-d 没有池,用 P2P 事后补救 locality 破裂。一个是事前放置,一个是事后补传;且 lake 的位置目录是权威视图,llm-d 的是派生索引加握手兜底。

## 8. 多副本与 HA

| 模式 | 做法 | 限制 |
|------|------|------|
| Active-Active | 多副本并列,各自订阅全部事件流 | **近似前缀路由下官方建议避免**——副本不共享该状态(issue #1290);精确前缀可靠各自收敛 |
| Active-Passive | K8s Lease 选主(readiness=leader)或 Envoy 优先级路由 | 切换期索引重建 |
| Fail-open | EPP 全挂时 Envoy 直打后端 | 失去亲和与负载感知 |

跨副本前缀状态同步:peer discovery 已就位(`docs/peer-discovery.md`),但**只为未来 syncer 铺路**,当前副本间不交换前缀元数据。

**小结**:EPP 的 HA 答案本质是"让派生缓存可以快速重建"——因为索引本来就不是权威,丢了重建即可。这再次印证其定位:决策辅助层,不是状态权威层。
