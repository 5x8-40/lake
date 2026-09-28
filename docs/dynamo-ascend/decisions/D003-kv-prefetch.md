# D003: KV 预取——预取请求 + 读路径热缓存 / 显式副本复制

- 日期: 2026-09-28
- 状态: 提议（方案定型；落地前需完成文末实测清单）

## 背景

- 目标场景：vllm-ascend 0.26 + Mooncake Store 做 KV 卸载（`AscendStoreConnector`，`backend=mooncake`）。前缀 KV 已在池中（远端节点 DRAM 副本），真实请求到达前把 KV 预取到目标节点，消除首个请求的远端读延迟。
- 需求形态：SGLang [#27574](https://github.com/sgl-project/sglang/issues/27574)（Programmatic KV）式的控制面可编程预取——外部控制面决定"何时、把哪个前缀、放到哪个节点"，引擎只执行。参考 [`../../research/vllm_vs_sglang/agent-kv-cache.md`](../../research/vllm_vs_sglang/agent-kv-cache.md)、[`../../research/vllm/kv-session-roadmap.md`](../../research/vllm/kv-session-roadmap.md)。
- 形态约束：**预取请求 + 加载完就释放**。预取请求 = prompt 与目标前缀 token 级一致（block hash 才一致）、`max_tokens=1` 的合成请求，唯一目的是触发 connector 的池加载。不加新的带外传输通道，复用现有请求路径与池机制。

代码核实基线：vllm-ascend 源码 `~/vllm-ascend-0.26`；Mooncake submodule `3rdparty/mooncake` @ a2966b6a（2026-07-13）；上游 vLLM `3rdparty/vllm` @ 027b6f3a2（2026-09-28）。

## 两种模式的分野

`AscendStoreConnector` 两种用法，预取的目标介质不同：

| | 非 layerwise | layerwise |
|---|---|---|
| 配置 | `use_layerwise=false`（默认） | `use_layerwise=true` |
| 读路径 | 整对象 `batch_get_into_multi_buffers` 直读进 HBM block（`mooncake_backend.py:484` `MooncakeBackend.get`；`kv_transfer.py:1293`） | 每层计算前 range 读 `batch_get_into_multi_buffer_ranges` 进本地 block（`mooncake_backend.py:410` `batch_copy_get`；适配文档 §3.2） |
| 预取目标介质 | **HBM**（APC 前缀缓存） | **本机 DRAM**（池副本 / 客户端热缓存） |

layerwise 目标介质必须是本机 DRAM 的依据：

- chunked prefill 后续 chunk 会逐层重读已提交前缀（适配文档 §3.3；代码类名 `LayerwiseSessionTracker`，`session_tracker.py:24`，文档中写作 `MooncakeSessionTracker`，同一物）
- layerwise 模式即使本地 APC 命中也强制从池加载：`force_layerwise_load = self.use_layerwise and store_skip_tokens > 0`（`pool_scheduler.py:726`）
- 副本不在本机 → 每个 chunk 的每层都远端 RDMA；副本在本机 DRAM → 本地读

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
- 池命中即 `skip_save=True`（`metadata.py:1150-1151`），预取请求不回写池；对已存在的 key，Put 是静默 no-op（`client_service.cpp:1552-1556`，`OBJECT_ALREADY_EXISTS` 按成功返回）

## 方案 B：layerwise → 预取进本机 DRAM

### B1: 预取请求 + LocalHotCache（读路径自动回填，零代码）

Mooncake 客户端有本地热缓存（`LocalHotCache`，`local_hot_cache.h`）：读远端内存副本成功后异步填充本机 DRAM，后续读重定向到本机副本。

- 机制链（`Client::BatchGet`，`client_service.cpp:1339`）：
  - 读前 `RedirectToHotCache`（`client_service.cpp:1408`；定义 L1506——把副本地址改写为本机缓存块，要求整对象大小一致 L1518）
  - 读后 `ShouldAdmitToHotCache` → `ProcessSlicesAsync`（`client_service.cpp:1470-1471`）
  - 填充 = 提交时**同步** `std::memcpy` 进缓存块（`local_hot_cache.cpp:540-542`），异步部分只是 LRU 插入——无源缓冲区生命周期问题
- 准入：有 CountMinSketch 时按频率，`admission_threshold_ = 2`（`client_service.h:919`）——**默认要读 2 次才填充**；无 sketch 立即准入
- 副本已在本机内存时跳过填充（非 shm 模式，`client_service.cpp:4127-4129`）
- 启用全 env，vllm-ascend 零改动（`client_service.cpp:4009-4028`，部署指南 `mooncake-store-deployment-guide.md` L833-836）：
  - `MC_STORE_LOCAL_HOT_CACHE_SIZE=<字节>`（默认关）
  - `MC_STORE_LOCAL_HOT_ADMISSION_THRESHOLD=1`（覆盖默认 2）
  - 可选：`MC_STORE_LOCAL_HOT_BLOCK_SIZE`（默认 16MB）、`MC_STORE_LOCAL_HOT_CACHE_USE_SHM=1`（memfd 共享，供 dummy client IPC 零拷贝）
- 读路径副本选择本机优先（`real_client.cpp:292-296`，"local MEMORY — best case"）

预取动作：发预取请求（同 A）。1-token prefill 驱动全前缀逐层 range 读 → 热缓存填充本机 DRAM → 真实请求的 range 读被重定向到本机。

**未验证项（阻塞 B1，必须实测）**：

1. range 读 API `batch_get_into_multi_buffer_ranges` 来自 Mooncake PR #2881，本地 submodule（a2966b6a）没有该实现，其是否走 `Client::BatchGet` 的热缓存路径**无法本地核实**。若绕过 → B1 不成立，用 B2
2. 热缓存填充源是读的目的缓冲区，vllm-ascend 两条读路径的目的缓冲区都是 **HBM 地址**（`kv_transfer.py:176/206`）；`std::memcpy` 主机侧读设备内存在 Ascend 默认配置下不一定合法（热缓存的设计意图是"SSD 对象前的 DRAM 读缓存"，即目的缓冲区为主机内存的场景——部署指南 L829）。可能崩溃或填出脏数据

### B2: 显式副本复制 `create_copy_task`（全 Python API，无引擎参与）

不依赖热缓存，直接让池在目标节点 segment 建一个正式副本：

1. 独立预取进程（任意机器）建 store 客户端，调 `create_copy_task(key, [目标segment])`（Python 绑定 `store_py.cpp:2957`）
2. master 校验后把 REPLICA_COPY 任务派给源副本所在 segment 的属主客户端（`master_service.cpp:8115-8174`；源 segment 从现有副本中随机选，L8156-8159）
3. 源客户端后台线程每 1s 拉任务并执行（`client_service.cpp:3563` `TaskPollThreadMain` → `FetchTasks` L3541 → `ExecuteReplicaTransfer` L3167，要求源在本机内存 L3190）
4. 完成后目标节点读路径自动选本机副本（`SelectBestReplica` local-first，`real_client.cpp:292-296`）

要点：

- 目标 segment 名 = worker 的 `local_seg`（`hostname:rpc_port`，`mooncake_backend.py:263`；fabric-mem 路径为裸 hostname，L276）
- key 名单：按 key 格式 `model@block_hash@rank`（适配文档 §2）自行计算；`QueryByRegex` 无 Python 绑定，只能逐 key 算。放置结果可用 `batch_get_replica_desc` 核验（`store_py.cpp:2935`）
- 任务状态可查：`QueryTask`（`master_service.h:767`）
- 复制粒度是整个对象（全层），不是单层 range——对预取正好
- 产出是池正式副本，不受热缓存 LRU 影响，只受池级驱逐策略影响
- 源属主客户端必须在线（vllm-ascend 部署中即贡献了 segment 的 worker 进程内客户端）

### B 方案选型

- 先实测 B1 的两个未验证项；都过 → B1 最简（一个预取请求，纯 env）
- 任一不过 → B2。B2 同时是"预取后不依赖请求路径"的更稳形态，也是副本放置的显式控制面抓手

## 决策

- 预取统一定义为"把目标前缀的 KV 副本放到目标节点的目标介质"：非 layerwise = 方案 A（预取请求进 HBM/APC）；layerwise = B1 或 B2（本机 DRAM，待实测定）
- 不加新的带外传输通道；不改池的放置策略；不做 pin（HBM APC 与热缓存都是 LRU，预取-使用窗口归控制面把握）
- 代码改动总量：A 路径一个可选 patch（finish-at-promotion）；B1 零改动；B2 零改动（预取器用现成 Python API）

## 后果

落地前实测清单（按序）：

1. range 读是否走热缓存（需含 PR #2881 的 Mooncake 构建）
2. 热缓存填充对 HBM 源缓冲的 `memcpy` 安全性
3. A 路径端到端：预取请求 → 真实请求 APC 命中
4. B2 端到端：`create_copy_task` → 本机副本 → layerwise 读本地化

风险：

- 预取后无 pin：HBM APC 与热缓存均可被 LRU 驱逐，预取-使用间隔长时收益流失
- B2 的复制流量走源客户端后台任务，与正常读共享带宽；批量预取需控制面限速
