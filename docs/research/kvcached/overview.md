# kvcached — GPU 虚拟内存化的弹性 KV Cache

> 调研快照 2026-09-08，submodule `60cad94`（2026-08-22，v0.1.5）。源码 `3rdparty/kvcached/`。
> 上游 [ovg-project/kvcached](https://github.com/ovg-project/kvcached)（Apache-2.0），官网 [kvcached.org](https://kvcached.org/)。
> 论文：Prism（OSDI 2026，[arXiv 2505.04021](https://arxiv.org/abs/2505.04021)，多 LLM 服务 + GPU memory ballooning，kvcached 是其开源底座）、GPU OS 愿景（[arXiv 2508.08448](https://arxiv.org/abs/2508.08448)）。
> 本文含对「四大核心特性」的逐条源码核实（结论：**3 条成立、1 条名不副实**），以及与 Dynamo 生态和 lake 的对照。

## 定位

一句话：把 OS 的虚拟内存抽象搬到 GPU KV cache——虚拟地址（VA）与物理页解耦，物理页按需 map/unmap，同一张卡上的多个 vLLM/SGLang 实例因此可以弹性共享显存。支持 MHA/GQA/MLA/滑窗/hybrid 注意力，TP 与 PP（2026-03 起），SGLang ≥0.4.9 / vLLM ≥0.8.4。

**边界（不是什么）**：

- **不是 KV 分层系统**：不管 DRAM/SSD/对象存储，不做跨介质卸载（那是 HiCache/LMCache/KVCR 的层）；
- **不是前缀索引**：不维护 radix/位置视图，前缀复用仍靠引擎自己的 APC（2026-04 起与弹性共存，`KVCACHED_MAX_CACHED_TOKENS` 给 APC 配内存上界）；
- **不是独立 daemon**：名字叫 "KV cache daemon"，实现是嵌在引擎进程里的库 + 带外 CLI（见特性 2 核实）；
- **不碰权重**：只虚拟化 KV cache 张量；权重的 sleep/wake 依赖引擎自身能力（vLLM sleep / SGLang release）。

## 四特性核实

| # | 声称 | 结论 | 一句话 |
|---|------|------|--------|
| 1 | 跨进程显存超卖 | ✅ 成立 | 各实例 VA 私有、物理页按需 map/unmap，闲置物理页经 CUDA driver 流转给其他实例 |
| 2 | 全局显存统筹（独立 Daemon） | ❌ 名不副实 | 无显存 daemon；per-process 库 + `/dev/shm` 记账 + driver 仲裁，防 OOM 是去中心化的 |
| 3 | 零物理分配极速冷启动 | ✅ 成立（范围：仅 KV） | 启动只 reserve VA + 全段映射到同一 zero page（COW），物理占用 ≈ 1 页 |
| 4 | 多租户显存硬配额 | ✅ 成立（语义需精确化） | kvctl 写 shm → 引擎 100ms 轮询 resize；「硬」在分配拒绝，不能强收 in-use 页 |

### 特性 1：跨进程显存超卖 ✅

**机制**：每个引擎进程启动时 `cuMemAddressReserve` 一段**进程私有**的 VA 空间（`csrc/ftensor.cpp::alloc_virtual_mem`），KV block 分配到 2MB 页（`KVCACHED_PAGE_SIZE_MB`），页被使用时才 `cuMemCreate`+`cuMemMap` 物理页（`csrc/page.cpp::GPUPage`），页全空时 `cuMemUnmap`+`cuMemRelease` 把物理页还给驱动（`csrc/page_allocator.cpp::PageAllocator::free_page`）。

**超卖的两层**：

1. **VA 超卖**：vLLM 集成把调度器可见的 KV 容量固定为 `total_memory × gpu_memory_utilization`，**不因其他进程已占物理显存而折减**（`kvcached/integration/vllm/patches.py::_get_virtual_kv_capacity_bytes`，注释原文 "do not reduce it based on physical memory consumed by peers"）。同卡每个实例都可以按 90% 显存规划自己的虚拟 KV 空间。
2. **物理页流转**：实例 A 空闲时其 KV 页被 unmap/release 回驱动全局空闲池；实例 B map 前自查 `cudaMemGetInfo`（`PageAllocator::get_avail_physical_pages`，扣除 `KVCACHED_GPU_UTILIZATION=0.95` 的 headroom）后拿到这些物理页。

**工程细节**：prealloc 后台线程维持 min 5 / max 10 页（`KVCACHED_MIN/MAX_RESERVED_PAGES`）的已映射热页缓存，alloc 快速路径为微秒级；释放先回热页缓存、超出才真 unmap。开启 APC 时被前缀缓存引用的页保持映射（`KVCacheManager::get_page_occupancy` 按页统计存活块），所以「闲置回收」的实际力度受 APC 上界配置影响。

**实证**：官网 benchmark 为 3×Llama-3.1-8B 同卡 A100-80G 间歇峰值负载，TTFT 降 2–28×（`benchmarks/bench_latency_benefit`）。

### 特性 2：全局显存统筹（独立 Daemon）❌ 名不副实

"kvcached = KV cache daemon" 是 GPU OS 愿景的命名，**实现上没有独立显存管理 daemon 进程**。实际架构是去中心化的四件套：

| 件 | 是什么 |
|----|--------|
| 引擎进程内 C++ 库 | `PageAllocator`/`FTensorAllocator` 直接长在引擎进程里，自己决定 map/unmap |
| `/dev/shm` 记账段 | 每进程组一段（`kvcached_engine_<pgid>` 或 `KVCACHED_IPC_NAME`），flock+mmap 读写 `{total, used, prealloc}` 三字段（`csrc/inc/mem_info_tracker.hpp::MemInfoTracker`/`RwLockedShm`） |
| kvctl 带外 CLI | 直接扫 `/dev/shm` 段做 list/limit/watch/kvtop（`kvcached/cli/kvctl.py`），不与任何 daemon 通信 |
| CUDA driver | 物理显存的最终仲裁者：谁先 `cuMemCreate` 谁拿到页 |

**「统一防 OOM」的真实形态**：每个进程在 map 前**各自**检查全卡空闲显存（留 5% headroom），加上 map 失败回滚（`alloc_page` 慢路径异常时把页退回 free list）。没有全局分配互斥——两个进程同时看到空闲、同时 map 的 TOCTOU 竞态存在，靠 headroom 和重试缓解，不是集中调度消灭。

`controller/` 目录是**示例级**多模型前端：OpenAI 兼容路由 + 流量监控 + sleep 管理（tmux 拉进程，`controller/README.md` 自述 "complete example"）。它管「请求路由到哪个模型、闲了睡」，**不管显存分配**。

> 对「全局统筹」有刚需的部署，kvcached 给出的答案是「shm 记账 + driver 仲裁 + 人工/外部 kvctl 配额」，不是「一个权威调度者」。这正是 lake 存储控制面（强一致位置视图 + 池级配额）要超越的点。

### 特性 3：零物理分配极速冷启动 ✅（范围：仅 KV）

**机制**：`FTensor` 构造时 `cuMemAddressReserve` 整段 VA，随后 `init_with_zero_()` 把**全部 VA 页映射到同一个物理 zero page**（2MB，contiguous 布局为 compound page）——启动时物理占用 ≈ 1 页（`csrc/ftensor.cpp::FTensor::FTensor`/`init_with_zero_`）。首次使用某页时 `FTensor::map` 先 unmap zero-page 映射、再 `cuMemCreate` 新物理页映射进去——教科书式的 COW（copy-on-write）语义。

**精确化**：

- 消除的是 **KV cache 的物理预分配和静态容量选择**（不必再用 profiling 实测空闲显存反推 KV 大小）；**权重加载、CUDA graph capture 仍在**，端到端冷启动不是全免。
- 因为 VA 稳定、tensor 地址不变，物理页 remap 对 kernel/CUDA graph 透明——这是 VMM 路线成立的关键性质。
- Serverless 秒级拉起的完整故事是 kvcached（KV 零占用）+ 引擎 sleep/wake（权重 offload/恢复）+ controller 流量触发唤醒三者组合（`examples/06_serverless_serving`）；`benchmarks/bench_idle_footprint` 量的是闲置足迹。

### 特性 4：多租户显存硬配额 ✅（语义需精确化）

**链路**：`kvctl limit <ipc> <size>` / `limit-percent` → 写该实例 `/dev/shm` 段的 `total_size` 字段（`kvcached/cli/utils.py::update_kv_cache_limit`）→ 引擎内 `resize_watcher` 线程 100ms 轮询发现目标变化（`PageAllocator::resize_watcher` → `MemInfoTracker::check_and_get_resize_target`）→ `PageAllocator::resize()` 收缩/扩张可用页数。TP 组内 map/unmap 经 IPC 广播到各 worker（`kvcached/tp_ipc_util.py`）。

**「硬」的精确语义**：

- shrink 只能回收 **free/reserved** 页；若 in-use 高于新限额，`resize` 返回 false、记为 `deferred`，等引擎自己释放后补齐——**不能强制回收 in-use 页**（`page_allocator.cpp::PageAllocator::resize`，`kvcached/control.py::set_instance_memory_limit` 的状态机：`applied/deferred/stale/conflict`，revision 防并发写冲突）。
- 真正的「硬」体现在**分配拒绝**：可用页收缩后，超限分配直接失败（"No free pages left"），引擎层表现为 KV 满、触发驱逐或排队——一个长文本请求吃不光整卡，因为该实例的虚拟空间已被缩到配额内。
- 配额上限 = 启动时定的虚拟容量（`min(limit, total_capacity)`）；粒度是 per 引擎进程组（一个 shm 名），多池（hybrid 注意力 group_id）按容量比例拆分。

## 架构

```
┌─ kvctl / kvtop (带外 CLI) ── 读写 ──▶ /dev/shm/kvcached_engine_<pgid> (flock+mmap)
│                                              ▲ 100ms 轮询(resize_watcher)
├─ controller/(示例前端: router + sleep_manager + traffic_monitor, tmux 编排)
│
└─ 引擎进程(vLLM/SGLang, autopatch 注入)
     └─ KVCacheManager(py): block→page 映射、alloc/free、APC 共存、占用统计
        └─ PageAllocator(C++): 页生命周期(free/reserved/in-use)、prealloc 线程、resize
           └─ FTensorAllocator/FTensor(C++): VA 预留、zero page、页级 map/unmap
              └─ gpu_vmm: cuMemAddressReserve/Create/Map/Unmap/Release (+HIP 移植层)
```

- **接入方式**：`ENABLE_KVCACHED=true` + `KVCACHED_AUTOPATCH=1`，`.pth` 文件在 Python 启动时注册 `when_imported` 钩子，引擎模块 import 时打补丁（`kvcached/autopatch.py`）。vLLM 侧补丁面：`patch_kvcache_manager`/`patch_initialize_kv_cache`/`patch_allocation_methods`/`patch_reshape_methods` 等（`integration/vllm/patches.py`，约 1900 行）；SGLang 侧：`patch_profile_available_bytes`/`patch_radix_cache_limit` 等。另有 `engine_integration/patches/` 的静态 patch 文件（老版本）。**引擎零代码改动是卖点，但补丁面跟随引擎版本演进是维护成本**（README 标注 tested up to vLLM v0.24.0 / SGLang v0.5.15）。
- **布局**：默认 per-layer KV 张量（K/V 各一）；`KVCACHED_CONTIGUOUS_LAYOUT=true` 时全层单张量 + compound page；MLA 单 buffer（`num_kv_buffers=1`）。
- **TP/PP**：TP 内各 worker 进程经共享内存/Unix socket 广播 map/unmap offset（`tp_ipc_util.py`），C++ 侧回调避免持锁进 Python GIL（`page_allocator.cpp` 注释，issue #371）；PP 2026-03 起支持。

## 与 Dynamo 生态的关系：能不能在 Dynamo 基础上做东西

这是本次调研的核心问题。结论：**kvcached 与 Dynamo/KVCR 正交互补，但组合有一个真实的硬问题（VMM × RDMA 注册）**。

### 正交性：KVCR 不碰的，恰好是 kvcached 唯一做的

KVBM 被 sunset 的教训之一是 **GPU 紧耦合**——外部组件直接管理 GPU 侧 KV block，与引擎 kernel 调度抢资源（[`../kvcr/overview.md`](../kvcr/overview.md)「与 KVBM 的区别」）。KVCR 因此把边界划在「引擎拥有 GPU、给指针，KVCR 经 NIXL 代办搬运」，只管 DRAM/SSD/对象存储。

kvcached 恰好反过来：**它只碰 GPU 页管理，且不管 KV 语义**（布局、前缀、传输都不管，全留给引擎）。一个在 G1 的页层之下（VMM 映射），一个在 G1 之外（L2+ 卸载与跨节点 P2P）。组合形态：kvcached 让同卡多引擎的 HBM 弹性化（G1 层），KVCR/Dynamo 做跨介质与跨节点（G2+），Dynamo Router 做全局路由——`controller/` 的示例前端正是 Dynamo KV Router 的极简对照物。

### 硬问题：VMM 弹性 × RDMA 注册

KVCR/NIXL 做 GPU 直传需要注册显存（MR）；而 VMM 页 unmap/remap 会使已注册区域失效，或被迫 pin 住物理页从而失去弹性。kvcached 自己的 PD 支持现状印证了难度：vLLM 15 个 KV connector 里只有 `NixlConnector` 在验证（要求 `KVCACHED_CONTIGUOUS_LAYOUT=false`，配 `nixl_compat.py` 补丁），Mooncake/LMCache/FlexKV 等均未测（`docs/PD_DISAGGREGATION.md`）。**要在 Dynamo 上做「弹性 G1 + P2P 传输」，必须先解这个共存问题**——可选路线：按页注册/注销回调、传输期间 pin、或整段 VA 预注册 + 物理页池化（牺牲部分弹性换注册稳定）。

### 可做的方向（按可行性排序）

1. **多模型/serverless 单机层**：Dynamo 做集群编排，kvcached 做单机 GPU 超卖——Prism（OSDI 2026）已验证两级调度（kvcached 弹性 + 上层调度），Red Hat Sardeenz 已在 k8s/OpenShift 上产品化此形态。风险最低，因为不依赖 PD/RDMA。
2. **弹性 G1 + KVCR 的 L2+**：kvcached 页事件（map/unmap）作为 KVCR 的 offload 触发信号；需解 VMM×NIXL 注册问题。
3. **D-direct 类优化**：kvcached 的 VA 稳定性（tensor 地址不变、物理页透明 remap）对「KV 已在目标节点 HBM」的判定友好，但跨节点位置视图仍需 router/控制面另建——kvcached 自己不产生任何位置元数据。

## 与 SGLang Elastic Memory Pool 对照

同为 CUDA VMM 按需 commit，但目标正交（详见 [`../sglang/elastic-memory-pool.md`](../sglang/elastic-memory-pool.md)）：

| 维度 | kvcached | SGLang Elastic Memory Pool |
|------|----------|---------------------------|
| 共享单位 | 同卡多**进程**（引擎实例） | 单进程内 2 个**子池**（Mamba/KV 双形态） |
| 物理页 shrink | 有（unmap+release 回驱动） | 当前 monotonic 只增，无主动 unmap |
| 协调 | 跨进程：driver 仲裁 + shm 记账 | 进程内：v2p 表重映射 + compacting free |
| 接入 | 引擎无关 autopatch（vLLM+SGLang） | SGLang 原生 `--enable-unified-memory` |
| 配额 | 有（kvctl 外部设限） | 无（两池争用即全部语义） |

两者可叠加：kvcached 管进程间弹性，Elastic Pool 管进程内形态间弹性。

## 与 lake 的关系

**值得参考的点**：

1. **VMM 页级弹性是 L0 归池的可行机制**：lake 的 L0（HBM）归存储池，池 agent 需要按需分配/回收 HBM 物理页——kvcached 在 vLLM/SGLang 真实引擎上验证了「VA 预留 + 页级 map/unmap + 引擎无感（tensor 地址稳定）」的工程闭环，包括 TP 广播 IPC、布局约束（2MB 页对齐、block≤page）、async 调度下 unmap 前 `device_synchronize` 等坑。
2. **zero page COW**：冷启动物理零占用 + 首次写触发真实分配，可直接用于 lake 计算节点拉起的 L0 预热策略。
3. **带外配额通道**：shm + 轮询的 kvctl 模式足够轻；lake 的池级配额（软/硬 + 借用）可用同类带外通道下发给 L0 agent，不必走请求路径。
4. **APC 与弹性共存的占用统计**：`get_page_occupancy` 按页统计存活块决定能否 unmap——lake L0 做「引用数>0 冻结」时需要同款的页级引用计数。

**关键差异（不照搬）**：

1. **去中心化 vs 控制面权威**：kvcached 无全局视图（shm 只记账、driver 仲裁物理页、TOCTOU 靠 headroom 缓解）；lake 的 L0 位置归存储控制面强一致管理（radix + `locations`），放置是权威决策不是各进程自查。多实例单机场景 kvcached 的轻量近似够用，跨节点存算分离必须权威视图。
2. **KV 语义无关 vs 块级位置**：kvcached 不知道页里装的是哪个前缀的 KV（索引全在引擎 APC）；lake 的 L0 slot 有块级身份与位置，支撑 D-direct 与 F4 恢复。
3. **RDMA 是后补的 vs 一等公民**：kvcached 的 PD 支持刚起步且与 VMM 弹性有张力；lake 的 Transfer Bus 从设计上就要求 L0 slot 可注册可传输——**VMM 页与 RDMA MR 的共存策略需要在 lake 存储层设计时前置解决**（对照 [`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md) §5：归池后注册的是池 slot，不是引擎 BlockPool 再挂句柄）。
4. **实例私有弹性 vs 池统一编址**：kvcached 的弹性止步于单机单卡（VA 空间进程私有）；lake 的 L0–L3 统一编址，HBM 只是池的一层物理载体。

## 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| VA 预留（2MB 对齐） | `csrc/ftensor.cpp::alloc_virtual_mem` |
| zero page COW 初始化 | `csrc/ftensor.cpp::FTensor::init_with_zero_` |
| 页级 map（unmap zero → cuMemCreate → cuMemMap） | `csrc/ftensor.cpp::FTensor::map`、`csrc/page.cpp::GPUPage` |
| CUDA/HIP VMM 原语封装 | `csrc/inc/gpu_vmm.hpp::gpu_vmm::{address_reserve,mem_create,mem_map,mem_unmap,mem_release}` |
| 页生命周期 + 热页缓存（min5/max10） | `csrc/page_allocator.cpp::PageAllocator::{alloc_page,free_page,prealloc_worker}` |
| 物理余量自查（0.95 headroom） | `csrc/page_allocator.cpp::PageAllocator::get_avail_physical_pages` |
| 配额 resize + 100ms 轮询 | `csrc/page_allocator.cpp::PageAllocator::{resize,resize_watcher}` |
| shm 记账（flock+mmap） | `csrc/inc/mem_info_tracker.hpp::MemInfoTracker/RwLockedShm` |
| block→page、APC 页占用 | `kvcached/kv_cache_manager.py::KVCacheManager.{alloc,free,get_page_occupancy}` |
| 配额状态机（revision） | `kvcached/control.py::set_instance_memory_limit` |
| kvctl CLI | `kvcached/cli/kvctl.py::cmd_limit/cmd_limit_percent` |
| 虚拟容量超卖（不折减 peer 占用） | `kvcached/integration/vllm/patches.py::_get_virtual_kv_capacity_bytes` |
| APC 内存上界 | `kvcached/integration/vllm/patches.py::_get_max_cached_blocks`（`KVCACHED_MAX_CACHED_TOKENS`） |
| vLLM/SGLang 补丁面 | `kvcached/integration/{vllm,sglang}/patches.py::patch_*` |
| TP 广播 IPC | `kvcached/tp_ipc_util.py::broadcast_kv_tensors_created` |
| PD/NIXL 兼容 | `kvcached/integration/vllm/nixl_compat.py`、`docs/PD_DISAGGREGATION.md` |
| 示例前端（路由+sleep） | `controller/{router,sleep_manager,frontend}.py` |
