# Dynamo / FlexKV / llm-d / AIBrix — 四栈对比

> 调研快照:2026-09-11。各项目深挖文档:[dynamo/](dynamo/overview.md)、[flexkv/](flexkv/overview.md)、[llm-d/](llm-d/overview.md)、[aibrix/](aibrix/overview.md);KVCR 见 [kvcr/](kvcr/overview.md)。  
> 这篇回答一个问题:**四个项目看起来都在做"KV 复用 + 智能路由 + PD 分离",区别到底在哪?**

## 1. 一句话各自是什么

1. **Dynamo**(NVIDIA):完整的**分布式推理运行时**——路由、PD 分离、KV 块管理、RDMA 传输、SLA 扩缩全在一个框架里,Rust 核心。
2. **FlexKV**(腾讯云 TACO):一个**引擎旁 KV 卸载库**——只管"GPU 放不下时把 KV 卸到 CPU/SSD/远端",以插件(connector)形态注入现有引擎,不做路由、不做扩缩。
3. **llm-d**(Red Hat/Google/IBM 等):K8s 原生推理栈,**组织下十余个仓**:核心是 EPP 路由器(Endpoint Picker,挂在 Envoy 扩展点上做精确缓存感知选路;我们的 submodule `llm-d-router` 就是这个仓)+ PD 边车编排 + P2P KV 共享(2026-08 公开,实例间 NIXL 直传),外加 WVA 扩缩优化器、KV 索引库、文件系统卸载后端(已上游进 vLLM)、延迟预测器、基准/仿真工具链。
4. **AIBrix**(字节跳动 → vllm-project):K8s **平台积木全家桶**——网关路由、自动扩缩、编排 CRD(K8s 自定义资源)、元数据服务、KV 卸载框架,可按需拼装。

## 2. 为什么看起来相似

1. **同一个生态**:都围绕 vLLM/SGLang,都在 K8s 上跑(FlexKV 除外)。
2. **同一个动机**:KV cache 复用是推理省钱省延迟的关键,大家都围绕它做文章。
3. **同一批技术**:前缀树(radix)/哈希块索引、引擎 KV 事件(ZMQ)、RDMA 传输、Envoy/网关扩展点。
4. **同一个收敛方向**:K8s 侧都在向 Gateway API Inference Extension(K8s 网关 API 的推理扩展标准)靠拢,llm-d EPP 是该标准下的参考实现,production-stack 与 kgateway 都在向它迁移。

相似的是**词汇表**,不同的是**各自动的是哪一层、状态归谁**。下面两节是关键。

## 3. 覆盖的层不同

![分层覆盖](serving-stack-comparison/figures/fig1-layer-coverage.png)

读法:

1. **Dynamo 是唯一全栈**:八层都有自家组件(引擎靠对接,不自研)。
2. **FlexKV 只做中间三层**:卸载、索引、传输;路由、扩缩、编排一概没有——它是库,不是平台。
3. **llm-d 集中在入口与编排**:路由(EPP)、PD 编排、K8s CRD 是核心;KV 卸载走引擎原生通道(它贡献的文件系统后端已上游进 vLLM),扩缩在同组织独立仓(WVA)。
4. **AIBrix 覆盖最广但每块停在平台常规深度**:路由、扩缩、编排、卸载都有,但卸载以引擎为中心(L1 进程内),索引是网关侧估计。

图中 ◐(部分/附属)逐条说明:

- **Dynamo·引擎**:对接 vLLM/SGLang/TRT-LLM,不自研引擎。
- **FlexKV·引擎**:FlexKV 没有引擎,是以 connector 形态注入别人引擎的库,所以该格标"—"但有文字。
- **llm-d·引擎**:vLLM 是父项目依赖,不是 router 仓的组件。
- **llm-d·KV 卸载**:文件系统卸载后端已上游进 vLLM,llm-d 不自维护存储栈;跨实例共享靠 P2P(见 §6.4)。
- **llm-d·数据传输**:PD/P2P 传输由引擎与边车里的 NIXL 连接器执行,router 仓只下发决策。
- **AIBrix·引擎**:对接 vLLM/SGLang,不自研。
- **AIBrix·PD 分离**:只有静态角色编排(StormService),PD 间 KV 传输未落地(代码里是 TODO)。

