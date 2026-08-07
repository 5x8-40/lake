# 06 — 路由与调度

> 调度分四层(请求级路由 / 池间 / 节点级 / 弹性)。**模式选择与请求生命周期权威描述见 [`data-flow.md`](data-flow.md)**(决策树 + 三模式执行段 + F4 分支);**执行模式与 KV 流转时序见 [`execution-modes.md`](execution-modes.md)**(本地完成 / 跨节点传输两条时序)。本文讲调度层各层次的机制:模式选择依存储池本地命中、prompt 规模、传输成本逐请求决策(见 [`../features/features.md`](../features/features.md) "执行模式"节)。

调度是存算分离系统能否兑现弹性与低延迟承诺的控制核心。本系统调度器**无状态**（所有决策依据来自控制面的共享视图），可水平扩展。

## 调度层次

```
1. 请求级路由 (Gateway 准入 / Router 选路)
2. 池间调度 (Prefill ↔ Decode ↔ Draft)
3. 节点级调度 (continuous batching、内存分配)
4. 弹性调度 (扩缩容)
```

## 1. 请求级路由

输入：请求 `(model_id, prompt_tokens, max_tokens, SLO)`。

步骤：
1. **前缀解析**：Router 读**本地命中视图镜像**（radix 前缀结构 + 位置视图的全局副本，零 RPC），得到可复用 KV block 列表、对应物理位置，以及是否已**本地命中**（前缀 KV 是否已在某执行节点 HBM，支撑 D-direct）。本地命中判定本身需要全局信息（哪个节点 HBM 有），故镜像内容是全局的、副本存本地。
   - **镜像来源与刷新**：位置视图权威在**控制面进程内存**（单写者线性一致，etcd 只存降频 checkpoint），Router 与各节点 in-process agent 各持一份本地镜像，由控制面**推送**刷新（gRPC stream 主方案，同机走共享内存直读，见 [`control-plane.md`](control-plane.md)「Router 持位置视图镜像」），非主动拉。触发推送的事件 = 位置视图权威变更：block 放置到 L0、驱逐覆写、迁移、满块注册进 radix。**ref 归 0（变可驱逐候选）不触发推送**——未驱逐覆写则位置视图不摘（B1 闭环），仍可命中/直传；只有驱逐覆写才摘视图并推送。
   - **陈旧兜底**：推送异步有延迟，镜像最终一致。陈旧只影响命中率/传输成本，不影响正确性——误判本地命中时，agent pull 向控制面确认发现已不在 → 从池（L1/L2，未命中退 L3 SSOT）回填（多一次回填），与 compute-layer Q1 的"本地视图是镜像、全局权威在控制面、miss 回填兜底"语义一致。热点前缀（system prompt 等）变动少，镜像基本不陈旧；陈旧风险主要在冷门 block，miss 代价也小。
2. **Prefill 节点选择**：
   - 亲和性：优先选 KV block 所在的 KV Node 附近的 Prefill 节点（减少传输）。
   - 负载：考虑队列长度、HBM 放置余量（HBM 由存储池统一管理，Router 读存储池的 L0 容量视图）。
   - 目标：最小化 (增量 prefill 计算 + KV 传输) 的加权和。
3. **Decode 节点预分配**：在 Prefill 完成前就选定目标 Decode 节点，由存储池把 KV 放置到其 HBM（本地命中优先）。
   - **消歧（请求路径内放置 ≠ 方案 Z 后台预放置）**：此处"由存储池把 KV 放置到 D 的 HBM"是**请求路径内**的新产出 KV 放置（prefill 刚产出的 KV 必须现放，与时序二正向"与 A 计算重叠"一致，见 [`execution-modes.md`](execution-modes.md)），**不是**方案 Z 的后台热度预放置（方案 Z 是请求到达前按热度通用预放置、发布位置视图，不感知具体 batch，见 [`storage-layer.md`](storage-layer.md) "放置与 batch 的职责边界"）。两者都是池主动放置、调度单向消费,但触发时机与对象不同:后台预放置=热度驱动为本地命中攒数据(供 D-direct);请求路径内放置=新 KV 必现放(供本次 PD 传递)。守方案 Z 单向耦合——调度只读视图、不指挥放置,请求路径内放置也由池在选路结果上自主执行。
