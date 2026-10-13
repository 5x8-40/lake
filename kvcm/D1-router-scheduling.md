# D1: Router 集成与请求调度（预复制的选路侧）

- 状态: **已闭环**——事实基线（F1-F15）已核实；六个设计问题全部闭环
- 上游问题: [backlog.md](backlog.md) D1——Router 如何感知预复制副本并据此选实例；「谁预取、何时放行」的进程形态归 D4
- 角色约定: kvcm 目前没有 router 组件。本文「选路方 / router」指未来消费引擎 kv event、决定请求发往哪个实例的组件（gateway 侧扩展 / 独立 router 进程 / 编排内置均有可能），进程形态归 D4

## 参考实现

kvcm 是独立原型，以下仅作参考，不构成约束。

| 参考 | 锚点 | 借什么 | 关键差异 |
|------|------|--------|----------|
| lake 选路面 | `../go/router/modeselect.go::SelectExecMode` + `prefixHint`；[`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「权威、镜像与本地 overlay」 | 反向陈旧不查（只损性能）、miss 回退权威的经验 | lake router 是池感知组件（持位置视图镜像）；kvcm router 走经典事件驱动，不碰池位置 |
| lake 命中定义 | [`../docs/features/features.md`](../docs/features/features.md)「Pool 命中 vs 本地命中」 | 术语：Pool 命中（省重算需传输）≠ 本地命中（省传输） | lake 本地命中指 L0/HBM 池放置；kvcm 的「本机」含 HBM 与本机 DRAM 两层，经 gpu/cpu 事件均可观测（§3） |
| lake 单向边界 | [`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「池放置·调度读视图」 | 调度读视图、不反向指挥放置的边界形态 | lake 的池有自主放置能力；Mooncake 池没有，放置智能只能在外置控制面 |
| llm-d Router | [`../docs/research/llm-d/overview.md`](../docs/research/llm-d/overview.md)「核心结论」；[`../docs/research/llm-d/architecture.md`](../docs/research/llm-d/architecture.md) §4 | 推测索引（TTL 2s）是派生索引系统补决策-确认窗口的手法 | llm-d 索引是事件流派生的副本视图（引擎才是真相）；master 直读没有该窗口 |
| Dynamo router | [`../docs/research/dynamo/overview.md`](../docs/research/dynamo/overview.md)「Router(KV-aware)」 | 成本模型（分层命中×权重 + decode 占用 + 在途请求）；分层计价 device 1.0 / host 0.75 / disk 0.25；在途负载无权威、router 本地记账 | Dynamo 缓存状态权威在 worker 本地索引，router 靠事件逼近（丢事件需 gap→快照重放）；直读 master 无此失效模式。外部重算 block hash 链 Dynamo 已为先例（keys.py 同款） |
| Mooncake 界限 | [`../docs/architecture/kv-cache-pool.md`](../docs/architecture/kv-cache-pool.md)「为何池必须自长 radix」 | Mooncake store 是 dumb blob 池（无内容寻址、无前缀匹配）——预复制控制面是外置的「懂前缀 + 懂位置」层 | 不照搬 lake 的池内 radix：keys.py 按 key 逐把算，不建树；前缀匹配退化为「连续满块命中长度」 |

## 事实基线（代码核实）

以下事实已核实，作为讨论输入。Mooncake 锚点 @ `1fc27b62`（submodule）；vllm-ascend 锚点 @ 0.26（rc1）；上游 vLLM 锚点 @ submodule `027b6f3a2`。编号按核实先后（F15 后补，主题属副本生命周期，列于「池与副本」），小节按主题归类。

**池与副本**

- **F1 mooncake_master 是放置权威**：key → 副本 → seg 由 mooncake_master 记录；读路径 get 时 `BatchQuery` 现查（`real_client.cpp` 8 处调用点，客户端无位置缓存）；没有其他位置通知通道（池 publisher 不带位置，F14）。
- **F2 一把 key = 一个满块在一个 rank 上的切片**：KV 按 block 切开（本模型 128 token 一块）；TP 下每个 rank 只持有块的一部分，同一块在不同 rank 内容不同——「满块 × rank」的每一片是池里一个独立对象 = 一把 key，尾部带 rank 编号（`head_or_tp_rank`）。例：801 token 前缀 = 6 个满块（768 token，余 33 不满一块不进池），TP=2 → 6 × 2 = **12 把 key**（详见 README「三个对象」）。
- **F3 读路径：先查后选，本机优先**：引擎读池分两步——先向 master 查副本列表（`BatchQuery`，F1），再开读会话搬数据。非 layerwise 读时 `SelectBestReplica` 本机内存副本优先（`replica_selection.h:122`）。layerwise 的读会话（get session）建立时从列表里选定一个副本（本机优先，`SelectCompleteMemoryReplica`，`real_client.cpp:470`），整个会话只从它读（读时校验锁定的必须恰好一个，否则报 `INVALID_REPLICA`，`:6470`）；会话本身带租约，建立后一个 TTL 窗口内有效（`:6464`）。推论：复制必须在会话建立前落位，本次才用得上；赶不上，选择器自动挑远端副本，跨机读，只影响当前这一次请求（[kv-prefetch.md](kv-prefetch.md) 时序节）。
- **F4 复制 API**：`create_copy_task(key, [seg])`（Python 绑定 `mooncake-integration/store/store_py.cpp:3362`）；master 只校验 key 存在 + 目标 seg 可分配（`master_service.cpp:13677 CreateCopyTask`）；整对象复制；源属主客户端必须在线；副本落位用 `batch_get_replica_desc` 核验（`store_py.cpp:3340`）。
- **F5 副本无 pin：READY 后随时可能被池驱逐**：复制路径不加 pin / lease（F15），副本落位后不受保护，池驱逐策略说收就收。属主实例（最初写入 key 的引擎）下线后，master 把它写入的 key 连同这些 key 在**所有机器**上的副本一起删除——**我们预热到 B 的副本也在其中，一起没**（实测：属主下线后 exist 0/32，机制与时机待确认 → E5；记录见 [EXPERIMENTS.md](EXPERIMENTS.md)）。注：mooncake main 有 soft pin 机制（挂 `ReplicateConfig`），属 D2 pin 候选（见 [backlog.md](backlog.md) D2），与本条「预复制副本默认无保护」不矛盾。
- **F6 引擎 APC 私有**：HBM 侧前缀缓存是引擎私有 LRU——无 pin、无池元数据、外部不可见（kv-prefetch.md 方案 A 注意节）。
- **F15 lease 是 key 粒度：读续租、复制不续租、删 key 释放全部副本**：
  - lease 挂在 key 的元数据上（`metadata.lease_`，group 内共享 `RegisterGroupMember:1669`），副本没有独立 TTL。
  - 读路径续租：`GrantReadLease(default_kv_lease_ttl_)`（`ExistKeyImpl:3299` / `GetReplicaList:4401` / `BatchGetReplicaList:4566`）；`CopyStart` 不触碰 lease（`:6444` 起）。
  - 删除与驱逐：TTL 到期 / 驱逐一律走 `EraseMetadata`——删 key 元数据并释放**全部**副本（`:2308-2313` 注释、`:2374-2375`）；驱逐候选必须 lease 已过期（`:8117` / `:8299` / `:12434`）。
  - 续租 API：`ExistKey` / `BatchExistKey` 顺带续租（`grant_lease=true`，`:3271` / `:3307`）；`ProbeKey` / `BatchProbeKey` 不续租（`:3276` / `:3312`）；默认 `default_kv_lease_ttl=10s`（`types.h:84`，部署以 master 配置为准）。
  - 推论一：lease 在 key 上，不存在「原副本 TTL 到、复制的副本没到」的状态；removed 是 key 全局事件，无需位置。
  - 推论二：复制不续租，READY 后一个 TTL 窗口内无流量即进入可驱逐状态——控制面需有界保活（周期 `batch_is_exist`，kvcm 现成调用；无限续租 = 变相 pin，违背 F5），保活策略归 D3。

**引擎 kv event**

- **F7 事件 schema 带介质字段**：`MEDIUM_GPU` / `MEDIUM_CPU`（上游 `vllm/distributed/kv_events.py:51-52`）；`BlockStored{block_hashes, parent_block_hash, token_ids, block_size, medium}`（`:56-68`）。medium 标记 KV 在哪种介质上——gpu = HBM 显存、cpu = DRAM 内存池；router 据此区分命中层级（§5 计价）。
- **F8 事件有两个发布源**：
  1. **引擎核（GPU/APC 块）**：上游 `BlockPool` 在块进 HBM 缓存时发 `BlockStored(medium="gpu")`、驱逐时发 `BlockRemoved(medium="gpu")`（上游 `vllm/v1/core/block_pool.py:362` / `:548` / `:621`）；vllm-ascend 的 coordinator patch 原样建 `BlockPool` 并透传门控（`patch_kv_cache_coordinator.py:150-154`）。
  2. **连接器（池 put）**：worker 侧在引擎自己 put 成功后生成 `BlockStored(medium="cpu")`（`kv_transfer.py:1162`、`pool_worker.py:3111`），跨 rank `KVEventAggregator` 聚合后经 `take_events()` 发出（`ascend_store_connector.py:213`）；门控 `enable_kv_cache_events`（`pool_scheduler.py:108`）。池副本的删除/驱逐不在这条引擎通道上发——那是 master publisher 的事（另一条通道，F14，0.3.11 不含）。
  - 范围：本部署只挂 AscendStoreConnector；`preempt_offload` connector 另有事件路径（`preempt_offload/manager.py:732`，cpu_block_pool 事件），不在本部署范围。
- **F9 盲区一：预复制在两条通道上都不会自动产生事件**：引擎事件只在引擎处理请求时产生（F8 两条路径都要过引擎代码），`create_copy_task` 完全在引擎之外 → 引擎通道**结构上**不会有预复制事件；master publisher（F14，0.3.12 起）是另一条通道，其复制路径同样零发布——所以预复制落位只能由 kvcm 补发（§2 方案 a）。
- **F10 盲区二：引擎事件通道上看不到池副本的删除**：vllm-ascend 全仓零 `BlockRemoved` → 池副本消失（驱逐 / TTL / 属主下线删除，F5）对引擎事件消费者不可见。注意三条通道别混淆：HBM 淘汰有 Removed（引擎通道，F8-1）；池删除有 removed（master publisher 通道，F14，0.3.11 不含）；引擎通道的池 put 只有 Stored 没有 Removed。
- **F14 池原生事件流（main 有，0.3.11 无）**：mooncake main 有 `KvEventPublisher`（`mooncake-store/include/kv_event/kv_event_publisher.h`），ZMQ 传输，RFC #1527 schema——**用途是池级缓存失效广播**：面向外部索引器报告「某把 key 存了 / 删了 / 清了」；对我们的价值只在 removed/cleared（删除面），stored 无 hash 内容且引擎自己发得更全。字段名兼容 vLLM（legacy `type:"BlockStored"` / `block_hashes` / `parent_block_hash`）但**内容全空/nil**（`kv_event_publisher.cpp:440-462`；config 注释「parent_hash and token_ids are not interpreted by the storage publisher」）——有效载荷 = `object_key`（PoolKey 字符串）+ `medium` + tenant/group + 固定上下文（`model_name` / `backend_id` / `block_size` / `dp_rank`），**不含 seg/机器位置**（事件只说哪把 key 怎么了，不说 key 在哪台机器上）；`backend_id` 标识池级流（一 master 一端点，非实例级）。stored 事件对 router 无可用 hash 内容。
  - 发布点（精确到宿主函数）：

    | 事件 | 发布点 |
    |------|--------|
    | stored | `PutEnd:5372`（引擎 put 提交，唯一） |
    | removed | `EraseMetadata:2338`（删除 / 属主下线）+ 驱逐四路径（`:8161`/`:8314`/`:11366`/`:12549`） |
    | cleared | `RemoveAll` |
    | 复制路径（`CopyStart:6444` / `CopyEnd:6666`） | **零发布——预复制副本落位不会自动产生池事件** |

  - 开关：编译 `ENABLE_KV_EVENTS`（CMake 默认 OFF，官方 wheel 构建显式 ON）+ 运行 `enable_kv_events`（`master_config.h:220`，默认 false）。
  - **版本口径：publisher 代码在 v0.3.11 之后才进仓（v0.3.11 tag 无 `kv_event/` 目录、CMake 与 wheel 构建均无此开关）→ 0.3.11.post1 不含此机制，要用须升级 mooncake**。vllm-ascend 不消费它。

**复制幂等性（重复发 copy 的后果）**

- **F11 重发安全，三层防护都在 CopyStart（`master_service.cpp:6444`），不依赖任何事件**：
  1. **目标 seg 已有副本 → 跳过**：按 seg 粒度去重——`GetReplicaBySegmentName(tgt) == nullptr` 才计数（`:6498`）、才分配（`:6595`）；全部目标已有副本 → 返回空 targets → 客户端 `replica_copy_skipped / target_replicas_already_exist`，按**成功** no-op（`client_service.cpp:4363`）。同 seg 不会产生第二份同 key 副本。
  2. **同 key 有在途任务 → 拒绝**：`HasReplicationTask()` → 第二个 `CopyStart` 失败 `OBJECT_HAS_REPLICATION_TASK`（`:6488`）——并发重发不会建两个任务；但这是**任务失败**（`query_task` 可见）不是静默成功，控制面重发时要把该错误码按「已在途」处理，不当失败。
  3. **部分重叠按 seg 粒度**：targets 中部分 seg 已有副本 → 只给缺失的 seg 建副本，非全有或全无。
- **F12 提交层无去重**：`CreateCopyTask`（`master_service.cpp:13677`）只校验 key 存在 + 目标 seg 可分配，不查目标已有副本、不查在途任务——两个 `create_copy_task` 都会拿到任务 ID；去重发生在源属主执行 `Copy→CopyStart` 时（F11），时序：先执行者赢，后者按 F11-2 失败。
- **F13 同机不同 seg 的重复是设计内**：防护是 per-(key, seg)；同机多实例各有 seg，同 key 在同机不同 seg 各一份是异构同机的正常工作形态（README 异构 TP 节）。key 不同的内容重复（不同模型/hash 命名空间）不在防护范围。

## 基线：无 kvcm 时怎么运作

```mermaid
flowchart TB
    Q["请求"] --> RT
    subgraph RT["Router（经典事件驱动）"]
        IDX["索引 block hash → 实例<br/>分层 gpu=HBM / cpu=池"]
    end
    RT -->|"① 前缀亲和：发给算过该前缀的实例"| EA
    subgraph A["实例 A"]
        EA["引擎 + APC（HBM 私有）"]
        SA["本机 DRAM seg"]
    end
    subgraph B["实例 B"]
        EB["引擎 + APC"]
        SB["本机 DRAM seg"]
    end
    EA -->|"② put 满块，钉本机 seg（preferred_segment）"| SA
    EA -.->|"③ BlockStored gpu/cpu；BlockRemoved 仅 gpu（F8）"| IDX
    EA -->|"local-first 本机读（F3）"| SA
    EB -->|"⑤ 副本在 A：跨机 RDMA 读"| SA
    EA -->|"④ 读：BatchQuery 现查（F1）"| M
    EB -->|"④ 读：BatchQuery 现查"| M
    SA <-->|"注册/心跳"| M
    SB <-->|"注册/心跳"| M
    M["mooncake_master<br/>key → 副本 → seg 权威"]
    M -.->|"stored/removed/cleared，只说 key 怎么了，不说在哪台机器（F14）"| PB
    PB["池事件 publisher<br/>0.3.11 不含，main 才有（F14）"]
    PB -.->|"ZMQ"| IDX
```

要点：

- **事件来源实例 = 物理位置**：写被 `preferred_segment` 钉在本机 seg → router 做前缀亲和即拿到本机读。这是「router 知道 KV 在哪台机器」的机制——不是事件带位置，而是 put 者即位置。
- **引擎是唯一写者** → 引擎事件的并集 ≈ 池内容；缺口是池侧删除面（引擎通道无 Removed，F10；池原生 publisher 有但默认关，F14）。
- **任何实例都能读任何前缀**：`BatchQuery` 现查 master，副本在哪都能读（跨机 RDMA）；位置只影响快慢，不影响正确性（F3）。

## 设计决策

### 1. 选路方需要哪些输入

决策：router 维护「block hash → 实例」索引，新请求按同一 hash 链算前缀 block hash、查最长匹配（Dynamo/llm-d 同构）。索引的事件来源两个，**kvcm 负责补齐引擎看不到的部分**：

1. **引擎原生事件**：引擎自己的动作——HBM 块的存入与淘汰（medium=gpu）、池 put 的存入（medium=cpu）（F7/F8）。
2. **kvcm 补发/翻译的事件**：预复制落位（放置，§2 方案 a）；升级 mooncake 后，池里的删除/驱逐由 kvcm 订阅 master publisher、翻译成引擎格式重发（删除，§2 方案 b）。

router 自始至终只消费引擎格式（`BlockStored` / `BlockRemoved` / `AllBlocksCleared`），不碰池 key、不读 master（§2 方案 c 排除理由）。事件由引擎发出，来源实例天然可知，不需要额外的 seg→实例映射表；kvcm 补发按目标实例端点发，归属同样天然（§2「方案 a 落地」）。

事实约束：引擎事件只覆盖引擎自己的动作（F8）——HBM 淘汰有 Removed，但池侧删除/驱逐/预复制都不产生引擎事件（F9/F10），这些正是 kvcm 要补的部分。事件里的 medium 字段标记 KV 在哪种介质上（gpu=HBM / cpu=DRAM 池），供 §5 分层计价。

留待 D4：事件开关与传输接线（`enable_kv_cache_events` 默认关，需打开才有事件，F8；ZMQ 端点配置、router 订阅方式）在部署形态下怎么接。

### 2. 可见性：router 的索引怎么知道预复制放置

预复制绕开引擎、不产生引擎事件（F9）→ router 的事件索引永远只知道「前缀在 A」，不知道拷完后「B 也有了」。这里有两个问题：**放置**（router 不知道 B 有了）与**删除**（副本没了，router 还当它存在）。

结论：**放置靠控制面补发事件（方案 a）；删除靠升级 mooncake 后由 kvcm 订阅池 publisher、翻译成引擎格式重发（方案 b）**。router 自始至终只消费引擎格式，不碰池格式。对账与 TTL 不做，直读 master 排除。

候选与裁决：

- **a. 控制面补发事件（选定，管放置）**：copy 完成后控制面按引擎格式补发 `BlockStored`，router 零改动。放置归属任何版本都只能靠它（池事件无位置、复制路径零发布，F14）。实现见下方「方案 a 落地」。
- **b. 池侧事件流（管删除，升级后由 kvcm 翻译接管）**：升级后打开 master publisher（F14），removed/cleared 正是删除面需要的（删除是 key 全局事件、全部副本一起删，无需位置，F15）。**消费方是 kvcm，不是 router**：kvcm 订阅 master 的 ZMQ 端点，把事件里的 PoolKey 解析出 block hash（keys.py 已知格式），按引擎格式重发 `BlockRemoved` / `AllBlocksCleared`；同一逻辑块的多个 rank key 由 kvcm 去重成一条。router 不碰池格式，不会变成池感知组件。stored 事件不取（无 hash 内容，且引擎自己发得更全，F14）。管不了放置：复制路径零发布 + 载荷无位置；「升级后让它顺便管放置」不成立——加发布点加位置 = fork mooncake（submodule 只读，fork 长期背维护），事件仍是池 schema，放置反被绑上升级节奏，而方案 a 在 0.3.11 即可跑。**0.3.11.post1 不含 publisher（E8 已核），要用须升级 mooncake 版本**——删除问题随之解决，不另做机制。
- **c. router 直读 master（排除）**：放置删除都能解决（现查现准），但要求 router 懂 PoolKey / seg / rank 映射，变成池感知组件，脱离经典事件驱动架构，且是全新增机制。
- **kvcm 轮询对账（不做）**：唯一价值在删除面（周期拉 master 快照 diff 能发现副本没了）；只为删除做一个对账器得不偿失，删除由升级解决。
- **router 条目 TTL（当前不做）**：纯删除兜底，与预复制放置无关（它不携带「B 有了」的任何信息）；router 侧独立机制，不需要任何新通道。升级前若删除面要兜底可再议。

**索引条目过时的后果**：删除面接通前，池副本被删（驱逐 / TTL / 属主下线，F5/F10）router 收不到通知 → 索引里 cpu 条目只增不减，会把请求导向其实已经没有副本的实例。后果只是变慢：请求打过去 get miss，引擎自动跨机读或重算（F3），不出错。HBM 条目有淘汰事件（F8-1），不过时。

**方案 a 落地（复用与身份）**

事件体每个字段都是 keys.py 的现成中间产物，发布类直接 import 上游，新增代码只有一个薄模块（拼事件 + 管理 publisher 实例）：

| 事件字段 | 来源 |
|----------|------|
| `block_hashes` | keys.py `_hash_prompt` 的 hash 链（`hash_block_tokens` 逐块结果，转 hex 前的原值） |
| `parent_block_hash` | 同上，链首块的 parent |
| `token_ids` | `_hash_prompt` 的 tokenizer 输出 |
| `block_size` | 参数 |
| `medium` | 与引擎池 put 事件对齐（F8；上游常量 `MEDIUM_CPU`，`kv_events.py:51`） |

- **发布组件**：直接 import 上游 `ZmqEventPublisher`（`kv_events.py:305`）——构造给 `endpoint`/`topic`，`publish(EventBatch)` 一次调用；wire 格式（topic + seq + msgpack 三帧）与 replay buffer 内置。kvcm 容器路径本就已 import vllm / vllm_ascend（keys.py），零新增依赖；沿用 keys.py/common.py 的 lazy import 形态，debug 路径不背 vllm。
- **身份（哪台机器）**：事件体无实例字段（仅 `data_parallel_rank`，`kv_events.py:39`），上游机制是**身份 = 端点**——router 按订阅端点把事件归入实例；一个实例多个端点有现成先例（DP rank 端口偏移 `offset_endpoint_port`，`kv_events.py:528`）。因此 kvcm **每个目标实例开一个发布端点**，由编排把「端点 → 实例」注册给 router（与引擎端点同一注册通道，归 D4/E13）；router 把补发端点并入该实例的端点集合，零新概念。ZMQ topic 是消息分拣标签（订阅端按它过滤消息），多个发布者可共用同一个 topic——它标不出「来自哪台机器」，所以身份只能走端点。
- **发布时机**：READY 核验通过（`precopy.py::assert_targets_have_replicas`）后，一次预复制发一条 `BlockStored`（block_hashes = 前缀连续满块链；引擎侧经 `KVEventAggregator` 跨 rank 聚合后同语义）。
- **幂等**：router 索引是集合语义，补发与引擎自发重复无害；重复预热重复补发亦无害（池层幂等 F11）。

### 3. 「哪个实例拥有前缀」怎么定义

问题：副本在 B 的 seg ≠ B 引擎 radix 里有该前缀——master 视图 / 引擎 kv event / 控制面 copy 记录三来源覆盖范围不同，§2 闭环后统一为索引声称一个口径。

定义：**实例 X 拥有前缀 P = P 的连续满块链的 KV 在 X 本机**——HBM（APC，gpu 事件）或本机池 seg（cpu 事件 / kvcm 补发）都算；命中长度 = 连续满块数 × block_size，不满一块的尾巴由引擎重算（801 token 命中前 768）。HBM 与本机 DRAM 的差异不在定义层区分，留 §5 打分。

成立条件（已核事实）：

- **router 口径即索引声称**：router 不读 master（§2），「拥有」= 索引中 P 的连续满块链每个 block hash 都有 X 的条目。条目可能陈旧（cpu 侧只增不减，F10），选错由读路径兜底（F3），只损性能。
- **rank 维度由事件语义吃掉**：引擎侧全 rank put 才发（`KVEventAggregator`，F8-2）；kvcm 侧全 rank READY 核验才补发（`assert_targets_have_replicas`）。一条事件 = 该实例全 rank 齐备，router 无需 per-rank 索引。MLA（`put_step>1`）key 折叠 rank 0，判定退化单 rank，未实测（E7）。
- **本机 = 事件归属实例**：cpu 条目的物理位置 = 发布端点归属的实例（引擎 put 钉本机 seg；kvcm 按目标实例端点补发，§2）。同机多实例各持 seg 的细分（本机他实例 seg 算不算「本机」）当前部署不出现，出现时按 seg→实例注册细化，归 D4/E13。

### 4. 时序：预热与选路的先后

问题：复制要秒级，请求来了等不等；复制什么时候动手。

结论：

- **请求不等 READY**：不做「hold 住请求等复制完成」——请求按本机有 / 池远端有 / 重算照常走（F3 兜底链），复制在后台追，本条赶不上不错只慢。测试床里「READY 后才放行请求」只是实验操作顺序（[kv-prefetch.md](kv-prefetch.md)「与请求调度联动」），生产不设这种等待。
- **复制三个时机并存**：
  1. **顺手补**：请求打到无本地副本的实例，本次跨机读，控制面观测后异步补复制，后续请求受益；
  2. **提前备**：多轮会话首轮选定实例后异步预热，第二轮起本地命中；提前量的最终来源是前端 agent/coder——它的钩子在请求发出前先把信号给控制面（信号形态与通道归 D4）；
  3. **挑现成的**：索引里已有的实例优先选——这是 router 消费事件后的本职（前缀亲和），不是第三种复制时机，也不是 kvcm 决策。
- **预热中到达照常下发**：引擎读池时先查副本列表选一个——本机没有就选远端，跨机读，慢一些不出错；选择只管当前这一次请求（F3），复制完成后，之后的请求自然用上本机副本。
- **intent 去重**：控制面以（前缀 × 目标实例）为幂等键，在途不重复发、完成不重复发；重发由池层幂等兜底（F11）。

职责边界：kvcm 管复制 / 保活 / 补发事件（让 KV 到位、告诉 router KV 在哪），不决策选路；router 管索引与选路，不直接下复制命令——它不懂池（容量 / 在途任务 / 副本现状），只向 kvcm 表达意图（intent：某前缀接下来在实例 B 上用，通道归 E12），复制与否、何时、去重由 kvcm 决策（它懂池）。两条单向通道：intent 上行（router → kvcm），事件下行（kvcm → router）。

### 5. 目标函数（归 router 内部实现）

口径：目标函数是 router 的内部问题，kvcm 侧无决策——§1-§3 闭环后 router 的输入已齐（引擎事件 + kvcm 补发 + medium 分层），怎么打分由 router 实现自定。候选方案记录在案：

- **分级兜底（最简）**：全本地 > 池远端 > 重算，字典序定档，档内按负载选。
- **打分制**：命中收益（连续本地满块数 × 分层计价；Dynamo 价目 device 1.0 / host 0.75 / disk 0.25 可借初值）− 负载惩罚（队列长 / in-flight；在途负载无权威，router 本地记账，参考实现·Dynamo）。
- **传输成本两档**：显式比较（跨机字节 / 有效带宽 vs 重算成本，不划算换实例重算）或简化（远端副本固定扣分）。

权重待真机校准（与 P7 耦合）；layerwise 下逐层远端更贵，远端计价按 D6 选型修正。

### 6. release 与放置的边界

分工：router 提供线索（会话结束 / 迁移 / 超时），控制面（kvcm）决策执行，池策略兜底。router 不直接下释放命令——与 §4 不直接下复制命令同一条边界。依据：router 知道「谁还会用」，池知道「谁还在」，释放决策需要两者。

前提事实：删除与 lease 都是 key 粒度（F15）——「只删 B 的副本、保留 A 的」不存在也不需要：B 的副本占 B 自己的内存，该让位时由池驱逐处理，不归释放决策。于是 release 只剩「整个前缀（所有副本）怎么处理」。

手段三个 + 兜底一个：

- **主路径·停保活（软释放）**：副本活着是因为有读在续租、或控制面在保活（F15）——释放不需要新动作，停止动作就行：控制面停掉某前缀的保活，租约到期池自然收走。保护机制：任何实例还在读这个前缀，读路径会自动续租，key 不受影响——停保活只是控制面不再续租，动不了仍在使用的数据。
- **中间档·demote（降层）**：不删除，DRAM → SSD，前缀还在池里可读（更慢）；适合「近期不用、但可能还会用」。操作集归 D2（`enable_ssd_offload` / `EvictDiskReplica`）。
- **例外通道·主动 Remove（硬释放）**：等不了自然过期、要立即腾地方时用；key 全局删除（A 的原本一起没），门槛 = 确认前缀谁都不再用（会话明确结束、前缀一次性）。`Remove` / `BatchRemove` 现成（D2）。
- **兜底·池驱逐（非手段）**：即使控制面出错（没停保活、没发 Remove），池的 TTL 与驱逐也会最终回收——不会出现没人记得、永远占着内存的孤儿副本。三个手段只让回收更快更准，正确性不靠它们。

已知缺口（登记，不归 D1）：master 无显式 pin API（lease 机制 → D2）；属主下线副本一并移除（F5 → D3/D5）。

## 留给后续

编号登记处（含 P/E 系列、状态与依赖）：[backlog.md](backlog.md)。

- **D2**：KV 调度操作集（pin/demote/prefetch/retain/release；master 无显式 pin → `SubmitReplicaActionProposal` lease 机制；释放有 `Remove`/`BatchRemove` 现成）
- **D3**：副本生命周期与 READY 语义（F5 属主下线移除、TTL、属主下线后的副本归属）
- **D4**：编排进程形态（预复制控制面独立进程或并入选路方；READY 确认接 gateway；seg→实例与「端点→实例」注册通道；预复制视图由控制面补发事件注入，§2）
- **D5**：故障衔接（源属主掉线 copy 失败（F4）、与重路由的先后）
- **D6**：layerwise 选型（[../docs/research/vllm-ascend/layerwise-taxonomy.md](../docs/research/vllm-ascend/layerwise-taxonomy.md)）
- **D7**：方案 A（壳请求进 HBM）去留与叠加顺序
