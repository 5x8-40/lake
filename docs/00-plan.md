# 00 — 路线图与执行计划

本文件是 lake 仓库的**主线计划**，列出依次要做的事情。每完成一个里程碑更新本文档状态。

> 总立地：探索并验证一套**彻底的存算分离推理系统**。所有有状态物（权重、KV cache、调度队列）从算力路径剥离，算力节点可随时销毁/拉起。

## 阶段总览

```
P0  特性设计         → docs/ 里把"要做什么"定义清楚
P1  架构设计         → 围绕特性设计把"怎么搭"定下来
P2  模块划分 + 技术选型 → 各模块语言/框架/接口边界
P3  最小可运行骨架   → 跨语言跑通一条请求（mock 模型）
P4  KV Pool 原型     → 内容寻址 + 前缀复用 + 分层缓存
P5  存算分离验证     → Prefill/Decode 物理隔离 + KV 迁移
P6  弹性与调度       → 无状态路由器 + 秒级扩缩容
P7  性能建模与验证   → 量化各假设，回填设计
```

---

## P0 — 特性设计（done 2026-07-15）

**目标**：把系统"要交付哪些能力"讲清楚，作为架构设计的输入。不谈实现。

产出文档（`docs/features/`）：
- [x] [`features.md`](features/features.md) 特性清单：按 Must / Should / Could 分级（每条含输入/输出/失败语义）
  - 设计前提：**三种执行模式**（PD 分离 / 混部 / D-direct），Router 按存储池本地命中、prompt 规模、传输成本逐请求选路；详见 features.md "执行模式"节
  - [x] F1 KV cache 池化与前缀复用（内容寻址、radix tree）
  - [x] F2 混合执行模式（PD 分离 / 混部 / D-direct，含模式选择）
  - [x] F3 分层缓存（HBM→DRAM→NVMe→对象存储，**四层全部由存储池统一管理**，层=介质非位置，计算节点不拥有本地内存）
  - [x] F4 故障恢复（基于 KV Pool 续推）
  - [x] F5 无状态路由
  - [x] F11 多模型存储池与生命周期管理（长期存续/模型无关/配额扩缩/GC/碎片整理）
  - [x] F6 投机解码（draft / target 分离）
  - [x] F7 秒级弹性扩缩容
  - [x] F8 多租户隔离与共享前缀（Could，远期预留——lake 不做,归外部;留 KVBlockID scope 维度预留）
  - [x] F9 模型版本/热更新（Could）
  - [x] F10 跨机房（Could）
- [x] [`slo.md`](features/slo.md) SLO 与衡量指标（TTFT / ITL P50/P99 / 吞吐 / 命中率 / 冷启动时延，初版 draft）
- [x] [`nonfunctional.md`](features/nonfunctional.md) 非功能需求（可观测性、安全、成本、部署、可维护性、可测试性）

**完成判据**：每条特性有明确的输入/输出/失败语义 ✅；SLO 数值化 ✅；与 [`goals.md`](features/goals.md) 对齐且无矛盾 ✅。
**P0 状态：done 2026-07-09**

---

## P1 — 架构设计（围绕特性设计）

**目标**：基于 P0 特性，定下数据流、组件边界、一致性模型、故障域。

**P1 状态：done 2026-07-15**（八篇架构文档补齐）。

产出文档（`docs/architecture/`）：
- [x] 更新 [`overview.md`](architecture/overview.md)：纳入混合执行模式与 KV 流转视角，替代刚性 P→D；去 ⚠️
- [x] [`architecture/execution-modes.md`](architecture/execution-modes.md) 以 KV 为中心的执行模式与 KV 流转时序（本地完成 / 跨节点传输含正向产出与反向回传）；失败处理统一归 F4 重路由，不设独立降级阶梯
- [x] [`architecture/data-flow.md`](architecture/data-flow.md) 请求生命周期详图（含 F4 故障分支、模式选择决策树 mermaid、三模式执行段、ready/done 双 fence 一步契约）
- [x] [`architecture/consistency.md`](architecture/consistency.md) 一致性与故障模型（控制面强一致/数据面最终一致、写一次读多次、ref 两级 + writeback ref 防悬空 radix、持久语义分层 L2 恢复点 / L3 SSOT、风险窗口、F4 续推、GC reconcile）
- [x] [`architecture/topology.md`](architecture/topology.md) 部署拓扑（单/跨机房、双网络 CNIC/SNIC、RDMA 三级退化与 GPUDirect 依赖、NUMA/NIC 多 NIC 聚合、故障域边界）

**完成判据**：任一特性的"数据从哪来、写到哪、谁来调度、失败怎么办"都可在此找到答案。

### P1 已定决策摘要（跨轮固化）

