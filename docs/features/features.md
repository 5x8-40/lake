# 特性清单

P0 阶段产出。定义 lake 系统"要交付哪些能力"，作为架构设计（P1）的输入。
分级：**Must**（必须有，否则不成立，含 F1–F5、F11）/ **Should**（应当有，优先排期）/ **Could**（可探索，非阻塞）。

每条特性给出：**输入 → 输出 → 失败语义**。

---

## 执行模式（设计前提）

系统**不假定请求固定走 P→D**。Router 按请求特征在三种执行模式间逐请求选择，三者共存、可同集群混用：

| 模式 | 含义 | 适用场景 | KV 流转 |
|------|------|----------|---------|
| **PD 分离** (disaggregated) | Prefill 在 P 池、Decode 在 D 池，KV 跨节点传输 | prefill 大、传输值得、P/D 池各有负载空间 | P 产出 → KV Pool → prefetch 到 D |
| **混部** (co-located) | Prefill + Decode 同节点完成，无跨节点 KV 传输 | prompt 短 / 传输成本 > 分离收益 / 提升利用率 | 本地完成，异步写回 Pool |
| **D-direct** (本地命中直跳) | 前缀 KV 已被存储池**放置**在某执行节点 HBM，直接在该节点跑残差 prefill + decode | 本地命中率高（公共前缀、重复请求） | 零/极小传输，本地命中 |

### Pool 命中 vs 本地命中
- **Pool 命中**：前缀 KV 在分布式 KV Pool → 省重算，但仍需传输到执行节点。
- **本地命中**：前缀 KV 已被存储池放置在某执行节点 HBM（L0 副本，存储池元数据可见）→ 可 D-direct，零/极小传输。

→ D-direct 要求本地命中，而非仅 Pool 命中。

> 彻底存算分离下不存在计算层私有本地缓存：L0–L2 全部归存储池统一管理，"本地命中"是存储池放置决策的结果，所有 KV 位置均为存储池权威元数据。原 APC（D 节点私有缓存）概念已删除，由存储池统一的 HBM 放置管理取代。详见 [`../architecture/storage-layer.md`](../architecture/storage-layer.md)。

### 模式选择（Router 决策，命中维度阈值 v1 由 P7.2 成本模型给出）

下表两个维度**正交**（命中维度 = 传 vs 算的成本比较；利用率/专池维度 = 集群拓扑与负载信号），决策取交集：任一维度判混部则混部，两者都指向 PD 分离才分离。

| 条件 | 选择 |
|------|------|
| 本地命中 ≥ 1 block（128 token；前缀已在某执行节点 HBM） | D-direct |
| 池命中 ≥ 1 block 且路径有效带宽 ≥ 1 GB/s | PD 分离（传 KV 划算） |
| 命中 < 1 block 或带宽 < 1 GB/s | 混部（重算划算） |
| prefill 大、P/D 池有空闲（利用率/专池维度） | PD 分离 |
| P 或 D 池满载（利用率/专池维度判混部） | 混部 |

> 命中维度阈值来源：[`../architecture/cost-model.md`](../architecture/cost-model.md)——分界带宽 ≈0.57 GB/s，block=128 量化后「≥1 块即传」在 ≥1 GB/s 下恒成立；sub-GB/s 细分待真机校准。利用率/专池维度由集群拓扑与负载信号决定，不在成本模型内。

> **Router 当前接线口径**（P7.6，`go/router/modeselect.go::SelectExecMode`）：上表是成本模型给出的**目标语义**；原型代码目前只接了**带宽闸**（池命中且带宽 < 1 GB/s → 混部）与 D-direct 本地命中闸，PD 分离仍由节点角色（专用 P/D 池）驱动——hybrid 角色下「池命中 + 高带宽 → PD」尚未接进选路，待 P5 真字节路径后随带宽视图一并落地。

> **注**：混部与 D-direct 不违背"存算分离"——KV 仍归存储池权威、写回 Pool（保故障恢复与跨请求复用），只是计算放置在单节点。分离的是**存储与计算**，不是 P 与 D 必须分机。

---

## Must

### F1 — KV cache 池化与前缀复用
把 KV cache 作为全局、内容寻址的分布式资源，跨请求/跨节点复用公共前缀。