4. **SLO 感知与优先级调度（Router 职责）**：Router 在选路时纳入 SLO 预算（如 D-direct 模式选择开销须 < 5ms，否则吃掉本地命中省传输的收益）与请求优先级——**优先级队列**决定已准入请求的执行顺序与抢占（被抢占者 KV 在存储池保留，见第 3 节）。**不设降级链**：若选不到合适节点或执行失败（故障/超时），不写 mode-to-mode 的预设 fallback、不"拒绝"已准入请求，而是触发 F4 故障恢复 → Router 依最新集群状态重跑 `f(请求, 集群状态) → (模式, 节点)` 重选模选点。
   - **边界**：过载层面的**入口准入 / 限并发 / 按优先级丢弃**归 gateway/外部控制面（决定请求"进/不进"，过载拒绝不计推理系统失败率）；Router 只对**已准入**请求做 SLO 路由与优先级**排队顺序**（决定"去哪/何时/怎么跑"，不丢请求）。Worker 不自 shedding，只上报剩余容量、队列长度、in-flight 等信号。详见 [`../features/slo.md`](../features/slo.md) "过载控制"节。

### 1.1 选路形态：KV 感知 Router 与 External 式分发（倾向）

> 对照参考：vLLM Internal / Hybrid / External LB、SGLang `DataParallelController`——见 [`../research/sglang/model-runner.md`](../research/sglang/model-runner.md)「Data Parallel」。本节落 lake 取舍。

**问题**：有了**命中视图驱动的 Router**（§1 前缀解析 + 模式/节点选择）之后，计算侧还要不要再做一层「DP 内部 LB」（类 vLLM `DPLBAsyncMPClient` / SGLang Controller）？

**结论（倾向）**：对外选路权威收束到 **Router 这一层**；计算节点按部署形态独立或联合执行，**默认不再在引擎内做跨 rank 二次分发**——形态上对齐 vLLM **External LB**（外部决定去哪个 rank/端点），而不是 Internal（head 上 DPLB 在全集 engine 里打分）或 Hybrid（上游选机 + 机内再 DPLB）。

| 角色 | 职责 |
|------|------|
| **Gateway（Bifrost）** | 鉴权 / 限流 / 过载准入（进不进；去哪归 Router） |
| **Router**（逻辑单一选路面） | `f(请求, 命中视图镜像, 负载信号) → (模式, 节点/rank)`；**完全决定**请求落到哪个执行端点 |
| **计算端点** | 只消费已派发请求：节点级 continuous batching；副本内若 TP/PP 联合，则按「一份调度 + 多卡执行」锁步（见 research TP/PP 节），**不**再选别的 DP rank |

**「对外 1 个 Router」的精确含义**：

- **逻辑上**只有一层 KV 感知选路（Router），客户端/Gateway 不直连各 GPU rank 的私有 LB。
- **物理上** Router **仍可水平扩展**（多实例、无状态、共用命中视图镜像推送；见下「调度器一致性」乐观并发）——扩展的是同质选路面，不是「每机再嵌一套 DPLB」。
- 与「4 机 32dp、head 上默认 32 个 `DPLBAsyncMPClient`」的 Internal 默认相反：lake **不把**「在 32 个 EngineCore 上打分」做成计算栈内建能力。

**计算节点：独立 vs 联合**（Router 选完之后）：

| 部署形态 | Router 选的「点」 | 计算侧行为 |
|----------|------------------|------------|
| 独立副本（每卡/每机组一个可服务端点，普通 DP） | 直接选该端点 | 该端点独立 Scheduler/队列；其它副本无感（除非 MoE/集体通信要 IDLE/dummy 陪跑） |
| 联合副本（单端点内 TP×PP） | 选该联合组的入口（一组 GPU 的逻辑 rank0 / EngineCore） | 组内 Executor/广播锁步；**组内不分 DP** |
| 混合 | Router 输出仍是「逻辑执行单元」ID | 单元内部拓扑是部署配置，不进入二次选路 |

**对照 SGLang 双层管理**（详见 [`../research/sglang/model-runner.md`](../research/sglang/model-runner.md)「SGLang 双层管理模式」）：

