# AIBrix — 总览

> 源码:`3rdparty/aibrix`(submodule,HEAD `fe7db93e`,2026-09-08)。上游 [vllm-project/aibrix](https://github.com/vllm-project/aibrix)(字节跳动发起并捐赠,现属 vllm-project)。许可:**Apache-2.0**。  
> 白皮书:arXiv [2504.03648](https://arxiv.org/abs/2504.03648);官网文档 [aibrix.readthedocs.io](https://aibrix.readthedocs.io)。  
> 网关路由/事件同步/卸载框架细节见 [architecture.md](architecture.md);与 lake 对照见 [pain-points.md](pain-points.md)。路由策略横评见 [`../model-routing.md`](../model-routing.md) §5。

## 一句话定位

AIBrix 是一套 **K8s 原生的 GenAI 推理基础设施积木**:控制面(CRD 编排 + 自动扩缩 + 元数据服务)+ 数据面(Envoy 网关插件做 KV 感知选路)+ 引擎旁 KV 卸载框架(L1 DRAM / L2 远端集群),目标是把"部署、管理、扩缩、路由 LLM 推理"这一整层平台化,而不是只做一个路由器或只做一个缓存库。

![AIBrix 架构](figures/aibrix-architecture-v1.jpeg)

(图源:AIBrix 官方文档。左侧控制面:metadata service、autoscaler、CRD controllers;右侧数据面:Envoy 网关 + gateway plugins + 推理 pod(runtime sidecar)。)

## 与本系统的关系

| AIBrix 概念 | 本系统对应 | 关系 |
|-------------|-----------|------|
| Gateway Plugins(Envoy ExtProc 选 pod) | Go Router | **同层对照**;但 AIBrix 只做"选 pod",lake Router 还要做模式选择(PD 分离/混部/D-direct)与集群级调度 |
| `prefix-cache` 路由(进程内哈希表) | Router 读存储池位置视图镜像 | **形态可对照**;AIBrix 的索引是网关侧估计, lake 的位置视图由存储控制面权威维护 |
| KV 事件同步(vLLM ZMQ → 网关索引) | 存储池位置视图推送 | **机制同源**(都消费引擎 KV 事件);lake 的事件流由存储控制面汇聚成权威视图再推镜像,AIBrix 各网关副本各自订阅、各自收敛 |
| `aibrix_kvcache` L1(DRAM)/L2(InfiniStore/HPKV 等) | lake L1/L2 分层 | **直接对标** FlexKV/LMCache 同层;lake 的 L1/L2 归存储池统一管理,AIBrix 的 L1 在引擎进程、L2 是可插拔外部集群 |
| PodAutoscaler(HPA/KPA/APA) | 无(lake 不做扩缩控制) | **职责边界样本**:扩缩属外部控制面;lake 只上报信号。其"按 KV 使用率/排队长度扩缩"的指标选择可参考 |
| StormService / RoleSet(PD 角色编排) | 无(编排归 K8s/外部) | PD 角色编排的 K8s 化样本;lake 的 PD 是运行时模式,不是静态角色 |
| Metadata Service + Redis | etcd + 存储控制面 | AIBrix 用 Redis 做用户/限流/元数据权威;lake 用 etcd 做强一致元数据 |
| Runtime Sidecar(指标标准化/LoRA/权重下载) | Python worker 辅助面 | sidecar 不代理推理流量,只做管理;职责切分干净,可参考 |

**核心结论**:AIBrix 是"**平台层**"项目——路由、扩缩、编排、卸载、元数据全覆盖,但每一块都停在"K8s 生态标准做法"的深度:路由索引在网关进程内、KV 卸载以引擎为中心、状态权威分散在 K8s etcd 与 Redis。lake 与它的根本分歧不在功能清单,而在**状态权威的归属**:lake 把 KV 位置、分层、生命周期全部收归存储池(强一致控制面),AIBrix 把它们留在网关估计、引擎进程与外部缓存集群里。

## 设计哲学

1. **云原生积木**:一切能力做成 K8s CRD/controller 或标准扩展点(Envoy ExtProc、Gateway API),用户按需拼装,不自研 serving 引擎。
2. **控制面/数据面分离**:控制面(CRD、autoscaler、metadata)走 K8s API 与 Redis;数据面只有 Envoy → gateway-plugins → 引擎 pod 一条链,runtime sidecar 不碰推理流量。
3. **引擎无关**:vLLM / SGLang 经指标适配层接入;KV 卸载框架以 connector 注入,不改引擎主干。
4. **可组合策略**:路由策略注册表 + 加权混合(`"least-request:2,throughput:1"`),每种策略可独立灰度。

## 架构

```
Client HTTP
  → Envoy Gateway(Gateway API HTTPRoute;modelrouter controller 自动建路由)
  → ExtProc gRPC → gateway-plugins
        ├─ 鉴权 / Redis 限流(RPM/TPM)
        ├─ 路由打分(prefix-cache / least-* / VTC / Preble … 加权组合)
        │     ├─ 进程内 PrefixHashTable(默认)
        │     └─ 或 KV 事件同步索引(vLLM ZMQ → SyncPrefixHashTable)
        └─ 回写 target-pod 头 → Envoy 转发到 pod
推理 Pod = vLLM/SGLang (+ 可选 runtime sidecar + 可选 aibrix_kvcache connector)
        └─ aibrix_kvcache:L1 DRAM(引擎进程内) → L2 远端集群(InfiniStore/HPKV,RDMA)
控制面(K8s controller manager):PodAutoscaler / StormService / ModelAdapter(LoRA)
        / KVCache CR / ModelRouter;Metadata Service(Python FastAPI + Redis)
```

| 模块 | 职责 |
|------|------|
| **gateway-plugins**(`cmd/plugins`) | Envoy ExtProc 服务:鉴权、限流、选 pod、回写路由头 |
| **进程内 Cache**(`pkg/cache`) | pod/模型视图、指标拉取、prefix 索引、可选 KV 事件订阅 |
| **controllers**(`cmd/controllers`) | 全部 CRD 的 reconcile:扩缩、编排、LoRA、KVCache 集群 |
| **kvcache-watcher**(`cmd/kvcache-watcher`) | watch L2 集群成员,写 Redis 成员表 |
| **Metadata Service**(`python/aibrix/metadata`) | HTTP 元数据/批量推理 API,状态存 Redis |
| **runtime sidecar**(`python/aibrix/runtime`) | 指标标准化、LoRA/权重下载、ModelClaim 唤醒;不代理流量 |
| **aibrix_kvcache**(`python/aibrix_kvcache`,C++ 内核) | 引擎旁 L1/L2 KV 卸载框架,TP 感知,LRU/FIFO/S3FIFO 逐出 |

## 分布式模型

> 跨项目汇总对比见 [../distributed-models.md](../distributed-models.md);细节见 [architecture.md](architecture.md)。

- **拓扑**:典型 K8s 平台拓扑——API server/etcd 为中心,网关多副本无状态(除本地索引),KV 事件流从引擎 pod 扇出到各网关副本。
- **元数据权威**:分三块,互不统一:
  1. **编排期望态** → K8s etcd(CRD 权威);
  2. **用户/限流/元数据** → Redis(metadata service 与网关限流器共用,部署偏单 master);
  3. **路由亲和状态** → 各网关副本**进程内**(prefix 哈希表 / KV 事件索引),无权威。
- **同步机制**:路由状态默认不同步;开 `AIBRIX_STATESYNC_ENABLED` 后经 Redis 周期 pull/push(**最终一致**);KV 事件索引走另一条路——每个网关副本各自用 ZMQ 订阅全部引擎 pod 的 BlockStored/BlockRemoved 事件,各自收敛。
- **一致性分级**:编排态强一致(etcd);用户态 Redis 单点语义;路由亲和弱一致(本地估计 + 可选最终一致同步);KV 事件索引依赖事件流完整,丢事件则亲和短暂失真。
- **HA 与故障**:网关副本重启丢本地索引(靠重新订阅事件流/重新同步重建);Redis 挂则鉴权/限流/元数据不可用;L1 KV 随引擎 pod 消亡;L2 数据持久性取决于外部后端(InfiniStore/HPKV)。
- **扩展性**:网关无状态可水平扩;代价是每副本全量订阅事件流、全量维护索引,副本数 × 事件吞吐是固定放大系数。
- **与 lake 对照**:AIBrix 的"各副本各自订阅、各自收敛"与 llm-d EPP 同构,都是 lake"单写者权威 + 镜像推送"的对立面。lake 的位置视图只有存储控制面一份权威,Router 读的是权威触发推送的镜像,可回查;AIBrix 每个副本的视图都是独立估计,副本间可能给出不同选路结果,且无权威可回查。另外 lake 的 KV 索引覆盖 L0(HBM),AIBrix 的两条索引路线(哈希表/事件同步)都只是**前缀→pod** 的亲和提示,不记录块在哪层介质。

## 技术栈

| 层 | 技术 |
|----|------|
| 控制面/网关 | Go(controller-runtime、Envoy ExtProc gRPC、go-redis) |
| 元数据/runtime/卸载框架 | Python 3.10–3.12(FastAPI、Poetry) |
| KV 卸载内核 | C++/CUDA(`csrc/cache_kernels.cu`,自定义 Torch ops) |
| 状态存储 | K8s etcd(CRD)+ Redis/Valkey(限流、元数据、L2 成员表、可选路由状态同步) |
| KV 事件 | vLLM ZMQ PUB,msgpack 编码(需 `-tags=zmq` 构建网关插件) |
| L2 后端 | InfiniStore / HPKV / Vineyard / RocksDB / 共享文件系统,RDMA 或 TCP |
| Web UI | TypeScript(console / chat / aiconfigurator) |

## 优势与局限

**优势**(对 lake 有参考价值的):

1. 功能面最完整的 K8s 推理平台样本:路由/扩缩/编排/卸载/元数据一套打齐,且都是生产部署形态。
2. 路由策略数量最多且可组合:prefix-cache(哈希表与 RadixTree 两版)、least-request/least-latency/least-kv-cache、VTC、Preble,加权混合。
3. 双索引路线并存(本地估计 vs KV 事件精确同步),是"近似派 vs 精确派"在同一项目里的天然对照实验。
4. `aibrix_kvcache` 的 TP 感知对齐(各 TP rank 对齐已取回 KV 长度再 prefill)是跨引擎 KV 复用的真实工程问题,AIBrix 给出了明确解法。

**局限**(详见 [pain-points.md](pain-points.md)):

1. 路由状态权威缺失:本地表默认不同步,Redis 同步是最终一致,事件索引各副本各自收敛——三种方式都不是强一致。
2. least-request 计数、VTC token tracker 都只在本副本内,多网关实例下公平性与均衡性失真(代码 TODO 与 issue #761 自认)。
3. KV 卸载框架限定 FlashAttention/XFormers 后端;L1 在引擎进程内,pod 重启全丢。
4. PodAutoscaler 目前只支持单一指标源。

## 借鉴点与关键差异

**借鉴点**(展开见 [architecture.md](architecture.md) 与 [pain-points.md](pain-points.md)):

1. **策略注册表 + 加权组合打分**:`RouterManager` + `Register` 的插件形态,lake Router 的代价函数演化为 scorer 组合时可直接对照。
2. **KV 事件消费的工程细节**:ZMQ 订阅、msgpack 编解码、事件→前缀索引的转换层(`pkg/cache/kvcache/`),lake 存储控制面消费引擎事件时的编解码与背压处理可参考。
3. **TP 感知 KV 对齐**:跨引擎复用时各 TP rank 先对齐命中长度再继续,这是 lake P5 对接引擎时绕不开的问题。
4. **职责边界样板**:限流/鉴权/扩缩全在推理系统之外(网关与 K8s 控制面),与 lake"过载控制归 gateway"的原则完全同向,可作为该原则在业界落地的证据。

**关键差异**(lake 更彻底,不照搬):

1. AIBrix 的 KV 位置知识分散在**三处**(网关估计表、引擎进程 L1、外部 L2 集群),lake 收归存储池一份权威。
2. AIBrix 的卸载框架以**引擎为中心**(connector 注入、L1 进程内),lake 的 L0–L3 全由存储池管理,计算节点不拥有任何内存。
3. AIBrix 路由只做"选 pod",lake Router 做"模式 + 节点"的联合决策(PD 分离/混部/D-direct),输入是存储池的权威位置视图而非网关侧估计。

## 代码索引

> 路径相对 `3rdparty/aibrix/`;符号为锚点,行号会漂移时 `grep -rn "符号名" 3rdparty/aibrix/<路径>`。

| 概念 | 文件:符号 |
|------|-----------|
| 网关插件入口 | `cmd/plugins/main.go`::`main` |
| ExtProc 服务 | `pkg/plugins/gateway/gateway.go`::`NewServer` / `Process` / `selectTargetPod` |
| 请求体处理 | `pkg/plugins/gateway/gateway_req_body.go`::`HandleRequestBody` |
| 打分器接口 | `pkg/types/router_context.go`::`PodScorer`(`ScoreAll`/`Polarity`) |
| 路由注册表 | `pkg/plugins/gateway/algorithms/router.go`::`Register` / `RouterManager` / `ParseMultiRouterConfig` |
| prefix-cache 路由 | `pkg/plugins/gateway/algorithms/prefix_cache.go`::`RouterPrefixCache` / `prefixCacheRouter.ScoreAll` / `kvSyncPrefixCacheRouter.ScoreAll` |
| 前缀哈希表 | `pkg/utils/prefixcacheindexer/hash.go`::`PrefixHashTable` / `MatchPrefix` |
| Redis 状态同步 | `pkg/plugins/gateway/statesync/redissync.go`::`RedisSync` / `Start` |
| 限流 | `pkg/plugins/gateway/gateway_ratelimit.go`::`checkLimits` / `checkRPM` / `checkTPM` |
| 进程内 Cache | `pkg/cache/cache_init.go`::`Store` / `InitWithOptions` |
| 指标拉取 | `pkg/metrics/engine_fetcher.go`::`EngineMetricsFetcher` |
| KV 事件管理 | `pkg/kvevent/manager.go`::`Manager` / `NewManager` / `ProcessBlockStored` |
| ZMQ 事件客户端 | `pkg/cache/kvcache/zmq_client.go`;msgpack 编解码 `msgpack_decoder.go` |
| 输出长度预测 | `pkg/cache/output_predictor.go`::`SimpleOutputPredictor` |
| Autoscaler 算法工厂 | `pkg/controller/podautoscaler/algorithm/algorithm.go`::`NewScalingAlgorithm` |
| Autoscaler 决策 | `pkg/controller/podautoscaler/autoscaler.go`::`DefaultAutoScaler.ComputeDesiredReplicas` |
| KVCache CR reconcile | `pkg/controller/kvcache/kvcache_controller.go`::`KVCacheReconciler.Reconcile` |
| L2 后端 | `pkg/controller/kvcache/backends/infinistore.go`::`InfiniStoreBackend` |
| L2 成员发现 | `cmd/kvcache-watcher/main.go`::`main` |
| Runtime sidecar | `python/aibrix/aibrix/runtime/model_runtime.py`::`ModelRuntime` |
| Metadata Service | `python/aibrix/aibrix/metadata/app.py`::`build_app`;`metadata/store.py`::`RedisMetadataStore` |
| KV 卸载总管 | `python/aibrix_kvcache/aibrix_kvcache/cache_manager.py`::`KVCacheManager` / `GroupAwareKVCacheManager` |
| L1 / L2 缓存 | `.../l1/l1_cache.py`::`L1Cache`;`.../l2/l2_cache.py`::`L2Cache` |
| L2 connector | `.../l2/connectors/infinistore.py`::`InfiniStoreConnector` 等 |
| RDMA 传输 | `python/aibrix_kvcache/aibrix_kvcache/transport/rdma.py` |

## 参考

- 上游:[github.com/vllm-project/aibrix](https://github.com/vllm-project/aibrix) @ `fe7db93e`
- 白皮书:arXiv [2504.03648](https://arxiv.org/abs/2504.03648)(仓内 `docs/paper/` 有 PDF)
- 路由横评:[`../model-routing.md`](../model-routing.md) §5(AIBrix 节)
- 卸载层对照:[`../flexkv/overview.md`](../flexkv/overview.md)、[`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)
- 分布式模型归类:[`../distributed-models.md`](../distributed-models.md)
- 四栈对比(Dynamo / FlexKV / llm-d / AIBrix):[`../serving-stack-comparison.md`](../serving-stack-comparison.md)