- **彻底存算分离**：L0–L3 全归存储池统一管理，计算节点不拥有任何内存；APC 概念删除，"本地命中"= 存储池放置决策的结果。
- **radix tree 归存储池**，按 `model_id` 分命名空间；Router 一次查询拿前缀复用 + 本地命中，守 5ms 模式选择预算。
- **放置与 batch 职责边界（方案 Z）**：存储池按热度主动预放置 KV 到 HBM + 发布位置视图；调度器读视图组 batch（本地命中优先→D-direct，缺失补拉），不反向指挥放置。单向耦合。
- **冷热与生命周期**：L0/L1 做副本（易失缓存）、L2/L3 间按移动、L3 永久权威；冷热按"引用数>0 冻结 + 热度分(LFU-Aging) + 前缀亲和"；迁移主动为主 + 被动兜底；迁移/GC/碎片整理共享后台带宽池（<10%）。
- **执行模式时序**（存储池视角不区分 P/D）：时序一本地完成（D-direct/混部共用，入口由本地命中定 prefill 工作量）；时序二跨节点传输——正向（产出→消费，服务本次）+ 反向（消费→池，D 延伸 KV 回传增强未来前缀，agent 多轮核心）。
- **decode 增量写回双重目的**：容错 + 前缀生长。频率 N 策略留开放。
- **HBM 池化下的入图与 KV 管理（Q1/Q2，本轮定）**：
  - **Q1 入图**：固定基址 KV arena（不上 VA，分配给模型后不扩缩容/不跨模型回收物理页）；入图三约束（静态输入 buffer / 固定 KV 基址 / 固定地址 block table）；decode 走 graph、prefill 走 eager；block table 由**本地 agent（in-process，持本地视图镜像）组装**，非全局池每步 RPC 推表（守 5ms）；**ready/done 双 fence 一步契约**（池发 ready→引擎 replay→引擎发 done→池解冻/写回/注册 radix/驱逐），引擎零分层逻辑；正确性地基 = in-flight 跨层冻结（ref>0 的 block step 期间物理映射冻结）。详见 [`architecture/compute-layer.md`](architecture/compute-layer.md) "HBM 池化下的入图与 KV 管理"。
  - **Q2 KV 管理**：block 对引擎**纯寻址单位**（连 block table 索引填充都归池，引擎只 replay 读，不感知满块）；写回两路——**满块路**（填满→池算哈希→写回 L2 durable→注册 radix，NVMe F4 恢复点）+ **尾块路**（请求结束时未满尾块写一次，纯容错不进 radix）；**ref 池权威维护**（多引擎共享前缀 block 的分布式一致性，引擎不持计数），请求结束且无续推引用才减（F4 续推 ref 转移），含在途传输引用（源端冻结）。
  - **跨实例/PD 传输**：engine-to-engine 控制链**切断**，池的本地 agent 发起传输，引擎降到 `publish`/`pull`+fence、不知地址、不组装 block table；数据线仍直连 RDMA（wire 效率不变）。默认**直传**（A→B L0，PD 时序重叠主场景）+ **Drain 推 L2**（节点下线前把还被远端引用的 block 落 L2 NVMe）。详见 [`architecture/kv-cache-pool.md`](architecture/kv-cache-pool.md) "跨实例 KV 传输"。
  - **重叠语义**：拒绝**引擎驱动** intra-step 重叠（SGLang `get_key_buffer` 每层 `wait_event`，绑死引擎、破坏 graph）；保留**池驱动**异步重叠——消费侧 step 间重叠 + 生产侧 prefill 层级重叠（`page_first_direct` 子块传输/"分块流水线"，支撑 PD 分离 TTFT）。引擎无感、graph 安全。
  - **持久语义**：层=介质，L0/L1 易失缓存、L2(NVMe)= F4 恢复点（NVMe 持久 + NPU 故障不烧 NVMe，恢复能力与位置无关，worker 崩溃后从 L2 续推）、L3(对象存储)= SSOT 永久权威（抗整机级/池级失败，L3 缺失才视为 block 不存在）。风险窗口分两级：NPU/进程级故障（常见）丢"最后一次写回 L2 之后的少量 token"；整机级故障（罕见）退 L3 SSOT，丢"自上次冷下沉 L3 之后的增量"。
  - **ref 分两级(B1 闭环)**：原"池单点强一致计数"修正为——**本地引用计数**(池本地 agent,请求级,同 vLLM `free_blocks`/sglang `inc_lock_ref` 机制,归池 agent 而非引擎)+ **全局引用汇总**(控制面,最终一致,供 tier up/down 与 GC,不进 hot step loop)。ref 归 0 ≠ 删内存,而是变可驱逐候选(对齐 vLLM `free_block_queue`/sglang `evictable_size_`,内存仍在、可命中复用/作传输源);归 0 不摘位置视图(未驱逐覆写则仍可命中/直传,D-direct 与 D→P 子情况 A 的命中来源);驱逐覆写才摘视图,L2/L3 有副本可回填。step 期间冻结是引用计数自然结果,无需额外 fence。L0/L1 是副本→驱逐 L0 不影响别节点(读各自副本),故全局 ref 只用于 tier/GC,不用于阻止 L0 副本驱逐。详见 [`architecture/kv-cache-pool.md`](architecture/kv-cache-pool.md) "引用计数与驱逐"。
  - **D→P 流 + 双网络路径 + DualPath 原生支持（本轮新增）**：agent 多轮里上一轮 decode 产出的延伸 KV 是下一轮 prefill 的输入前缀,不必绕一跳存储,可直接由 decode 侧喂回 prefill——这是与 P→D(本次)、D→池(未来)并列的**第三条方向 D→P(服务下一轮)**,即 DualPath(arXiv:2602.21548v2,非 submodule)storage-to-decode 路径的**原生支持**。**双网络隔离**(compute network 跑 L0→L0 RDMA + GPU collective / storage network 跑 L1/L2/L3 访问)是 DualPath 的架构前提,两类带宽是**池的资源**(池统一分配,非引擎"借用",比 DualPath 更彻底)。D→P 选路按"所需 KV 是否已在 D 的 L0"分子情况:**A 零存储读取**(KV 已在 D 的 HBM → D L0 经 compute network 直传 P,连 storage network 都不占,DualPath 不强调,我们独有) / **B**(需从 L1/L2 加载 → 池可选 D 侧加载+compute network 回传,借 D 闲置 storage 带宽绕开 P 侧瓶颈) / 传统 P 侧自拉;由池按 NIC 带宽视图决策,collective 突发窗避让留 P7。engine-to-engine 控制链仍切断(池 agent 发起,引擎不知对方存在)。详见 [`architecture/data-flow.md`](architecture/data-flow.md) §3.4、[`architecture/kv-cache-pool.md`](architecture/kv-cache-pool.md) "双网络路径"、[`research/dualpath.md`](research/dualpath.md)。
  - **多租户隔离(B2 闭环)**：**lake 不做多租户**(与 goals.md "不做多租户隔离"一致),F8 降级到 Could(远期预留)。当前 `KVBlockID=(model_id, layer, block_hash)` 不含租户维度,同 model_id 内 KV 全局共享复用;多租户隔离归外部控制面/部署切分(按 model_id 命名空间或独立集群)。未来若需,可加 `scope` 维度(public/tenant,靠 scope 过滤隔离、公共只可平台写)——仅预留,不入当前寻址。消了 goals(不做)↔ F8/nonfunctional(要做)的矛盾。详见 [`features/features.md`](features/features.md) F8、[`architecture/kv-cache-pool.md`](architecture/kv-cache-pool.md) "Block 寻址"预留。
  - **Router 命中视图访问(B3 闭环)**：Router 持**本地命中视图镜像**(全局位置视图的本地副本,与 in-process agent 同机制),模式选择 = 本地读镜像 + 本地纯函数决策,**零 RPC**,守 5ms。镜像内容是全局的(本地命中判定需知"哪个节点 HBM 有"),副本存本地。刷新由控制面**推送**(gRPC stream 主方案,同机走共享内存直读;etcd 只存降频 checkpoint,非推送源),触发=位置视图权威变更(放置/驱逐覆写/迁移/满块注册);ref 归 0 不推(未驱逐不摘视图)。陈旧由 miss→控制面确认→从池(L1/L2)回填兜底,只影响命中率不影响正确性。统一了 overview/scheduling/data-flow 里"Router 查池/一次 RPC"的措辞。详见 [`architecture/scheduling.md`](architecture/scheduling.md) §1 前缀解析。
  - **选路形态倾向 External 式(本轮固化)**：KV 感知下选路权威收束到 Router 一层(逻辑单一选路面,实例可水平扩展);计算节点按部署独立或 TP/PP 联合执行,**默认不在引擎内做 Internal/Hybrid 式二次 DP LB**。对齐 vLLM External + 强化 SGLang 层 B(`sgl-model-gateway`/`cache_aware`)方向,用存储池命中视图替换近似树;拒绝 SGLang 层 A(`DataParallelController`)与 vLLM head `DPLBAsyncMPClient` 作第二权威。详见 [`architecture/scheduling.md`](architecture/scheduling.md) §1.1、[`research/sglang/model-runner.md`](research/sglang/model-runner.md)「SGLang 双层管理模式」。
  - **计算引擎结构(本轮固化)**：Python 计算层对齐 vLLM Model Runner V2 目录形态 + 薄 `model_runner.py`;节点调度落 `runtime/node_scheduler.py`;`engine/` 取代 prefill/decode/draft 三包,执行形态由角色配置 + `SchedulerOutput` 选择。**DP step 信息同步(token 数/mode/IDLE)落 Scheduler**,不进 ModelRunner。**Host `Req` 权威完全在 `node_scheduler`**(引擎无长期 RequestState);**默认启用 overlap**;请求结束 → `agent.on_request_finished`(引擎不 free KV)。详见 [`architecture/compute-layer.md`](architecture/compute-layer.md)「计算引擎结构」、[`architecture/scheduling.md`](architecture/scheduling.md) §3。
