# kvcached：GPU 虚拟内存与弹性 KV Cache

> 调研快照 2026-09-08，submodule `60cad94`（2026-08-22，v0.1.5）。源码 `3rdparty/kvcached/`。
> 上游 [ovg-project/kvcached](https://github.com/ovg-project/kvcached)（Apache-2.0），官网 [kvcached.org](https://kvcached.org/)。论文：Prism（OSDI 2026，[arXiv 2505.04021](https://arxiv.org/abs/2505.04021)）、GPU OS 愿景（[arXiv 2508.08448](https://arxiv.org/abs/2508.08448)）。
> 本文回答两个问题：宣称的四个特性是否属实（逐条对源码）；能在 Dynamo 基础上借鉴什么。

## 1. 定位

kvcached 把 OS 虚拟内存的做法搬到 GPU KV cache：虚拟地址（VA）与物理页解耦，物理页按需映射、空闲即释放。同一张卡上的多个 vLLM/SGLang 实例由此共享显存，不再需要启动时静态切分。

它不做的事：

- 不分层：不管 DRAM/SSD/对象存储（那是 HiCache/LMCache/KVCR 的层）；
- 不索引：不维护前缀树或位置视图，前缀复用仍是引擎自己的 APC；
- 不是 daemon：名字来自 "KV cache daemon"，实现是嵌在引擎进程里的库（见 2.2）；
- 不碰权重：只虚拟化 KV 张量，权重的 sleep/wake 靠引擎自身能力。

支持 MHA/GQA/MLA/滑窗/hybrid 注意力，TP/PP，SGLang ≥0.4.9、vLLM ≥0.8.4。

## 2. 四特性核实

| # | 宣称 | 结论 | 实际机制 |
|---|------|------|----------|
| 1 | 跨进程显存超卖 | 成立 | VA 私有 + 物理页按需映射，闲置页经驱动流转 |
| 2 | 独立 daemon 全局统筹 | 与宣传不符 | 无 daemon；进程内库 + /dev/shm 记账 + 驱动仲裁 |
| 3 | 零物理分配冷启动 | 成立（仅 KV） | VA 预留 + zero page（COW），启动物理占用约一页 |
| 4 | 多租户硬配额 | 成立（有边界） | 硬在分配拒绝；in-use 页不能强收 |

### 2.1 跨进程显存超卖：成立

![按需映射与跨进程流转](figures/vmm-mechanism.svg)

- 每个实例启动时 `cuMemAddressReserve` 一段私有 VA（`csrc/ftensor.cpp::alloc_virtual_mem`）。KV block 落到 2MB 页；页被使用时才 `cuMemCreate`+`cuMemMap` 物理页（`csrc/page.cpp::GPUPage`），页全空时 unmap+release 还给驱动（`csrc/page_allocator.cpp::PageAllocator::free_page`）。
- 虚拟容量不因他实例占用而折减：vLLM 侧调度器可见容量固定为 `总显存 × gpu_memory_utilization`（`integration/vllm/patches.py::_get_virtual_kv_capacity_bytes`）。同卡每个实例都可按 90% 规划自己的虚拟 KV 空间。
- 实例 A 空闲时物理页回到驱动空闲池；实例 B 映射前查 `cudaMemGetInfo`（默认留 5% headroom）后即可使用。
- 分配性能：后台线程维持 5–10 页已映射缓存（`KVCACHED_MIN/MAX_RESERVED_PAGES`），分配快速路径为微秒级。
- 前缀缓存与弹性共存：被 APC 引用的页保持映射，按页统计存活块（`KVCacheManager::get_page_occupancy`）；APC 占用上界由 `KVCACHED_MAX_CACHED_TOKENS` 控制。

官方 benchmark：3 个 Llama-3.1-8B 实例共享一张 A100-80G，间歇峰值负载下 TTFT 降 2–28×。

![TTFT 对比（3×Llama-3.1-8B @ A100-80G）](figures/ttft-mean.svg)

（图源：`3rdparty/kvcached/assets/ttft_results/ttft_mean.svg`）

### 2.2 独立 daemon 全局统筹：与宣传不符

![协调机制](figures/coordination.svg)

名字是 daemon，实现不是。全局协调由四件套完成，没有中心进程：

1. 引擎进程内的 C++ 库（PageAllocator/FTensorAllocator）自决映射；
2. 每进程一段 `/dev/shm` 记账（flock+mmap，三字段 total/used/prealloc，`csrc/inc/mem_info_tracker.hpp::MemInfoTracker`）；
3. kvctl CLI 带外读写这些段（`kvcached/cli/kvctl.py`）；
4. CUDA 驱动仲裁物理页：先 `cuMemCreate` 先得。

防 OOM 也是分布式的：各进程映射前各自查全卡空闲，加 headroom，失败回滚。两个进程同时看到空闲、同时映射的竞态（TOCTOU）存在，没有全局互斥。

`controller/` 是示例级前端：OpenAI 兼容路由 + 流量监控 + sleep 管理（tmux 编排），管「请求发给谁、闲了睡」，不管显存分配。

### 2.3 零物理分配冷启动：成立（仅 KV）

- 启动时整段 VA 先全部映射到同一个 zero page（2MB），物理占用约一页（`csrc/ftensor.cpp::FTensor::init_with_zero_`）。首次使用某页时才换成真实物理页（`FTensor::map`），即 COW。
- 省掉的是 KV 物理预分配，以及「按空闲显存反推 KV 容量」的 profiling 依赖；权重加载、CUDA graph 不省。
- VA 稳定、张量地址不变，物理页重映射对 kernel 和 CUDA graph 透明。这是这条路线成立的关键性质。
- 完整的 serverless 拉起 = kvcached（KV 零占用）+ 引擎 sleep/wake（权重）+ controller 按流量唤醒（`examples/06_serverless_serving`）。

### 2.4 多租户硬配额：成立（有边界）

链路：`kvctl limit <实例> <大小>` → 写该实例 shm 段的 total 字段 → 引擎内线程 100ms 轮询发现变化（`PageAllocator::resize_watcher`）→ `resize()` 收缩或扩张可用页。

- 收缩只能回收空闲页；in-use 高于新限额时记为 deferred，等引擎自己释放。不能强收在用的页。
- 「硬」体现在分配拒绝：可用页收缩后，超限分配直接失败，引擎表现为 KV 满。单个长请求吃不光整卡。
- 配额上限是启动时定的虚拟容量；粒度是单个引擎进程组；并发写用 revision 防冲突（`kvcached/control.py::set_instance_memory_limit`，状态：applied/deferred/stale/conflict）。

## 3. 架构与接入

```
kvctl / kvtop（带外 CLI）── 读写 ──▶ /dev/shm/kvcached_engine_<pgid>
                                          ▲ 100ms 轮询（resize_watcher）
controller/（示例前端：路由 + sleep 管理）
引擎进程（vLLM/SGLang，autopatch 注入）
  └─ KVCacheManager(py)：block→page、alloc/free、APC 共存、占用统计
     └─ PageAllocator(C++)：页生命周期、热页缓存、resize
        └─ FTensor(C++)：VA 预留、zero page、页级 map/unmap
           └─ gpu_vmm：cuMemAddressReserve/Create/Map/Unmap/Release（含 HIP 移植层）
```

- 接入：`ENABLE_KVCACHED=true` + `KVCACHED_AUTOPATCH=1`，`.pth` 在 Python 启动时注册 import 钩子，引擎零改动。代价是补丁面跟随引擎版本（vLLM 补丁约 1900 行，官方标注测到 v0.24.0 / SGLang v0.5.15）。
- 布局：默认每层一对 K/V 张量；`KVCACHED_CONTIGUOUS_LAYOUT` 为全层单张量 + compound page；MLA 单 buffer。
- TP/PP：TP 各 worker 经 IPC 广播 map/unmap；C++ 线程不持锁回调 Python（GIL 死锁教训，issue #371）。

## 4. 对 Dynamo 的可借鉴点

![与 Dynamo 组合](figures/with-dynamo.svg)

### 4.1 直接可组合的形态

1. **单机多模型超卖 + Dynamo 集群编排**。Dynamo 管跨节点调度，kvcached 管单机显存弹性。Prism（OSDI 2026）已验证这个两级结构，Red Hat Sardeenz 在 k8s/OpenShift 上产品化。不依赖 PD/RDMA，风险最低。
2. **serverless 扩缩容**。Dynamo Planner 拉起 worker 时，kvcached 省掉 KV 物理预分配和 profiling 定容；controller 的「闲置 → sleep → 按请求唤醒」补上 Dynamo 没有的细粒度节能。

### 4.2 机制级借鉴

3. **碰 GPU 的正确层次**。KVBM 被 sunset 的原因之一是直接管理 GPU 侧 block 布局、和引擎抢资源（见 [`../kvcr/overview.md`](../kvcr/overview.md)）。kvcached 示范了更安全的层次：只管页映射，不碰 KV 语义。Dynamo 若重做 GPU 侧弹性，应在 VMM 页层做，不在 block 层做。
4. **页分配器工程细节**。2MB 页、5–10 页热缓存、微秒级分配路径、映射失败回滚。任何页粒度 GPU 分配器都用得上。
5. **活进程改配额的协议**。带外写 shm + 100ms 轮询 + revision 状态机（applied/deferred/stale/conflict）。不占请求路径，Dynamo/KVCR 做动态限额可照搬。
6. **页级占用统计**。`get_page_occupancy` 按页统计存活块，决定页能否释放。KVCR 做 GPU→DRAM 卸载的驱逐粒度判断时需要同类信息。
7. **路由信号**。shm 段暴露每实例 used/limit/prealloc。Dynamo Router 目前按 KV 命中和负载路由，可加「可映射物理余量」信号，避免把请求打到映射会失败的实例。

### 4.3 要先解的问题

8. **VMM × RDMA 注册**。NIXL/KVCR 直传要注册显存；页 unmap/remap 会让注册失效，pin 住又失去弹性。kvcached 自己的 PD 只验证了 NixlConnector 一个（`docs/PD_DISAGGREGATION.md`）。可选路线：按页注册/注销、传输期 pin、整段 VA 预注册 + 物理页池化。
9. **多进程映射无互斥**。TOCTOU 靠 headroom 缓解。集群级部署时，这个责任更适合交给控制面（lake 的做法），而不是各进程自查。

## 5. 对照

### SGLang Elastic Memory Pool

同为 CUDA VMM 按需 commit，目标不同（详见 [`../sglang/elastic-memory-pool.md`](../sglang/elastic-memory-pool.md)）：

| 维度 | kvcached | SGLang Elastic Pool |
|------|----------|---------------------|
| 共享单位 | 同卡多进程 | 单进程内两个子池 |
| 物理页归还 | 有（unmap+release） | 当前只增 |
| 协调 | 驱动仲裁 + shm 记账 | 进程内 v2p 重映射 |
| 接入 | 引擎无关 autopatch | SGLang 原生 |
| 配额 | 有（kvctl） | 无 |

两者可叠加：kvcached 管进程间，Elastic Pool 管进程内形态间。

### lake

值得参考：VMM 页弹性在真实引擎上的工程闭环（TP 广播、布局约束、async 调度下 unmap 前先同步）；zero page COW；带外配额通道；页级占用统计（对应 lake「引用数>0 冻结」）。

不照搬：kvcached 无全局视图、不知页内 KV 身份、RDMA 是后补、弹性止步单机单卡。lake 的 L0 归池要求控制面权威位置、块级身份，且 Transfer Bus 要在设计时前置解决 VMM×RDMA 共存（见 [`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md) §5）。

## 6. 代码索引

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