- **输入**：`(model_id, prompt_tokens)`
- **输出**：可复用的 KV block 列表 + 需增量计算的 block 列表
- **失败语义**：前缀查不到 → 退化为完整 prefill（功能正确，仅损失性能）；KV Pool 不可达 → 丧失前缀复用、退化为完整 prefill，持续不可达影响执行时触发 F4 重新路由（不自行拒绝已准入请求，见 F4）
- **与 D-direct 的关系**：前缀复用同时支撑 D-direct——存储池把高频前缀 KV 放置到执行节点 HBM（本地命中）时，请求可跳过 prefill 直入 decode（见 F2）。Pool 命中省重算，本地命中额外省传输。

### F2 — 混合执行模式（PD 分离 / 混部 / D-direct）
系统支持三种执行模式（见上"执行模式"），Router 按请求特征逐请求选择，三种模式可同集群并存。

- **输入**：请求 + 存储池命中视图（Pool 命中 + 本地命中，均为存储池权威元数据）+ 各池负载
- **输出**：执行模式 + 目标节点（P 节点 / D 节点 / 混部节点）
- **失败语义**：执行失败（节点故障/超时）→ 触发故障恢复（F4）重新路由，Router 依最新集群状态重新选模选点（非预设的 mode-to-mode 降级阶梯）；资源不足 → Router 按优先级排队（不丢请求），过载层面的限并发/限流/按优先级丢弃归 gateway（见 [`slo.md`](slo.md) "过载控制"节）

### F3 — 分层缓存
权重与 KV 经历 HBM → DRAM → NVMe → 对象存储 四级分层（层=介质非位置），逐级回填。四层全部由存储池统一管理（放置/驱逐/副本/冷热/生命周期），计算节点不拥有本地内存。L0/L1 做缓存副本（易失）、L2/L3 间按移动、L3 永久权威；L2(NVMe)= F4 恢复点、L3 = SSOT；冷热按引用数+热度分(LFU-Aging)+前缀亲和判定；迁移主动为主+被动兜底。详见 [`../architecture/storage-layer.md`](../architecture/storage-layer.md) "冷热与生命周期管理"节。

- **输入**：block/权重读取请求
- **输出**：数据 + 命中的层级
- **失败语义**：某层缺失 → 逐级下查；对象存储（SSOT）不可达 → 该 block 视为不存在

### F4 — 故障恢复（基于 KV Pool 续推）
算力节点崩溃后，未完成请求从 KV Pool 中最近的 KV checkpoint 续推。

- **输入**：故障节点的在途请求 ID
- **输出**：请求被路由到新节点并从断点续推
- **失败语义**：断点 KV 也已丢失（未被写回 Pool）→ 从 prompt 重算，仅丢失最后增量窗口的 token

### F5 — 无状态路由
Router 无本地状态，所有决策依据控制面共享视图，可水平扩展。

- **输入**：请求 + 控制面负载视图 + 存储池命中视图（Pool 命中 + 本地命中，均权威）
- **输出**：执行模式 + Prefill/Decode/混部 节点分配
- **失败语义**：视图陈旧 → 次优路由（仅损失性能），可重试；Router 实例崩溃 → 其他实例接管（无状态丢失）。命中视图由存储池权威维护，陈旧度仅受读缓存时效影响。

### F11 — 多模型存储池与生命周期管理（Must）
存储池为长期存续、模型无关的独立基础设施：同一池同时承载多个 `(model_id, revision)` 的 KV/权重，可对接任意模型；模型上下线与池生命周期解耦。

- **输入**：模型注册/注销、各模型配额、负载与命中率
- **输出**：按模型分配的存储空间、池容量伸缩、回收/压实后的可用空间
- **能力**：
  - 多模型寻址：block ID 含 `model_id`，池不解释张量布局，按不透明字节存取（接入新模型只需注册命名空间）
  - 按模型空间分配与配额（软/硬配额 + 空闲借用），配额权重可按负载动态调整
  - 池容量扩缩容（加/减 KV Node，一致性哈希最小迁移）
  - GC：引用0且冷的块、孤儿块（崩溃残留）、模型下线级联删除、旧 revision 失效
  - 碎片整理：逻辑（热点序列 block 共置降读扇出）+ 物理（NVMe 页压实），后台节流、低峰重叠
- **失败语义**：
  - 配额耗尽 → 软配额按模型 LRU 淘汰冷块；触硬配额 → 写入背压信号上传（请求级 shedding 仍归 gateway，不在池内拒请求）
  - GC/碎片整理与读写竞争 → 后台低优先级、可暂停、不阻塞数据面
  - 模型下线 → 级联删其所有 block，进行中请求由 F4 处理完再清理
  - Pool 重启 → 从对象存储（SSOT）恢复持久副本，不丢数据