- **技术选型已定**（P2 落地）：存储 + 存储控制面 Rust / 请求控制面 Go / 计算 Python+Triton；元数据 etcd；SSOT 用 S3/MinIO；跨语言 gRPC+Protobuf（大块 KV 走 RDMA 旁路）。3rdparty 五个 submodule（sglang/lmcache/mooncake/vllm/dynamo）作实现参考。
- **KV 类型 t-type / r-type + 投机解码机制（本轮新增）**：
  - **KV 类型**：按 HBM 存储形态分 t-type(逐 token 完整 KV,paged block,full attention/MLA)与 r-type(紧凑表示——窗口最近 W token / Mamba 定长 state,sliding window/Mamba/卷积)。**两类复用条件一致**:都需命中全部前缀才能复用;区别**仅在 HBM(L0)存储形态**,目的是降低 r-type 的 HBM 占用。HBM 两类并存、分 arena 管理(r-type 另设固定状态 arena 入图);L1–L3 统一按 block(128 token)组织(两类复用条件一致、不区分类型),r-type 落下层在 block 边界 checkpoint 紧凑状态(trailing pages / state 快照)——相对 SGLang multi-pool 物理分池,我们把类型差异收敛到 L0 存储形态 + block 内布局,而非物理分池。详见 [`architecture/storage-layer.md`](architecture/storage-layer.md) "KV 类型"节、[`architecture/kv-cache-pool.md`](architecture/kv-cache-pool.md) "t-type / r-type"。
  - **block 粒度 128 token**：缓存命中/复用/传输/写回最小单位(初版默认,待 P7 校准)。
  - **投机解码(仿 SGLang)**：drafter 与 decode(target)默认共置、同 step 串行。**pre/post 共用同一 drafter 模型,拆 `post_forward` / `pre_forward` 两阶段(同类的两个方法,非独立组件)**统一自回归类与 diffusion 类编排:**`post_forward`**(target 之后,吃 target 输出做强耦合部分)承载 MTP/EAGLE/EAGLE3 的 draft head 前向(参数与主模型一致)+ DFLASH/DSPARK 的 draft cache 准备;**`pre_forward`**(下轮 target 之前)承载 MTP/EAGLE/EAGLE3 的自回归多 token 生成 + DFLASH/DSPARK 的 diffusion 并行产 block。**prefill 阶段仍产 draft**(drafter forward 照跑),差异在产出是否使用:vLLM PD 分离下 P 侧 draft 弃用、forward 仅为保 drafter KV 同步(`llm_base_proposer.py:567`),SGLang 暂未细究——**记为遗留问题,初步判断不影响整体设计**(prefill 产出是否用属节点侧策略)。**decode 多层 MTP** 分 chain-style(每层用自己上步输出 hidden,需 FULL 暂存)/ non-chain(每层用 target hidden,只需 LAST)两范式,参考 SGLang `multi_layer_eagle_worker_v2.py::chain_mtp_hidden_states`;单层 MTP 是 non-chain 退化。**drafter 自己的 KV 与 target KV 同款——进存储池统一管理、跨请求前缀复用、随迁**(SGLang `PoolName.DRAFT`,纠正此前"draft L0-only 不进池"的误记);seed hidden(自回归)/ 窗口状态(diffusion)是否跨请求缓存待定,先按 SGLang 重算式(draft-extend 重建,不进 radix),记为遗留问题。MTP 重算产出 `1+num_mtp_layers` token,残差 prefill 短时左 pad。**主攻方案**:MTP/EAGLE/EAGLE3/DFLASH/DSPARK(后两者 diffusion 类,半年内进生产),不主攻 medusa/mlp_speculator/ngram/独立 draft 模型。**支持梳理**:MTP/EAGLE/EAGLE3/DFLASH 两边都有;**DSPARK 仅 SGLang**;不参考 vLLM `spec_target_max_model_len`(独立 draft 模型时代遗留)。详见 [`architecture/compute-layer.md`](architecture/compute-layer.md) "投机解码"节。
  - **缓存命中感知调度**：命中(Pool/本地)是模式选择与 batch 组成的一等输入;新增**跨请求前缀共调度**(同前缀请求组同 batch/节点,前缀 block 复用+本地命中叠加,参考 SGLang `match_prefix` cache-aware scheduling / vLLM `get_computed_blocks`);守方案 Z 单向耦合(只读视图、不指挥放置),draft 候选不进 radix。详见 [`architecture/scheduling.md`](architecture/scheduling.md) "缓存命中感知调度"节。
  - **长度边界规避(max_model_length vs runner_max_model_length)**：推理临近最大长度时的边界 bug(drafter 跳过致 EP/DP 集合通信盲等、block 申请不够致请求永不可调度)在 vLLM/sglang 均靠累积特殊逻辑兜(vLLM `reserve_full_isl`+spec pad break+PD+spec lookahead=0;sglang `init_req_max_new_tokens` admission clamp+`speculative_skip_dp_mlp_sync`+`_build_trivial_verify_input`)。lake 用双长度变量规避:对外 `max_model_length`(gateway/scheduler 守,length cap+SLO 契约)+ 计算层内部 `runner_max_model_length = max_model_length + headroom`(arena/block table/graph 预分配);headroom 吸收 draft transient,runner 不写近 max debug 逻辑。代价:额外 HBM arena。详见 [`architecture/compute-layer.md`](architecture/compute-layer.md) "长度边界规避"节。

