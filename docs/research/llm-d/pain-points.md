# llm-d Router — 痛点与 lake 对照

> 调研快照:2026-09-11;`3rdparty/llm-d-router` @ `abb404ef`。  
> [overview.md](overview.md) · [architecture.md](architecture.md)。  
> 对照:[`../model-routing.md`](../model-routing.md) §5、[`../dynamo/overview.md`](../dynamo/overview.md)、[`../../architecture/kv-cache-pool.md`](../../architecture/kv-cache-pool.md)。

## 1. 索引权威

| 现象 | 证据 | lake |
|------|------|------|
| 索引是派生缓存,引擎事件才是真相 | `Index` 注释;默认 `InMemoryIndex` | 位置视图是存储控制面的权威状态,不是谁的派生物 |
| 丢 remove 事件留幽灵条目 | `event_dedup_filter.go` 注释 | 权威视图随放置/驱逐同步改,不存在"等事件来修正" |
| 副本间不共享近似前缀状态,Active-Active 应避免 | `docs/operations.md` Warning;issue #1290 | Router 副本读同一权威推送的镜像,天然一致 |
| peer discovery 只铺路,跨副本同步未做 | `docs/peer-discovery.md` | 不需要:权威只有一份,镜像推送替代副本收敛 |
| 索引只记"块在哪个 pod",介质只作打分权重 | `PodEntry.DeviceTier`;权重 gpu=1.0/cpu=0.8 | 统一编址 L0–L3,层是介质不是位置 |

## 2. 决策与窗口

| 现象 | 证据 | lake |
|------|------|------|
| 需要推测索引补"决策→确认"空窗 | `prerequest.go::defaultSpeculativeTTL`(2s) | 放置由池决定、视图由池发,无决策-确认窗口 |
| 推测与确认条目共存,TTL 只清推测 | `TestSpeculativeAndConfirmedCoexist` | 不涉及 |
| Director 对目标 pod 失效的 fallback 未完善 | `director.go` TODO | 失败即重跑选路函数(F4),无降级链 |

## 3. PD 分离

| 现象 | 证据 | lake |
|------|------|------|
| PD 是静态角色 + sidecar 串阶段 | `docs/disaggregation.md`;`cmd/pd-sidecar` | PD 是逐请求模式;sidecar 中间人不需要 |
| prefill 崩溃留 stranded memory | `disaggregation.md` Drawbacks | KV 归存储池,worker 崩溃不滞留状态 |
| TTFT 上升、多一跳 | 同上 | D-direct 模式就是为消这跳 |
| encode 分离是 PoC | `disaggregation.md` WARNING | 多模态阶段分离暂不跟进,先记坑 |

## 4. 规模与边界

| 现象 | 证据 | lake |
|------|------|------|
| 单 InferencePool 单 EPP(Envoy 限制) | `docs/architecture.md` 假设 | Router 无状态,水平扩 |
| 每 pool 单一 base 模型 | 同上 | 存储池模型无关,多 `(model_id, revision)` 共存 |
| DP rank 不进索引与去重 | `event_dedup_filter.go` TODO #370 | DP/TP 拓扑是放置输入,不是事后补的维度 |
| 内存索引按 key 数计容,非按字节 | `in_memory.go` TODO | 池按字节与配额管理 |
| 输出长度靠静态估计 | `inflightload/token_estimator.go` TODO | 调度输入含长度分布(参考 TIE,见 model-routing.md §6) |

## 可直接借鉴

1. **推测索引机制**:决策后先写短 TTL 条目、真实事件确认、二者共存——任何"决策先行、状态后至"的控制回路都可套用(`prerequest.go::buildSpeculativeCache` / `PreRequest`)。
2. **事件管线三件套**:gap 检测重放(`zmq_subscriber.go`)+ 去重过滤器(`event_dedup_filter.go`)+ 订阅生命周期管理(`subscriber_manager.go`),消费引擎事件流的完整模板。
3. **插件配置形态**:`EndpointPickerConfig` 一份 YAML 组合 filter/scorer/profile,加策略不动框架(`configloader.go::InstantiateAndConfigure`)。
4. **PD 决策顺序**:先选 decode 再倒推 prefill(`prefix_based_pd_decider.go`),与"状态最重的角色先定"的直觉一致,lake 组 batch 时同理。
5. **介质分权重打分**:gpu=1.0/cpu=0.8 的前缀命中折算,是"命中不等于命中"的最简表达——lake 的位置视图直接带层信息,表达力更强,但这个折算系数可作调度代价模型的初值。

## 明确不照搬

1. 派生索引 + 多副本各自收敛的一致性模型——lake 用单写者权威替代。
2. sidecar/coordinator 的静态 PD 编排——lake PD 是运行时逐请求模式。
3. "每 pool 单模型"的架构假设——lake 存储池模型无关是既定原则。
4. 把介质层级折算成打分权重——lake 位置视图直接携带层信息。
