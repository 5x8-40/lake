# D003: KV 预取——预取请求 + 显式副本复制

- 日期: 2026-09-28
- 状态: 提议（方案定型；落地前需完成文末实测清单）

## 背景

- 目标场景：vllm-ascend 0.26 + Mooncake Store 做 KV 卸载（`AscendStoreConnector`，`backend=mooncake`）。前缀 KV 已在池中（远端节点 DRAM 副本），真实请求到达前把 KV 预取到目标节点，消除首个请求的远端读延迟。
- 需求形态：SGLang [#27574](https://github.com/sgl-project/sglang/issues/27574)（Programmatic KV）式的控制面可编程预取——外部控制面决定"何时、把哪个前缀、放到哪个节点"，引擎只执行。参考 [`../../research/vllm_vs_sglang/agent-kv-cache.md`](../../research/vllm_vs_sglang/agent-kv-cache.md)、[`../../research/vllm/kv-session-roadmap.md`](../../research/vllm/kv-session-roadmap.md)。
- 形态约束：**预取请求 + 加载完就释放**。预取请求 = prompt 与目标前缀 token 级一致（block hash 才一致）、`max_tokens=1` 的合成请求，唯一目的是触发 connector 的池加载。不加新的带外传输通道，复用现有请求路径与池机制。

代码核实基线：vllm-ascend 源码 `~/vllm-ascend-0.26`；Mooncake submodule `3rdparty/mooncake` @ **1fc27b62**（2026-09-28，含 range 读 API 的 PR [#2881](https://github.com/kvcache-ai/Mooncake/pull/2881) `78726c5c`）；上游 vLLM `3rdparty/vllm` @ 027b6f3a2（2026-09-28）。

**为什么调度行为引用上游 vLLM 代码**：vllm-ascend 是插件，默认调度器就是上游 vLLM 的 `Scheduler`/`AsyncScheduler`——vllm-ascend 自带的调度器子类（ShortRequestFirst / Recompute / DyntraLB / ProfilingChunk / BatchJobAware）全部继承上游且均为条件启用（`platform.py:1051/1122/1359/1365/1377`），默认配置一个都不开。因此 `WAITING_FOR_REMOTE_KVS` 状态机等调度行为，引用上游 `scheduler.py` 就是引用 vllm-ascend 0.26 实际运行的代码；行号以上述快照为准。

## 两种模式的分野

`AscendStoreConnector` 两种用法，预取的目标介质不同：

| | 非 layerwise | layerwise |
|---|---|---|
| 配置 | `use_layerwise=false`（默认） | `use_layerwise=true` |
| 读路径 | 整对象 `batch_get_into_multi_buffers` 直读进 HBM block（`mooncake_backend.py:484` `MooncakeBackend.get`；`kv_transfer.py:1293`） | 每层计算前 range 读 `batch_get_into_multi_buffer_ranges` 进本地 block（`mooncake_backend.py:410` `batch_copy_get`；适配文档 §3.2） |
| 预取目标介质 | **HBM**（APC 前缀缓存） | **本机 DRAM**（池副本） |

layerwise 目标介质必须是本机 DRAM 的依据——分两种场景，机制完全不同：

- **同机写读**（chunked prefill 主场景）：靠**写路径 local-first**。`_build_replicate_config` 带 `prefer_alloc_in_same_node`（默认 true）与 `preferred_segment=local_seg`（需 mooncake.json 显式配 `preferred_segment:true`，默认 false）（`mooncake_backend.py:346-351`）；layerwise PutStart 同样传入（`mooncake_backend.py:376-383`）。chunk 1 写完落本机 segment → chunk 2+ 重读命中本机副本（副本选择 local-first，`replica_selection.h:122` `SelectBestReplica`，L149-151 本机内存副本优先）。不爆炸
- **跨机**（副本在别的节点：他机写入的会话复用、P≠D、故障迁移）：**默认代码没有任何机制把读到的 KV 留在本机**——
  - 读路径只把数据读进 HBM，不建本机 DRAM 副本；`BatchGetWhenPreferSameNode` 名字有"prefer"，实际只是按源 segment 聚合批量传输，不建副本（`client_service.cpp:1493`）
  - `skip_save`（`metadata.py:1150-1151`）保证加载过的前缀不回写本机
  - chunked prefill 后续 chunk 逐层重读已提交前缀（适配文档 §3.3；代码类名 `LayerwiseSessionTracker`，`session_tracker.py:24`，文档中写作 `MooncakeSessionTracker`，同一物）；layerwise 模式即使本地 APC 命中也强制从池加载（`force_layerwise_load`，`pool_scheduler.py:726`）
  - → 每个 chunk 的每层都远端 RDMA，性能爆炸。**跨机场景的本机落地就是本方案要补的空白**
  - 注意区分两个无关机制：master 侧 promotion-on-hit（`--promotion_on_hit`，默认 false，`master_config.h:202`）只把 **SSD-only** 对象在读命中后晋升回 DRAM（`master_service.cpp:395`），不管远端 DRAM→本机 DRAM；客户端 LocalHotCache 只挂在整对象读路径上，layerwise 的 range 读用不到（见 B1 节证伪）

## 方案 A：非 layerwise → 预取进 HBM

机制链（全部已核实，上游 = `3rdparty/vllm`）：

1. 发预取请求。调度器 `get_num_new_matched_tokens` 查池命中（`pool_scheduler.py:624`）；全命中时少记 1 个 token——末 token 必重算（`pool_scheduler.py:703-704`）
2. `load_async=true`（`kv_connector_extra_config`，**默认 false**，`pool_scheduler.py:107`；返回值 `self.load_async and not self.use_layerwise`，`pool_scheduler.py:747`）时请求进 `WAITING_FOR_REMOTE_KVS`（`scheduler.py:1267`）：
   - 传输期**不占** `max_num_seqs` 槽位——槽位只数 running + streaming-paused（`scheduler.py:877-879`）
   - 传输期**零计算**——等待中的请求被直接跳过调度（`scheduler.py:890-898`）
3. worker 异步把整对象直读进 HBM block（`kv_transfer.py:1293`；目标地址 = KV cache 基址 + block 偏移，`kv_transfer.py:176/206`）
4. 传输完成 → `_update_waiting_for_remote_kv` → `cache_blocks` 入 APC（`scheduler.py:3032/3064`）→ 请求转 WAITING（`scheduler.py:3091`）
5. 释放：前缀 KV 已在 HBM 且入 APC，预取请求在此结束

释放方式两选：

- **零代码**：`max_tokens=1` 自然结束。代价 = 1 token prefill（末 token 重算）+ 1 token decode + 短暂占一个 running 槽
- **patch（finish-at-promotion）**：`_try_promote_blocked_waiting_request` 提升成功即结束请求。零计算零槽位，但要改调度器

注意：

- `load_async=false`（默认）走同步加载：请求正常占 running 槽，worker 首次 forward 前同步读完。预取仍成立，只是传输期占槽
- 预取进 APC 后是标准前缀缓存：LRU、无 pin、压力下可驱逐 → 预取与使用的时间窗要短
- 池命中即 `skip_save=True`（`metadata.py:1150-1151`），预取请求不回写池；对已存在的 key，Put 是静默 no-op（`client_service.cpp:1958-1961`，`OBJECT_ALREADY_EXISTS` 按成功返回）

## 方案 B：layerwise → 预取进本机 DRAM

### B1: LocalHotCache 读路径自动回填——**已证伪（layerwise 不适用）**

Mooncake 客户端有本地热缓存（`LocalHotCache`，`local_hot_cache.h`）：读远端内存副本成功后异步填充本机 DRAM，后续读重定向到本机副本。但它**只挂在整对象读路径上**，layerwise 的 range 读完全绕过：

- 热缓存只集成在 `Client::Get` / `Client::BatchGet` / `Client::BatchGetWhenPreferSameNode`（`client_service.cpp:1377/1419`、`1527/1607`、`1702/1860` 的 `RedirectToHotCache` / `ShouldAdmitToHotCache`）
- layerwise range 读走独立路径：`batch_get_into_multi_buffer_ranges`（`real_client.cpp:6975`）→ `execute_session_memory_range_reads`（`real_client.cpp:6545`）→ `Client::BatchTransferReadRanges`（`client_service.cpp:4669`）——纯 scatter 传输（`ScatterRangeBuilder` + `CompleteScatterRanges`），**无任何热缓存钩子**
- → 开热缓存对 layerwise 模式没有任何效果，B1 排除

对非 layerwise 模式热缓存理论上可用（整对象读有集成），但仍有未排除的风险：填充 = 提交时**同步** `std::memcpy`，源是读的目的缓冲区（`local_hot_cache.cpp:541-542`），而 vllm-ascend 整对象读的目的缓冲区是 **HBM 地址**（`kv_transfer.py:176/206`）——主机 memcpy 设备内存在 Ascend 默认配置下不一定合法（热缓存设计意图是"SSD 对象前的 DRAM 读缓存"，即目的缓冲区为主机内存的场景，部署指南 `mooncake-store-deployment-guide.md` L1365）。启用参数（全 env：`MC_STORE_LOCAL_HOT_CACHE_SIZE` 默认关、`MC_STORE_LOCAL_HOT_ADMISSION_THRESHOLD` 默认 2 需读两次才填充（`client_service.h:1116`）、`MC_STORE_LOCAL_HOT_BLOCK_SIZE` 默认 16MB、`MC_STORE_LOCAL_HOT_CACHE_USE_SHM`；`client_service.cpp:5376` `InitLocalHotCache`，部署指南 L1369 起）。本方案不依赖它。

### B2: 显式副本复制 `create_copy_task`（全 Python API，无引擎参与）——**选定**

不依赖热缓存，直接让池在目标节点 segment 建一个正式副本：

1. 独立预取进程（任意机器）建 store 客户端，调 `create_copy_task(key, [目标segment])`（Python 绑定 `store_py.cpp:3362`）
2. master 校验后把 REPLICA_COPY 任务派给源副本所在 segment 的属主客户端（`master_service.cpp:13677` `CreateCopyTask`；源 segment 从现有副本中随机选）
3. 源客户端后台线程每 1s 拉任务并执行（`client_service.cpp:4983` `TaskPollThreadMain` → `FetchTasks` L4961 → `ExecuteReplicaTransfer` L4273，要求源在本机内存 L4297）
4. 完成后目标节点读路径自动选本机副本：
   - 非 layerwise 整对象读：`SelectBestReplica` local-first（`replica_selection.h:122`，L149-151）
   - **layerwise range 读（已核实）**：副本选择在 get session 建立时——`batch_get_session_start`（`real_client.cpp:6361`）→ `SelectSessionReplica`（`real_client.cpp:490`）→ `SelectCompleteMemoryReplica`（`real_client.cpp:470-488`，本机内存副本优先）；session 锁定单副本（`real_client.cpp:6470-6474`，多副本直接报错）→ 之后每层 range 读都用这个本机副本

要点：

- 目标 segment 名 = worker 的 `local_seg`（`hostname:rpc_port`，`mooncake_backend.py:263`；fabric-mem 路径为裸 hostname，L276）
- key 名单：按 key 格式 `model@block_hash@rank`（适配文档 §2）自行计算；`QueryByRegex` 无 Python 绑定，只能逐 key 算。放置结果可用 `batch_get_replica_desc` 核验（`store_py.cpp:3340`）
- 任务状态可查：`QueryTask`（`master_service.h:915`）
- 复制粒度是整个对象（全层），不是单层 range——对预取正好
- 产出是池正式副本，不受热缓存 LRU 影响，只受池级驱逐策略影响
- 源属主客户端必须在线（vllm-ascend 部署中即贡献了 segment 的 worker 进程内客户端）
- 时序约束：`create_copy_task` 必须在目标节点的 get session 建立**之前**完成，否则 session 已锁定远端副本——预取-使用窗口仍归控制面把握

## 决策

- 预取统一定义为"把目标前缀的 KV 副本放到目标节点的目标介质"：非 layerwise = 方案 A（预取请求进 HBM/APC）；layerwise = 方案 B2（`create_copy_task` 建本机 DRAM 副本）
- B1（LocalHotCache）已证伪：range 读绕过热缓存（证据链见 B1 节）
- 不加新的带外传输通道；不改池的放置策略；不做 pin（HBM APC 是 LRU；池副本受池级驱逐策略管理）
- 代码改动总量：A 路径一个可选 patch（finish-at-promotion）；B2 零改动（预取器用现成 Python API）

## 后果

落地前实测清单（按序）：

1. A 路径端到端：预取请求 → 真实请求 APC 命中
2. B2 端到端：`create_copy_task` → 本机副本 → layerwise get session 选中本机副本（用 `batch_get_replica_desc` + 读路径日志核验）

风险：

- 预取后无 pin：HBM APC 可被 LRU 驱逐；池副本受池级驱逐影响——预取-使用间隔长时收益流失
- B2 的复制流量走源客户端后台任务，与正常读共享带宽；批量预取需控制面限速