### P1 待讨论 / 开放点

- decode 写回频率 N：多轮 agent（重前缀增强时效，N 小）vs 单轮（重带宽/容错，N 大），待 P7。
- 满块写回频率（满一个就写 vs 攒几个满块一起写），待 P7。
- 反向回传的 radix 增长时效：写回到 radix 可见的滞后上限。
- 分块流水线深度（`page_first_direct` 子块传输 k 与 prefill 层数对齐），待 P7。
- D→P 选路（§3.4 子情况 A/B 与 P 侧自拉的 NIC 带宽视图决策、collective 突发窗避让）待 P7。
- 模式选择决策树的具体阈值（本地命中判定、传输成本 vs 分离收益）待 P7；**决策树结构本身待 `data-flow.md` 落定**（结构定型、阈值留空标 P7）。
- **block 粒度 128 token** 与传输/写放大/碎片率的权衡,待 P7 校准。
- **r-type 状态 checkpoint**:Mamba/卷积 recurrent state 落 L1+ 的 checkpoint 间距/形式、sliding window trailing pages 阈值,待实现/P7 校准。
- **r-type SWA 尾段重算优化(idea,暂不实现,已记预留)**:SWA KV 不落 L1+、prefix 命中时重算匹配序列最后 `n*(w-1)+1` 个 token(position `[L+n-n*w-1, L)`)仅刷 SWA 窗口、不写非 SWA 模块(slot_mapping=-1),省存储换重算。暂不实现,但已留两处接口预留:① agent 的 slot 分配按模块差异化(只给 SWA 分 write slot,模块意识留池侧、引擎契约不破,经 Q2 张力权衡选此);② 残差路径区分"增量 prefill(未匹配尾)"与"刷新重算(已匹配尾,仅 SWA 写)"。r-type SWA 是否落 L1+(持久 vs 重算)二选一,待 P7。详见 [`architecture/compute-layer.md`](architecture/compute-layer.md) "r-type SWA 前缀复用的尾段重算优化"。
- **MTP 左 pad 策略**:是否总 pad 到固定宽度、pad token 是否复用命中 KV、pad 窗口上限,待实现/P7 校准。
- **drafter 共置 vs 独立 Draft 池**:默认共置(sglang 式);独立池的收益阈值(投机命中率 vs draft 候选传输延迟)待 P7。
- **r-type 入图**:sliding window / Mamba 固定状态 arena 与 t-type block arena 的 capture/replay 协同,待 P2/P3。
- **headroom 大小**:`runner_max_model_length − max_model_length`(覆盖 draft 深度+lookahead+block 对齐 margin+安全余量),待 P7 校准。
- **seed hidden 是否跨请求缓存(遗留)**:默认 SGLang 重算式(hidden 不进 radix、命中后 draft-extend 重建);备选按 token 存 hidden 进池换跨请求复用(省重算、费存储)。drafter KV 本身已定为进池复用。
- **DP 间在途再均衡(未来特性,框架预留分析)**:抢占重算式(仿 vLLM v1 `_preempt_request`),控制态硬核仅 RNG state + 结构化解码 FSM 游标,drafter KV 随池迁移、seed 由 `post_forward` 重建 → **框架无需特别预留**。多次迁移防抖/防饿死、imbalance 源(attn vs MoE/EPLB)、归存算分离整体细化 = 遗留问题。详见 [`architecture/scheduling.md`](architecture/scheduling.md) "DP 间在途再均衡"。