| SGLang | 说明 | lake |
|--------|------|------|
| **层 B** `sgl-model-gateway` | 独立 Rust 网关；默认 **`cache_aware`**：按请求历史建近似 radix + 失衡时最短队列；PD 分选 P/D worker | **方向对齐**——智能在网关；lake 用**存储池命中视图**(真本地命中)替换近似文本树 |
| **层 A** `DataParallelController` | 单次 serve 内按 ROUND_ROBIN / 负载再选 `dp_rank`；可用 `routed_dp_rank` 透传跳过 | **默认去掉权威二次分发**；避免「gateway 选机 + 机内再选 rank」 |
| 层 B 演进 | [#25760](https://github.com/sgl-project/sglang/issues/25760) SessionAware；`experimental/sgl-router` `cache_aware_zmq`(吃 KVEvent)；[#31458](https://github.com/sgl-project/sglang/issues/31458) KV Indexer——**全局 Router 调度与 lake 问题域重叠**，仍旁路/最终一致 | 可借鉴事件→索引形态；权威仍用 etcd **强一致**位置视图，不照搬旁路 Indexer |
| kv_events / Indexer | 旁路、最终一致，补 cache_aware 真值；未取消层 A | lake 控制面强一致视图 + **唯一**选路面 |

即：SGLang 生产常是 External(网关) + Internal(引擎 DP) **叠加**，并正把层 B 从「猜历史」推到「吃事件 / 独立 Indexer」；lake 只保留并强化「网关那一层」为唯一选路权威，且用池命中视图而非旁路目录。细节见 [`../research/sglang/model-runner.md`](../research/sglang/model-runner.md)「层 B 演进」、[`../research/sglang/pain-points.md`](../research/sglang/pain-points.md) §1.2。

**为何倾向 External 式**：

1. **KV/本地命中是一等输入**——只有持全局命中视图镜像的 Router 能正确做 D-direct / 前缀亲和；引擎内 waiting/running 打分**看不见**存储池放置，二次 LB 会稀释甚至抵消命中收益。SGLang 层 A 即此问题；层 B 生产仍是近似树，`cache_aware_zmq`/Indexer 仍弱于池权威。
2. **职责边界**：过载归 Gateway，选模选点归 Router，执行归 Worker——引擎内再 LB 会把「去哪」拆成两段，难守 5ms 模式选择预算与单向耦合（方案 Z）。
3. **对照**：vLLM External = 外部定 rank；SGLang 层 B = 外部定 worker(+软/演进中的事件 cache-aware)；lake Router ≈ 二者之上 + 真命中视图。队列打分只作**负载信号输入**（worker 上报 → Router 加权），不作为第二层权威分发。

**开放细节（不阻塞本倾向）**：

- Router 多实例时如何避免同前缀热点扎堆（已有开放问题「前缀亲和 vs 负载」）。
- MoE/dp-attn 类「有活则全体陪跑」时，Router 是否需感知 collective 组拓扑（选一个逻辑单元而非单卡）。
- 显式 `routed_rank` 调试接口与生产默认路径的关系。

## 2. 池间调度

> 本节「Prefill / Decode / Draft 池」指**部署与扩缩画像**(逻辑池),不是 `python/lake/{prefill,decode,draft}/` 代码包——进程内只有一套 `lake.engine`,角色由启动配置选择。见 [`compute-layer.md`](compute-layer.md)「池 ≠ 代码包」。

- Prefill → Decode 的 KV 传递通过存储池传输引擎(RDMA 数据面,见 [`kv-cache-pool.md`](kv-cache-pool.md) "跨实例 KV 传输"),需在 Prefill 完成时序上对齐 Decode 就绪（**仅 PD 分离模式**;混部/D-direct 为本地完成、无跨节点传输,见 [`execution-modes.md`](execution-modes.md)）。
- 投机解码：Draft 池(部署画像;默认 drafter 与 Decode 共置,独立 Draft 池可选)在 Decode 侧生成候选，验证失败回退到标准 decode。
- 反压：Decode 池拥塞时，减缓 Prefill 速率（背压），避免 KV Pool 堆积。属池间内部流控（不丢请求、不降 batch），区别于 gateway 的请求级 shedding。

## 3. 节点级调度

- **落点**:`python/lake/runtime/node_scheduler.py` → 产出 `SchedulerOutput`(节点侧;字段草图见 [`compute-layer.md`](compute-layer.md)「D1 — SchedulerOutput 字段草图」)。集群选路仍归 Go Router;计算节点内**一份**调度决策扇出多卡(见 compute-layer TP)。
- **Host `Req` 权威**:**完全**在 `node_scheduler`(token 历史、采样/stop/grammar、结束判定);`ModelRunner` 无长期请求表。见 [`compute-layer.md`](compute-layer.md) 决策 5。
- **默认 overlap**:主循环对齐 SGLang `event_loop_overlap`(CPU 收尾 ∥ 下一 GPU forward;device 侧 token 接力)。请求结束 → `agent.on_request_finished`(见 compute-layer「请求结束与资源释放」)。
- **Continuous batching**：Decode / 混部节点动态拼接 batch;执行形态由角色配置 + 本步 `SchedulerOutput` 选择(同一 `lake.engine`,非 prefill/decode 分树)。
- **PagedAttention** 风格的块状 KV 管理，与存储池的 block 粒度对齐(表由池 agent 组装,调度器不持 KV 权威)。
- **放置与 batch 单向耦合（方案 Z）**：同一 batch 各 sequence 的 KV 必须同时在本机 HBM（attention 一次读全部）。存储池按热度主动预放置 KV 到 HBM 并发布位置视图;调度器读视图组 batch（本地命中优先），缺失补拉，不反向指挥放置。见 [`storage-layer.md`](storage-layer.md) / [`execution-modes.md`](execution-modes.md)。
- **一步交互序（D5 已定）**：`schedule`（只读视图）→ `prepare_step`（**唯一**补拉/占槽/ready）→ `execute` → `done` → 结束则 `on_request_finished`。补拉预算 `pull_budget_ms`（0=同步等到齐）；默认 **不允许批内缺块请求仍进算**（`allow_partial_hit=false` = all-or-nothing；与单请求前缀部分命中无关）。`allow_partial_hit=true` 时 agent 回缩 `effective_*_set`，调度器须过滤后再 execute。详见 [`compute-layer.md`](compute-layer.md)「D5」。
- **三模式 + vLLM 调度几何（C5）**：节点消费 `PrefixHint`；`mode_select` 为纯函数骨架（生产权威在 Go Router）。`local_hit`（含部分）→ `ExecMode.D_DIRECT`；**整段**本地命中只把 `num_computed=prompt_len`，下一步按生成几何 schedule（**无** SGLang `PREBUILT` 分相）。Runner / prepare 只吃 `num_scheduled_tokens` + read/write 几何。
- **抢占**：高优先级请求可抢占低优先级，被抢占者的 KV 在存储池中保留（不丢失，本机 HBM 放置释放归还存储池）。

### 3.1 DP 间 step 信息同步(落节点 Scheduler)

> **与 §1.1 正交**:§1.1 否定的是跨 DP 的**二次选路/LB**;本节定的是集体通信所需的 **step 元数据同步**(token 数、可否 graph、forward mode 等)。前者归 Router;后者归节点 Scheduler。引擎(`ModelRunner`)不发起该同步。

**已定**:需要 DP/EP 锁步(如 dp-attn、MoE gathered buffer、需 IDLE 陪跑)时,lake 在 **`runtime/node_scheduler`** 这一层做 DP 间信息同步——对齐 SGLang `prepare_mlp_sync_batch` 的层级,不放进 `engine/model_runner`。

```
各 DP rank: node_scheduler 组本地 batch
        → sync(all_gather 本步 num_tokens / mode / graph 可行性…)
        → 空闲 rank 按需造 IDLE 陪跑
        → 写出 SchedulerOutput(含 global_num_tokens 等)
        → ModelRunner 只消费已同步字段做 pad / forward(不自行 all_gather 计数)
```

| 同步什么(初版方向) | 用途 |
|--------------------|------|
| 每 rank `num_tokens` / `num_tokens_for_logprob` | pad-to-max、MLP gather 形状 |
| local forward mode / 是否有活 batch | 决定 IDLE 陪跑 vs 全局空闲 `on_idle` |
| can_cuda_graph 等 step 标志 | 跨 rank 取交集,避免一 rank graph 一 rank eager 致 hang |

**参考**:SGLang `managers/scheduler_components/dp_attn.py::prepare_mlp_sync_batch_raw` + `MLPSyncBatchInfo.all_gather`(Scheduler 在 `run_batch` 前 gather);ModelRunner 侧 `forward_batch.prepare_mlp_sync_batch` 仅**消费**已写入的 `global_num_tokens`。见 [`../research/sglang/model-runner.md`](../research/sglang/model-runner.md)「Data Parallel」。vLLM 对等语义在 `dp_utils.sync_cudagraph_and_dp_padding` / `execute_dummy_batch`,协调偏 EngineCore+Coordinator——lake 显式选 SGLang 的 **Scheduler 层**落点。

**不做**:把 token 数 sync 塞进 `pool_iface`/存储池;不把该 sync 做成 Router 热路径(Router 只选端点,不知每 step batch 形状)。

**D1 已定**：`global_num_tokens` / `can_run_graph` 进 `SchedulerOutput`；单卡或无需 collective 时字段为 `None` 并跳过 sync。仍待补(D5/D8)：进程组与 TP 组关系、与 headroom 规避 drafter-skip 的衔接。

## 缓存命中感知调度

缓存命中是调度的**一等输入**——模式选择(请求级)与 batch 组成(节点级)都由命中视图驱动。本节把散见于各层的命中感知要点收敛,并补一条此前未显式写的**跨请求前缀共调度**。

**两层命中(均为存储池权威元数据,见 [`features.md`](../features/features.md) "Pool 命中 vs 本地命中")**:
- **Pool 命中**:前缀 KV 在分布式池 → 省重算,但仍需传输(驱动 PD 分离 / 混部选路,见第 1 节)。
- **本地命中**:前缀 KV 已被存储池放置在某执行节点 HBM → 可 D-direct,零/极小传输(驱动节点选择 + 残差 prefill 工作量)。

**跨请求前缀共调度**(节点级):把**共享公共前缀**的多个请求尽量组到同一 batch / 同一节点,使前缀 block 在 batch 内复用、本地命中叠加。参考 SGLang RadixAttention 的 cache-aware scheduling(`radix_cache.py::match_prefix` 驱动 scheduler 把同前缀请求 co-schedule)、vLLM `KVCacheManager.get_computed_blocks`(逐请求查前缀块、命中数影响调度顺序)。收益:同前缀请求共节点 → 共享同一批前缀 block 的 HBM 放置,本地命中密度提升、传输减少。

**请求级亲和选路(P7.6 B1,已落地)**:Router 选点为**两段式**(`go/router/affinity.go::pickNodeForRequest`,纯内存 µs 级):①**热路径**——逐 ready 节点在镜像上算本地前缀得分(命中前缀中在请求方 L0 的连续块数),得分最高者胜出;其 per-node in-flight ≥ 护栏(默认 8,`Config.AffinityInFlightGuard`)则降级次优,全部过载落②。②**冷路径**——纯整数 rendezvous(HRW):`score = fnv1a64(routingKey‖node)` 取 max、平局按节点 id 升序,与池侧 B2-a 预放置(`placement.rs::preplace_on_register`)严格同构;负载经护栏过滤表达(in-flight ≥ 护栏的节点先剔除,全过载退无过滤纯 HRW),**不进 score**——float64 加权在 u64→f64 精度碰撞或负载不等时会与池侧家节点分叉(review #69)。路由键 = `X-Lake-Session-Id` 头优先,否则有界深度前缀锚点 `hashes[min(len,8)-1]`(深度 1 会被全局系统前缀打成热点)。in-flight 记账在 `nodeRegistry`(**选路即占 +1 / 请求完成 -1**,含排队与抢占重试在途——护栏看得见排队中的亲和负载;原先 dispatch 前才 +1 有 TOCTOU + queue-blind 缺陷,review #69)——是选路热路径的即时本地信号,区别于 loadsync 的出站遥测上报。参考 Dynamo `lib/kv-router/src/protocols.rs::WorkerSelectionResult`(overlap_blocks 命中量化为一等输入)与 SGLang `sgl-model-gateway/src/policies/cache_aware.rs::CacheAwarePolicy`(失衡退最短队列);**关键差异**:reference 是 routing-to-history 近似树(按请求文本历史猜亲和、旁路最终一致),lake 是**池权威视图镜像上的真本地命中**(得分即 D-direct 资格)+ **池侧放置闭环**(B2 跟随流量放置 + HRW 预放置把命中数据造到 Router 将要选的节点上,见 [`kv-cache-pool.md`](kv-cache-pool.md))。冷路径哈希与池侧 B2-a 预放置同字节布局(FNV-1a 64,`placement.rs::hrw_score`),负载未饱和时 Router 与池**逐点同家**。实测(RR 0.106–0.230 → 亲和 0.905–1.000,连续本地前缀口径)见 [`../features/slo.md`](../features/slo.md)「本地命中率」P7.6 校准。抢占重跑节点沿用「Submit 前选一次」(跨节点 retry 重选与 F4 重路由统一设计,未来工作),但 **hint/模式每次 attempt 重算**——镜像可能已变(预放置/驱逐/新注册),冻结三元组会在视图变化后错模(review #69)。

**边界(重申,守方案 Z 单向耦合)**:
- 调度**只读**命中视图,不反向指挥存储池放置。存储池按热度主动预放置并发布位置视图;调度读视图组 batch(本地命中优先→D-direct,缺失补拉)。信息流单向。
- 命中视图由存储池权威维护,陈旧只影响命中率(miss→pull→控制面确认),不影响正确性(与 [`kv-cache-pool.md`](kv-cache-pool.md) "block table 池组装"的本地视图镜像语义一致)。
- **投机解码的 draft 候选 token(未验证)不进 radix**;但 **drafter 自己的 KV 与 target KV 同款进池、跨请求前缀复用**(SGLang `PoolName.DRAFT`),seed hidden 是否跨请求缓存待定(先按重算式,见 [`compute-layer.md`](compute-layer.md) "投机解码")。命中感知对 target KV 与 drafter KV 均适用(t-type 与 r-type 均含,复用条件一致——都按全前缀命中,见 [`kv-cache-pool.md`](kv-cache-pool.md) "t-type / r-type")。
- 前缀共调度与"前缀亲和性引发热点"(见下开放问题)存在张力:把所有同前缀请求固定到一个节点会过载。共调度目标是 batch 内复用,非全局集中;负载均衡由 Router 在节点选择时加权(见第 1 节 Prefill 节点选择)。

## DP 间在途再均衡(抢占重算式,未来特性 / 框架预留分析)

> **定位**:请求迁移是**未来特性**,本节现在讨论的目的是**确认框架无需为它做特别设计**(避免后续大重构),不是当前实现项。多 DP 部署下若各 DP 计算负载严重不均衡,需要把已在跑的请求换到别的 DP 执行。

**为什么不均衡有代价**:DP forward 是 lockstep + **padding-to-max**(vLLM `dp_utils.py::sync_cudagraph_and_dp_padding`:每 step 全体 pad 到 `num_tokens_across_dp.max()`),最闲的 rank 被最忙的拖着空跑。故**再均衡目标是降低 max(按 token/计算量衡量),不是拉平请求条数**。

**参考实现的现状**:SGLang `data_parallel_controller.py::LoadBalanceMethod`(ROUND_ROBIN/TOTAL_REQUESTS/TOTAL_TOKENS/FOLLOW_BOOTSTRAP_ROOM)与 vLLM DP 均**只在准入/派发时选 DP**,**都不做在途 DP↔DP 迁移**。lake 因存算分离,KV(target + drafter)归池权威,迁移代价远低于两者,才使在途再均衡可行。

**机制:抢占重算式(非活迁)**——参考 vLLM v1 `scheduler.py::_preempt_request`(free blocks + `num_computed_tokens=0` + 塞回 waiting,v1 已用重算式取代 v0 的 swap-to-CPU):
1. 带外监控:worker 异步上报负载信号(队列/in-flight/pad 后 token 数),**控制面带外决策,不进 hot step loop**(守原则 3:worker 只上报,不自决)。
2. 决定迁移 → **停止推理、退出 running**(不在 running 即无 in-flight 冻结冲突,天然原子)。
3. **记录已推进长度**,请求回 waiting → 重派目标 DP。
4. 目标 DP 重新准入:前缀 KV(target + drafter,均在池)**前缀命中**→ 命中式 re-prefill(seed hidden 按重算式由 draft-extend 重建);"重算"基本退化为一次命中重放。
5. "驱逐到 CPU 暂等"在 lake 里就是池把 L0→L1 降级,不需专门 swap 通道。

**控制态交接**(重算式下绝大多数随 request 重派或可由 token 历史重建,**硬核只两项**):
- **必须显式随迁**:① 采样 RNG generator state(seeded 时不迁则续写偏离);② 结构化解码 FSM 游标(guided/regex/ebnf/json,或在目标端重放已生成 token 复原)。参考实现里 FSM 在 host、仅 bitmask apply 上 GPU,见 [`../research/guided-decoding.md`](../research/guided-decoding.md)。
- **随 request 天然携带**:已生成 token 序列(= re-prefill 输入)、采样参数、max/min_tokens、优先级/SLO deadline。
- **可由 token 历史重算**:rep/freq/presence penalty、logprobs 游标、stop-string 部分匹配。
- **丢弃 + 目标端重建**:in-flight draft token(vLLM 抢占即清 `spec_token_ids`)→ 目标端 drafter `post_forward` 重新 seed。

**挑选与边界**:
- 迁移**计划单机内**(DP 间,同权重/同并行配置),传输走 NVLink/PCIe;"传输"本质是池的 **L0↔L0 同层迁移**(复用现有跨节点/同层迁移 + 后台带宽池 <10%),非 drafter/worker 私有通道。
- **优先迁短的**(重算式下短请求重放便宜);避开前缀亲和请求(迁走丢本地命中);加滞回/驻留阈值防抖动。
- **非故障(不走 F4)、非过载(不归 gateway shedding)**:这是节点级/弹性调度里的主动再均衡触发,与"模式选择纯函数"分开(模式选择是准入/重路由算一次,再均衡是对已在跑请求的周期策略)。

## 4. 弹性调度

触发指标：
- Prefill 池：队列长度 > 阈值 / TTFT 接近 SLO 上限 → 扩容。
- Decode 池：ITL P99 接近 SLO / QPS 上升 → 扩容。
- KV Pool：命中率下降 / 容量水位高 → 扩容 KV Node 或下沉冷数据到 L3（SSOT）。

缩容策略：
- 选最闲节点 Drain，in-flight 完成后销毁。
- KV 已在 Pool，无状态丢失。

## 调度器一致性

- **控制面强一致**（etcd）：KV block 位置表、节点拓扑、路由决策日志。
- **数据面最终一致**：调度器读到的负载视图有延迟，调度决策应**幂等可重试**，容忍基于陈旧信息的次优决策。
- **乐观并发**：多个 Router 实例并发决策，冲突时由控制面仲裁（如同一 Prefill 节点被超额分配 → 触发再路由）。

## 开放问题

- 前缀亲和性与负载均衡的冲突：把所有相同前缀请求固定到一个节点会引发热点。
- 预测性扩容：基于流量历史提前预热节点，模型如何选？
- 公平性：多租户下 KV 复用带来的成本节约如何在租户间分配？（多租户归外部，lake 不做；此为外部计费/公平性议题）
- **DP 在途再均衡的多次迁移(遗留)**:同一请求反复迁移的防抖/防饿死(参考 vLLM `num_preemptions` 计数)、迁移次数上限、滞回阈值取值。
- **不均衡的根因判定(遗留)**:imbalance 源于 attention(请求级/序列长度)还是 MoE(专家路由倾斜,需 EPLB 而非迁几条请求),`server_args.eplb_rebalance_*`。判定后才决定迁移是否是对的手段。
- **请求迁移归存算分离开放性**:请求迁移(在途换 DP/换节点)整体归存算分离的开放问题——KV 归池权威使迁移代价低,但控制态交接(RNG/FSM)、seed 状态重建、抖动控制待请求迁移特性立项时细化;现阶段结论是框架无需为此特别预留(drafter KV 已归池)。