## 4. KV 状态归谁:最本质的区别

![KV 位置知识归属](serving-stack-comparison/figures/fig2-kv-ownership.png)

四个项目对"**哪个块在哪、能不能用**"这个问题的回答方式完全不同。按"本机/引擎侧"与"远端"分开看(远端对四家都是**可选**的,不是必经路径):

| 项目 | 本机/引擎侧的位置知识 | 远端层的位置知识 | 一致性 |
|------|---------------------|-----------------|--------|
| Dynamo | 两处:router 进程内事件视图(估计)+ 引擎进程内 KVCR 块管理器(记 DRAM/SSD/对象存储驻留) | **对象存储本身无索引**——KVCR 本地记账,对象存储只是字节,只做存在性检查;router 不需要对象清单做选路(KVCR `design_overview.md`) | 事件流最终一致;块管理器私有 |
| FlexKV | 引擎旁插件进程内,每层一棵前缀树(CPU/SSD/远端各一) | **远端(Mooncake Store)有自己的集群级元数据**,跨节点可见性靠 Mooncake 不靠 FlexKV;FlexKV 侧只有可选的 Redis 周期快照 | 本机权威;集群弱一致 |
| llm-d | EPP 各副本各自订阅事件流、各自维护派生索引;引擎是真相但不共享 | FS 卸载**内容寻址、无独立索引**(块键即路径);P2P 共享时 EPP 索引兼作位置目录(只作 hint,握手确认) | 各副本最终一致;幽灵条目可能 |
| AIBrix | 网关索引(哈希表或事件同步两条路线)+ 引擎进程内 L1 | **L2 外部集群有自己的内部元数据**(InfiniStore/HPKV 自管);Redis 只存成员表,不存块位置 | 全部最终一致 |

一句话:**没有一家有全局强一致的 KV 位置权威**——区别只在"估计"发生在哪、有几份、怎么同步。Dynamo 和 llm-d 把估计放在路由器(事件驱动);AIBrix 放在网关(估计或事件二选一);FlexKV 放在引擎旁(本机精确,集群靠快照)。远端层都是可选加成:接的对象存储/共享文件系统可以是"哑"字节(Dynamo、llm-d),也可以是自带索引的存储集群(FlexKV 接 Mooncake、AIBrix 接 InfiniStore)——后者等于把远端的位置知识外包给了存储系统。

## 5. 定位象限

![定位象限](serving-stack-comparison/figures/fig3-positioning.png)

- **FlexKV** 在"库 × 深存储"格:功能面最窄,KV 管理钻得深。
- **llm-d** 在"栈 × 浅 KV"格:路由与编排做透,KV 只做索引,存储交给引擎原生通道。
- **Dynamo** 在"运行时 × 中深 KV"格:什么都管,但 KV 管理仍以引擎进程为单位。
- **AIBrix** 在"平台 × 中深 KV"格:面最广,K8s 耦合最深。

象限坐标的判定依据(纵轴"KV 管理深度"按 §4 核实过的事实打):

- **llm-d 纵轴最低**:自身只有路由器的派生索引;卸载后端上游进了 vLLM(存储语义归引擎),P2P 只是传输编排,没有自有的分层存储系统。
- **Dynamo 居中**:KVCR 在引擎进程内管理 DRAM/SSD/对象存储三层驻留——层数不少,但管理边界是单个引擎进程,没有集群级存储池,故不到"深"。
- **AIBrix 略深于 Dynamo**:L1 在引擎进程内(与 Dynamo 同级),但 L2 是 CRD 部署的外部集群,有集群级容量管理;不过块级元数据归 L2 后端自管、逐出决策在引擎侧,所以也不到 FlexKV 的"深"。
- **FlexKV 最深**:CPU/SSD/远端三层 + 逐层前缀树 + 逐出策略 + 多种传输后端,全部自己实现;它只做这一件事。
- **横轴**(形态):FlexKV 是库;Dynamo 是运行时(框架内什么都有);llm-d 是路由器 + 项目级工具链;AIBrix 是平台积木(全是 CRD,K8s 耦合最深),故最右。