### P1 下一步（收尾，按此顺序）

1. ~~`architecture/data-flow.md`~~ ✅（done 2026-07-15）：请求生命周期详图 + 模式选择决策树 mermaid + 三模式执行段 + F4 分支；清掉 [`scheduling.md`](architecture/scheduling.md) ⚠️ 固定 P→D 残留（注解改为指向 data-flow）。
2. ~~`architecture/consistency.md`~~ ✅（done 2026-07-15）：一致性与故障模型。形式化持久语义（L2 F4 恢复点 / L3 SSOT）、ref 分两级（B1 闭环）、写回频率 N 的风险窗口、写一次读多次、控制面强一致/数据面最终一致、崩溃恢复点、GC reconcile。
3. ~~`architecture/topology.md`~~ ✅（done 2026-07-15）：部署拓扑（单/跨机房、双网络 CNIC/SNIC、RDMA 三级退化与 GPUDirect 依赖、NUMA/NIC 多 NIC 聚合、故障域边界）。承接本轮多处"留 topology.md"（GPUDirect RDMA 依赖 PCIe root、TCP 退化带宽-延迟模型）。

> **P1 状态：done 2026-07-15**。八篇架构文档（overview / storage-layer / compute-layer / kv-cache-pool / scheduling / data-flow / consistency / topology）补齐，满足完成判据（任一特性的"数据从哪来/写到哪/谁来调度/失败怎么办"可在此找到答案）。转 P2（proto 起草）。

---

## P2 — 模块划分与技术选型（done 2026-07-21，[#17](https://github.com/chengda-wu/lake/pull/17) 合入；设计定稿更早）

**目标**：把架构落到模块，定语言、框架、接口、目录结构。

### 技术选型（已定）