---

## Should

### F6 — 投机解码（draft / target 分离）
小模型（draft）生成候选 token，大模型（target）并行验证，降低 decode 延迟。

- **输入**：当前 decode 上下文
- **输出**：经验证的 token 序列
- **失败语义**：draft 不可用 → 退化为标准 decode；验证全部拒绝 → 等价一次标准 decode（无正确性损失）
- **执行模型**：drafter 默认与 decode(target)共置、同 step 串行;pre/post 共用同一 drafter 模型,拆 **`post_forward`**(target 之后,吃 target 输出做强耦合部分:自回归 draft head 前向 / diffusion cache 准备)+ **`pre_forward`**(下轮 target 之前,产 draft token:自回归多 token / diffusion 并行 block)两阶段(同类的两个方法);**prefill 仍产 draft**(vLLM PD 分离下 P 侧产出弃用、forward 仅保 drafter KV 同步——记为遗留问题,不影响整体设计);decode 多层 MTP 分 chain / non-chain 两范式(参考 SGLang `multi_layer_eagle`);**drafter 自己的 KV 与 target KV 同款进池、跨请求复用、随迁**(SGLang `PoolName.DRAFT`),seed hidden 是否跨请求缓存待定(先按 SGLang 重算式 draft-extend,记遗留);MTP 重算产出 `1 + num_mtp_layers` 个 token,残差 prefill 短时需左 pad。**主攻方案**:MTP / EAGLE / EAGLE3 / DFLASH / DSPARK(后两者 diffusion 类,半年内进生产);不主攻 medusa/ngram/独立 draft 模型。DSPARK 仅 SGLang 有参考。详见 [`../architecture/compute-layer.md`](../architecture/compute-layer.md) "投机解码"节。

### F7 — 秒级弹性扩缩容
算力池按指标秒级伸缩，缩容不丢 KV、扩容冷启动可控。

- **输入**：负载指标（队列长度/TTFT/ITL/命中率）
- **输出**：节点加入/离开池
- **失败语义**：扩容资源不足 → 上报指标由 gateway 决定准入/限流（不在推理系统内拒请求）；缩容遇在途请求 → Drain 后再销毁

---

## Could

### F8 — 多租户隔离与共享前缀（Could，远期可探索，当前不实现）

> **范围裁定**：多租户隔离与鉴权、计费同属外部控制面/前端 serving 职责，**lake 当前不实现**（与 [`goals.md`](goals.md) "不做完整前端 serving 框架：不实现……多租户隔离等"一致）。lake 的池按 `(model_id)` 命名空间承载 KV，**同一 model_id 内 KV 全局共享复用**，不做租户间私有隔离——前缀复用是池的核心收益，跨"租户"共享正是其能力。租户隔离若需要，由部署方在外部按 model_id/部署实例切分（一个租户一套 model_id 命名空间，或多套独立 lake 集群）。

- **预留（不实现）**：`KVBlockID` 未来若需多租户，可加 `scope`/`tenant` 维度（public / tenant:<t>，靠 scope 过滤隔离、公共只可平台写），见 [`../architecture/kv-cache-pool.md`](../architecture/kv-cache-pool.md) "Block 寻址"预留说明。当前寻址不含该维度。
- **失败语义**：当前不适用（lake 不做隔离，无"污染拒绝"语义；隔离由外部命名空间切分保证）。

### F9 — 模型版本 / 热更新
新模型 revision 上线时不中断在途请求，旧 revision KV 按失效策略淘汰。

- **输入**：新 revision artifact
- **输出**：流量逐步切到新 revision
- **失败语义**：新 revision 加载失败 → 保持旧 revision 服务

### F10 — 跨机房部署
跨机房 KV Pool 副本与故障切换（远期，单机房验证后再考虑）。

- **输入**：机房亲和性偏好
- **输出**：跨机房路由决策
- **失败语义**：机房隔离 → 单机房自治服务

---

## 特性与目标的对齐

参见 [`goals.md`](goals.md)。Must 特性直接对应"彻底存算分离"的核心目标；其中 F2 把执行路径从刚性 P→D 打开为"分离/混部/D-direct"三模式，使系统能按存储池本地命中、prompt 规模、传输成本与利用率动态选路。Should/Could 围绕性能、弹性、运维延展。SLO 约束见 [`slo.md`](slo.md)，横切质量要求见 [`nonfunctional.md`](nonfunctional.md)。