## 6. 逐维度对比

### 6.1 路由

| | Dynamo | FlexKV | llm-d | AIBrix |
|---|---|---|---|---|
| 形态 | 独立 router 进程 | 无 | EPP(挂在 Envoy ext-proc 上) | Envoy ext-proc 网关插件 |
| 索引 | 链式块哈希 + 事件流 | — | 逐块索引 + 推测条目(TTL 2s) | 本地哈希表 / 事件同步索引 |
| 策略 | KV-aware + overlap 量化 | — | 插件 scorer 加权组合(20 种) | 独立策略约 19 种,可加权组合 |
| 多副本 | 各自维护视图 | — | 各自订阅收敛 | 默认各自为政,可选 Redis 同步 |

(ext-proc = Envoy 的外部处理协议:转发请求前先调外部服务要决策。两家都挂在这同一个扩展点上。)

**小结**:llm-d 与 AIBrix 都挂在 Envoy ext-proc 上,差别在索引精度(llm-d 逐块精确 + 推测补窗;AIBrix 两条路线并存)和插件体系规整度(llm-d 更规整);Dynamo 走自家 router 进程,不依赖 Envoy;FlexKV 不参赛。

### 6.2 PD 分离

| | Dynamo | FlexKV | llm-d | AIBrix |
|---|---|---|---|---|
| 地位 | 一等公民,旗舰特性 | 无关 | 主路径(边车代理)+ 备选(独立编排服务) | 静态角色(StormService)+ 雏形 |
| 决策 | 部署拓扑 + planner | — | 逐请求 decider(可按前缀命中决定不拆) | 部署拓扑为主 |
| 执行 | NIXL 直传 | — | decode 先选,边车替它串 prefill | PD + Mooncake 传输未落地(TODO) |

(边车 = 与引擎同 pod 的代理容器,替 decode 引擎向 prefill 发请求、接 KV;StormService = AIBrix 自研的 K8s CRD,把 prefill/decode 等不同角色的 pod 编成一组整体部署。NIXL = NVIDIA 的 RDMA 传输库。)

**小结**:Dynamo 把 PD 当默认架构;llm-d 把 PD 做成可编排的多阶段流水线,且"先选 decode 再倒推 prefill"的决策顺序最讲究;AIBrix 的 PD 更多是编排层概念;FlexKV 不涉及。

### 6.3 自动扩缩

先说清三家各是什么( FlexKV 是库,不管扩缩,下表不再出现):

- **Dynamo Planner**:框架内置的扩缩器。工作方式是**先测后算**——先用 profiler 实测出"负载 → TTFT/ITL"的性能曲线,运行时拿当前负载对照曲线反推:要满足 SLO,prefill 需要几个实例、decode 需要几个实例。**prefill 和 decode 独立扩缩**(分别输出 `predicted_num_prefill_replicas` 和 `predicted_num_decode_replicas` 两个副本数)。
- **llm-d WVA**(Workload Variant Autoscaler,工作负载变体扩缩器;同组织独立仓 `llm-d-autoscaling`):"变体"指**同一模型的不同部署配置**(不同 GPU 型号、不同并行方式,成本和容量各不相同)。WVA 读 Prometheus 指标(KV 利用率、队列深度、饱和度),解一个优化问题:每个变体该扩到几个副本,**便宜的配置先扩、贵的先缩**。它只算目标副本数,真正执行扩缩的是 K8s 标准组件(HPA/KEDA)。
- **AIBrix PodAutoscaler**:框架内 CRD,提供三种算法任选——HPA(K8s 原生,按指标)、KPA(Knative 风格,按并发数)、APA(自研,按 KV 使用率/排队长度/延迟);APA 可接 GPU Optimizer 处理异构机型。