| 层 | 模块 | 语言 / 框架 | 理由 |
|----|------|-------------|------|
| 存储层 | KV Pool / Weight Cache / Tiered Storage | **Rust** | 内存安全、零成本抽象、RDMA/IO 性能、长期常驻进程稳定性 |
| 存储控制面 | 位置视图权威 / radix / 配额·GC / etcd checkpoint | **Rust**（`rust/controlplane`） | 权威在进程内存强一致,与存储层同语言;etcd 只存降频 checkpoint + lease |
| 请求控制面 | Router（含集群级调度,无独立 Scheduler 进程） | **Go**（`go/router`） | 并发原语成熟、生态利于写控制面服务、gRPC 生态 |
| 计算层 | Prefill / Decode / Draft 前向 | **Python + Triton** | Triton 写自定义 kernel、与 PyTorch/生态兼容、迭代快 |
| 对象存储 | SSOT | S3 / MinIO | 现成，不自研 |
| 控制面存储 | 元数据权威 | **控制面进程内存**（强一致）+ etcd（降频 checkpoint + lease） | 强一致权威在进程内存（满块注册高频写不压 etcd）；etcd 只存低频 checkpoint + 节点 lease，非强一致位置表。详见 [`architecture/control-plane.md`](architecture/control-plane.md)「位置视图权威的归属」 |
| 跨语言通信 | 统一 RPC | **gRPC + Protobuf** | Rust/Go/Python 都有一等支持；数据平面大块 KV 走 RDMA/共享内存旁路 gRPC |

### 模块与目录划分