| | Dynamo Planner | llm-d WVA | AIBrix PodAutoscaler |
|---|---|---|---|
| 类型 | SLA 反推型 | 优化器型 | 控制器型 |
| 输入 | 实测性能曲线 + 当前负载 | KV 利用率/队列深度/饱和度 | KV 使用率/排队长度/延迟 |
| 输出 | prefill、decode **各自**的副本数 | 每个变体的目标副本数 | 整体副本数 |
| 执行 | 框架内直接执行 | 交标准 HPA/KEDA 执行 | 框架内 CRD 直接执行 |

**小结**:三家都做到了 KV 感知的扩缩,差别在建模方式——Dynamo 要先用 profiler 标定曲线(准,但要标定步骤);WVA 把扩缩当优化问题解,且考虑异构成本(有论文,arXiv 2603.09730:比 HPA 有效吞吐 +37%、请求失败降 10×);AIBrix 是传统的指标驱动控制器,算法可选。Dynamo 的 P/D 独立扩缩是三者中唯一按角色分别出副本数的。

### 6.4 KV 卸载与存储

| | Dynamo | FlexKV | llm-d | AIBrix |
|---|---|---|---|---|
| 组件 | KVBM(KV 块管理器,已 sunset)→ KVCR(继任者) | FlexKV 本体 | llmd-fs-backend(文件系统卸载)+ P2P 共享(实例间直传) | aibrix_kvcache |
| 层 | GPU→CPU→SSD→对象存储 | CPU/SSD/远端 | GPU↔CPU/共享文件系统 + 对等节点 CPU 层 | L1 DRAM + L2 外部集群 |
| 位置 | 引擎进程内 | connector 进程内 | 引擎侧(vLLM 原生卸载连接器);P2P 无中心数据面 | 引擎进程内 + 外部集群 |
| 传输 | NIXL(RDMA) | io_uring/GDS/Mooncake TE | 文件系统读写 + NIXL(CPU↔CPU) | RDMA/TCP |

(io_uring = Linux 异步 IO;GDS = GPU Direct Storage,盘与显存直传;KVCR 细节见 [kvcr/](kvcr/overview.md)。)

**小结**:FlexKV 与 aibrix_kvcache 严格同层(引擎旁卸载框架),FlexKV 层数更多、引擎接入面更宽;Dynamo 的 KVBM 已官宣 sunset,继任者 KVCR 换成"引擎进程内二级存储 + router hint 驱动 P2P"的路子;llm-d 不自建存储栈——文件系统卸载后端已上游进 vLLM,跨实例共享走 P2P(EPP 索引指路 + NIXL CPU↔CPU 直传),存储语义留在引擎原生通道里。

### 6.5 分布式模型与 HA

按三个模块分开看——路由层、KV 存储层、实例控制面(编排与元数据),各项目的分布式模型在三个模块上并不相同:

| 模块 | Dynamo | FlexKV | llm-d | AIBrix |
|------|--------|--------|-------|--------|
| **路由层** | 独立 router 进程,各副本各自消费事件流收敛视图;无状态可水平扩 | 无此层 | EPP 多副本各自订阅事件流;HA 三模式:Active-Passive 选主 / fail-open 直打 / Active-Active(近似前缀下应避免) | 网关插件无状态,多副本默认各自为政,可选 Redis 同步索引 |
| **KV 存储层** | KVCR 在引擎进程内,随引擎生死;对象存储可选、无自有索引;跨节点共享靠 router hint + P2P | 本机前缀树随引擎进程;远端可选(Mooncake 自带集群元数据与 HA);Redis 快照可选 | FS 后端靠文件系统自身语义;P2P 无中心数据面;都无位置权威 | L1 随 pod 消亡;L2 外部集群由 CRD 部署,块元数据后端自管,成员表在 Redis(单点风险) |
| **实例控制面** | operator + 部署 CRD,元数据在 K8s etcd;控制面事件走 etcd/nats(可集群化) | 无此层 | InferencePool 等 CRD,K8s etcd 权威 | CRD 全家桶 + Metadata Service(Redis);编排元数据在 K8s etcd |

**小结**:四家都属于"事件/快照最终一致"这一类(distributed-models.md 的 C 类);没有一家给 KV 位置知识配强一致权威。分模块看,HA 思路最直白的是 llm-d 路由层:索引是派生的,丢了重建,所以敢 fail-open;KV 存储层四家都接受"缓存丢了就重算"的语义,没人给 KV 数据面做强 HA——这与 lake"存储池是长期存续基础设施、L2 为恢复点"的定位是本质分歧。

### 6.6 其余硬条件

| | Dynamo | FlexKV | llm-d | AIBrix |
|---|---|---|---|---|
| 语言 | Rust 核心 + Python | C++ 内核 + Python | 几乎纯 Go | Go + Python + TS |
| K8s 耦合 | 可独立可 K8s(operator + 部署 CRD) | 无 | 深(Gateway API 标准) | 最深(全是 CRD) |
| 引擎 | vLLM/SGLang/TRT-LLM | vLLM/SGLang/TRT-LLM/Dynamo | vLLM 为主 | vLLM/SGLang |
| 背景 | NVIDIA | 腾讯云 TACO | Red Hat/Google/IBM 等 | 字节跳动捐给 vllm-project |

## 7. 它们之间不是纯竞争

1. **FlexKV 可以插进 Dynamo**:Dynamo 官方支持 `--connector flexkv`,卸载库与运行时组合使用。
2. **llm-d EPP 是 K8s 路由的收敛方向**:production-stack(#1032)与 kgateway 都在向 Gateway API Inference Extension + EPP 迁移,AIBrix 自研网关长期看也面对这个标准。
3. **llm-d 与 AIBrix 消费同一类事件**:都是 vLLM 的 ZMQ KV 事件,各自实现订阅管线;生态上可能进一步共用。
4. **Dynamo KVBM sunset 后的空位**由 KVCR(引擎内二级存储)接,与 FlexKV/AIBrix 的卸载框架形成同层竞争。
5. **llm-d 在把通用能力上游化**:KV 索引库从 kv-cache 仓迁入 router 仓([llm-d-router#1886](https://github.com/llm-d/llm-d-router/pull/1886)),文件系统卸载后端并入 vLLM 多层级卸载连接器——能推给标准/引擎的就不自己扛。
6. **"路由器指路 + 引擎间 NIXL 直传"在收敛**:llm-d P2P 共享(EPP 索引当位置目录 + NIXL CPU↔CPU)与 Dynamo KVCR(router hint 驱动跨节点 P2P)同构——两家独立走到同一个模式,说明这是 PD 分离之外 KV 复用的第二个收敛点。

## 8. 怎么选(场景导向)

1. **要开箱即用的完整 PD 分离栈,且在 NVIDIA 生态** → Dynamo。
2. **已有 vLLM/SGLang 服务,只想加 KV 卸载,不想动平台** → FlexKV(免补丁、库形态)。
3. **要 K8s 标准路线、精确缓存感知路由、可插拔策略** → llm-d(EPP + InferencePool);配套的无 GPU 模拟器与基准工具链也是四家里最齐的。
4. **要从零搭平台,路由/扩缩/编排/元数据/卸载全家桶一次拿齐** → AIBrix。
5. **只想要一块能搬走的**:路由插件形态看 llm-d;卸载框架看 FlexKV;扩缩指标选型看 AIBrix 与 WVA。

---

各项目与 lake 的逐条对照,见各自的 pain-points 文档:[dynamo](dynamo/overview.md) · [flexkv](flexkv/pain-points.md) · [llm-d](llm-d/pain-points.md) · [aibrix](aibrix/pain-points.md)。