> 与 [#3](https://github.com/chengda-wu/lake/issues/3) / [`control-plane.md`](architecture/control-plane.md) 对齐：**位置视图权威在 Rust 存储控制面**；**集群级调度归 Go Router 内**（不拆独立 Scheduler 进程）；节点级 scheduler 在计算节点（Python，后续）；入口 Gateway 用外部 Bifrost。空壳落地见 [PR #17](https://github.com/chengda-wu/lake/pull/17)。

```
lake/
├── docs/                       # 设计文档（语言无关）
├── rust/                       # 存储层（含存储控制面）
│   ├── proto/                  # tonic-build 在线生成（不入仓）
│   ├── controlplane/           # 位置视图权威进程（内存强一致 + etcd 降频 checkpoint）
│   ├── kv-pool/                # KV cache 分布式池（内容寻址、分片、驱逐）
│   ├── weight-cache/           # 权重分层缓存
│   ├── tiered-store/           # L0-L3 分层缓存引擎
│   ├── transfer/               # KV 传输（RDMA + TCP 退化）
│   └── storage-agent/          # 计算侧 / KV Node 双角色 agent（feature 分能力）
├── go/                         # 请求控制面（Router）
│   ├── pb/                     # protoc 生成入仓
│   ├── router/                 # 无状态路由 + 模式选择；集群级调度逻辑同进程
│   └── (无 gateway/ · 无独立 scheduler/——见 control-plane.md)
├── python/                     # 计算层（一套 engine；角色=配置，见 compute-layer.md「计算引擎结构」）
│   ├── lake_pb/                # grpcio-tools 生成入仓
│   ├── engine/                 # ModelRunner V2 形态（薄 runner + attn/sample/drafter/pool_iface…）
│   ├── runtime/                # Worker + node_scheduler + RPC；角色配置入口
│   ├── kernels/                # Triton kernel 集（attention/prefill/decode）
│   └── (prefill/·decode/·draft/ 空壳废止为实现树，由 engine+role 取代)
├── proto/                      # 共享 protobuf IDL
├── scripts/                    # gen_stubs.sh 等（Go/Python 重新生成）
└── deploy/                     # 部署（compose/k8s/镜像）
```

### 接口边界（P2 定稿）
- [x] `proto/schema.proto`：KVBlockID / Location / BlockMeta schema（#2，见 [PR #16](https://github.com/chengda-wu/lake/pull/16)）
- [x] `proto/lake.proto`：RPC 边界草稿——ControlPlaneService（边3/4/5）/ AgentService（边10）/ TransferService（边7/8），KV 字节走 RDMA 旁路、worker↔agent 走 FFI 不进 proto（边界草稿;三语言生成验证见 [PR #17](https://github.com/chengda-wu/lake/pull/17)）
- [x] 三语言空壳目录 + stub 编译（[PR #17](https://github.com/chengda-wu/lake/pull/17)：Rust workspace / Go router+pb / Python lake_pb+worker 包；`scripts/gen_stubs.sh`；工程基建 [PR #18](https://github.com/chengda-wu/lake/pull/18)：Cargo.lock 入仓 + 工具链钉版本 + CI 三语言 build/stub-drift/fmt/lint + storage-agent feature 门控）
- [ ] KV block 传输：gRPC 控制平面 + RDMA/共享内存数据平面，二进制布局规格（`TransferRequest` 控制信令已定，字节布局待传输引擎落地）

### 转 P2 切入建议

P1 关键篇（execution-modes + overview）已齐，够支撑 proto 起草。建议从 **`proto/lake.proto` 的 RPC 边界草稿**切入，把这几轮定的存储池接口固化：

- **Router ↔ 存储池**：热路径 Router 在本地位置视图镜像上 match（零 RPC，见 B3）；`LookupPrefix` RPC 作冷启动 / 镜像 gap / 调试时的权威回退（非热路径常态），输入 `(model_id, prompt 前缀)`，输出 `可复用 block 列表 + 各自位置（含本地命中判定）`。
- **调度器 ↔ 存储池**：读位置视图（组 batch 用）；补拉放置请求（缺失 KV 放到指定节点 HBM）。
- **Worker ↔ 存储池**：prefill 产出写回（含反向回传的延伸 KV）；decode 读 KV；增量写回（容错 + 前缀生长）。
- **元数据 schema**：KVBlockID = `(model_id, block_hash, pool_kind)`（block = page,128 token × 全部层,不含 layer_idx；寻址忽略 scope,F8 前默认 public）；block 的 `locations` 为多层位置集合（L0/L1 缓存副本 + L2/L3 稳态二选一），L3 缺失才视为不存在。详见 `proto/schema.proto`。

**完成判据**：三个语言仓各自空壳 crate/包可编译（Rust workspace / Go module / Python 包可 import）；proto 可双向生成；目录结构落地。

---

## P3 — 最小可运行骨架（done 2026-07-21）

**目标**：跨 Rust/Go/Python 跑通一条请求，模型用 mock（返回固定 token），验证三语言联通与 KV 流转链路。

### P3 骨架约定（与生产路径的差距，刻意缩小范围）

| 点 | P3 | 生产（P4+） |
|----|----|-------------|
| Gateway | **不做 Bifrost**；客户端直打 Router OpenAI HTTP（边2） | Bifrost → Router（远期） |
| KV 字节 | `SkeletonKvService` **gRPC 传 bytes**（入 Rust 内存池） | RDMA 旁路（边7/8），proto 仅控制信令 |
| worker↔agent | Router → `AgentService.Dispatch`（边10 **ack 占位**）→ `WorkerService.Generate`；worker 内调 ControlPlane+SkeletonKv | Dispatch → agent 组 batch → FFI(边6) |
| 执行模式 | 固定 **混部**（同进程 mock prefill+decode） | Router 三模式选路 |
| 前缀复用 | ControlPlane `LookupPrefix` + `RegisterBlocks`（进程内存） | 同协议；权威仍在 Rust 控制面内存 |

参考：早期单进程 `src/` 冒烟（两请求共享前缀）；Dynamo `DefaultWorkerSelector` / SGLang `match_prefix`（LookupPrefix）；vLLM `KVConnectorBase_V1`（worker↔池）；Mooncake PutEnd（RegisterBlocks）。

- [x] 客户端 → Go Router OpenAI `/v1/chat/completions`（**无 Gateway/Bifrost**）→ Dispatch → `WorkerService.Generate`
- [x] Python mock worker：LookupPrefix → SkeletonKv Get/Put → RegisterBlocks → mock decode
- [x] Rust `controlplane` + `kv-pool` + `storage-agent` 进程（内存权威 + 内存 bytes + Dispatch ack）
- [x] 端到端冒烟：两请求共享前缀，第二个 `reused_blocks>=3`（`./deploy/smoke.sh`，替代 `python -m src`）
- [x] `deploy/run-local.sh` 一条命令起全栈，curl 打通；`deploy/README.md` + CI `p3.yml`
- [x] ControlPlane `LookupPrefix` 单测（缺口截断）

**完成判据**：`deploy/` 一条命令起全栈，curl 打通；共享前缀第二次请求命中 KV。  
清单勾选以 [PR #19](https://github.com/chengda-wu/lake/pull/19) **合入 `main` 为准**（分支上预勾，未合入前勿当 main 已完成）。

---

## P4 — KV Pool 原型（Rust）（当前首要）

> 实现参考:`3rdparty/` 五个 submodule 逐层对应见 [`research/3rdparty-reference.md`](research/3rdparty-reference.md)。  
> **代码级复用（两模块正交）**：  
> - **A** Mooncake transfer-engine → `rust/transfer`（字节搬迁；P4 抄设计 + TCP，真 RDMA 推后）  
> - **B** Dynamo kvbm-logical → **P4.1** vendor（[PR #21](https://github.com/chengda-wu/lake/pull/21)）；**P4.2** 起 `lake-controlplane` 链依赖 + `BlockRegistry`/`InactiveIndex` 薄驱动（见 #20）  
> 切片命名用 **P4.1–P4.9**（勿与 GitHub PR 号混淆）；详见 #20 与「代码级复用策略」。

- [x] 内容寻址 block 存储 + 引用计数 + LFU-Aging / 前缀亲和驱逐（复用 B 起步；P4.2：`ReportRef` **合账骨架**——只累加 `global_refs`、忽略 `RefKind`，agent 本地一级/分 kind 后续；驱逐主路径 `LineageBackend::with_frequency`＝叶子约束≈前缀亲和 + TinyLFU 冷叶≈LFU-Aging；`MultiLruBackend` 仍 pub 对照；不 pub 纯 Lru/Fifo，见 `rust/vendor/UPSTREAM.md`。**驱逐正确性 = Authority 单测**（含 inactive 上界：满容 skip insert；压力 `allocate` 仅 `evict_n`）；生产 `ReportRef` 喂数与压力 `allocate` → 后续切片）
- [x] radix tree 前缀索引（前缀复用查询）（复用 B；P4.2：`RegisterBlocks.prefix_hashes` + `LookupPrefix`）
- [x] 分层缓存引擎（RAM/NVMe，对象存储回填）（P4.3：写回只 L2；L3=demote/cap XOR；L1=demotion；`TierPipeline`/`BandwidthPool`/PutEnd COMPLETE；真 NVMe/跨机 defer P5；满块顺便写 L1 留 P7。[PR #31](https://github.com/chengda-wu/lake/pull/31)）
- [ ] gRPC 接口 + RDMA 数据平面（先 TCP 后 RDMA）（传输面复用 A）
- [ ] 一致性哈希分片 + KV Node 扩缩时的 block 重分布
- [ ] **多模型生命周期**：模型注册/下线级联删、revision 失效（F11）
- [ ] **按模型配额与空间分配**（软/硬配额 + 借用 + 背压信号）
- [ ] **GC**：冷块/孤儿块回收 + 崩溃 reconcile（P4.3 review 遗留详见 [#20](https://github.com/chengda-wu/lake/issues/20) P4.7 + 评论「PR #31 review 遗留」：barrier 失败 inactive 窗口、WRITEBACK−1 失败永久冻结、`apply_location_events` 非原子等）
- [ ] **碎片整理**：逻辑共置 + 物理压实，后台节流可暂停

**完成判据**：前缀复用命中率、驱逐正确性有单测；吞吐 micro-benchmark；多模型隔离/配额/GC/碎片整理各有验证用例。

---

## P5 — 存算分离验证

> 计算层落地节奏见 [`architecture/compute-layer.md`](architecture/compute-layer.md)「计算层实现里程碑」C0–C5（vLLM V2 目录 + SGLang Req/overlap；KV 归池）。

- [x] **C0**：D1 `SchedulerOutput` 定稿 + `python/engine`·`runtime/node_scheduler` 骨架 + P3 Generate 挂新路径（mock）
- [x] **C1**：continuous batching + overlap 主循环 + FutureMap host 占位
- [x] **C2**：`pool_iface` FFI 草签（D2/D5）
- [x] **C3**：TinyLM + attn/sample 最小路径（零强制 torch；P3 默认仍 `model_backend=mock`）
- [x] **C4**：共置 TinyMTP（post/pre_forward）+ TARGET_VERIFY + chain reject
- [x] **C5**：vLLM 调度几何（`num_computed` + 本步 token）+ 三模式选路骨架（`PrefixHint`/`mode_select`）；整段本地命中→`computed=prompt_len`（无 PREBUILT 分相）；Go Router 权威联调后续
- [x] **C6–C10** 填充 scheduler/worker/runner（vLLM 为主）：详见 [`architecture/compute-layer.md`](architecture/compute-layer.md)「C6–C10 填充计划」
  - [x] **C6**：Worker 长期单环（一份 scheduler；Generate 入队；step 前 drain）
  - [x] **C7**：token_budget + chunked extend
  - [x] **C8**：InputBatch + AttentionMetadata（D4）+ TinyLM 批路径
  - [x] **C9**：D10 overlap×agent + D6 dummy_run
  - [x] **C10**：Warm/容量信号骨架（生命周期 + CapacitySignal；Router/真 pin 联调后置）
- [ ] 同步工程基建：CI 以 `engine`/`runtime`/`kernels` 为主（旧三包仅兼容 import）
- [ ] Prefill→Decode 的 KV 迁移流水线（计算与传输重叠）
- [ ] 故障注入：杀 Decode 节点 → 从 KV Pool 续推

**完成判据**：量化 KV 迁移带宽 vs 计算时间的比值，验证 P/Decode 物理分离可行区间。

---

## P6 — 弹性与调度（Go）

- [ ] 无状态 router + 本地命中视图镜像（权威在 Rust 存储控制面进程内存,etcd 只 checkpoint;Go Router 零 RPC 读镜像；读写分锁时：P4.3 `lookup_prefix` 因懒修复/`touch` 走写锁，懒修复应挪 register 路径——见 `authority.rs`）
- [ ] 池间调度 + 反压
- [ ] 基于指标的弹性扩缩容（队列长度/TTFT/ITL/命中率）
- [ ] 冷启动压缩（权重预加载、layer-async serve、KV prefetch）

**完成判据**：扩容决策到 Ready 接受请求 < 10s（目标值，待 P7 校准）。

---

## P7 — 性能建模与验证

- [ ] 成本模型：KV 传输带宽 vs prefill/decode 计算时间
- [ ] 分层缓存的命中率/成本曲线（P4.3 仅有 `HitStats` 计数骨架 + criterion micro-bench；`estimate_promote_cost` = `nbytes × hops`，hops∈{0..3} 对齐 L0/L1/L2/L3→L0 跳数，见 `LocalTierEngine::estimate_promote_cost`；真 workload 曲线与带宽校准本项）
- [ ] 弹性冷启动时延分解
- [ ] 回填到 `docs/` 与 SLO，修正非目标与设计假设

**完成判据**：每个 P0 假设有量化结论（成立/不成立/在何条件下成立）。

---

## 当前优先级

**现在做**：**P4 KV Pool 原型（Rust）**——内容寻址 block 存储 + radix 前缀索引 + 分层缓存引擎 + RDMA/TCP 数据面 + 一致性哈希分片 + 多模型生命周期/配额/GC/碎片整理。

P0–P3 已完成（特性 / 架构 / 三语言空壳+proto / 跨语言联通+前缀复用冒烟）。P3 入口直打 Router（无 Bifrost）。

## 状态约定

- 每个阶段用 `[ ]` 标未完成、`[x]` 标完成、`[~]` 标进行中。
- 阶段完成时在对应标题后加 `(done YYYY-MM-DD)`。
