# SGLang — 权重加载与外部集成边界

> 源码：`3rdparty/sglang`（路径前缀默认 `python/sglang/srt/`）。本篇记录权重冷启动的 I/O/远端加载路径，以及 SGLang 对外部依赖的集成方式；它们是计算层参考，不是 lake 的存储池实现。

## 一句话

SGLang 已支持**并行读取 checkpoint shard**、**后台预取 OS page cache**和**从远端实例传入权重**。但本地多线程加载只并行 shard I/O，参数遍历/模型绑定仍按 iterator 串行推进；预取和多线程在共享网络存储上会争抢 I/O，代码明确在未显式强制时二选一。

## 本地 checkpoint：并行的范围与限制

`DefaultModelLoader._get_weights_iterator` 根据格式选择 safetensors、PT、FastSafeTensors 或 npcache iterator。safetensors 的 `buffered_multi_thread_safetensors_weights_iterator` 用固定大小 sliding window：

```text
max_workers 个 shard 并发读取
    + 1 个预取完成、待 yield 的 shard
    → 按 iterator 顺序逐参数交给模型 load_weights
```

因此它降低多 shard I/O 的串行等待，但不会让同一 rank 的 `named_parameters()` 绑定、量化后处理或 GPU 参数写入变成张量级并行。峰值 host RAM 约为 `(workers + 2) × shard_size`，线程数应随 NVMe/NFS 带宽与内存预算校准。

`_prefetch_all_checkpoints` 是另一条路径：每个 node-local rank 负责一部分 shard，后台顺序读入共享 OS page cache，降低同节点 DP rank 重复从 NFS/Lustre 读取同一 checkpoint 的网络 I/O。loader 发现 mmap prefetch 与默认多线程加载同时开启时，除非用户显式设置线程参数，会退回单线程 loader，避免两者读取相同 shard 而 oversubscribe 存储。

| 机制 | 直接收益 | 代价 / 约束 |
|------|----------|-------------|
| 多线程 shard iterator | 本地多盘或可并发对象存储提高加载带宽 | host RAM 与随机/并发 I/O 增长 |
| 后台 page-cache prefetch | 同节点多 rank 复用 page cache，减少远端重复读 | 只对同一 node 有效；受 page-cache 容量影响 |
| 排序/TP stagger | 降低各 TP rank 同时撞同一 shard | 只重排文件访问，不改变最终权重语义 |
| `drop_cache_after_load` | 降低加载后 page-cache 压力 | 后续 mmap 访问可能重新 page fault |

## 从远端实例加载

`RemoteInstanceModelLoader` 先在本地构造空模型，再按 `remote_instance_weight_loader_backend` 选择：

| backend | 路径 | 含义 |
|---------|------|------|
| NCCL | `load_model_from_remote_instance_by_nccl` | seed 实例与目标实例建 group；逐参数 broadcast |
| Transfer Engine | `register_memory_region` → `load_model_from_remote_instance_by_transfer_engine` | 注册目标模型内存区域，由 transfer engine 搬运权重 |
| ModelExpress | `modelexpress.engines.sglang.loader.MxModelLoader` | 外部包负责具体模型传输/加载 |

这说明 SGLang 有“已有 warm 实例 → 新实例”的 cold-start 加速通路，但不是把权重变成全局强一致池：模型对象和参数生命周期依旧属于 runner。对 lake，可借鉴“控制面协调 + 数据面直传 + 预注册目的地址”的分层，而权重版本、分层放置和权威副本仍必须归独立存储池。

## 外部依赖的集成方式

此前“对其他仓的 patch”不应笼统写成 SGLang 直接维护了多个上游 fork。当前树中可观察到四种不同机制，维护和升级风险不同：

| 类型 | 例子 | 风险与 lake 的取舍 |
|------|------|--------------------|
| Python runtime monkey patch | `patch_torch.py::monkey_patch_torch_reductions`、`monkey_patch_torch_compile` | 依赖私有 API/固定 tuple index，升级 Torch 必须保留版本守卫；lake 不应把它当稳定抽象 |
| 构建时镜像修补/依赖组合 | `docker/Dockerfile` 固定 DeepEP/FlashInfer/Mooncake 等版本，并处理系统库兼容 | 可复现镜像与上游兼容性绑得很紧；应独立记录 ABI 与版本矩阵 |
| CMake 拉取并锁定源码 | `sgl-kernel/CMakeLists.txt` 的 CUTLASS、fmt、Triton、FlashInfer、sgl-attn `FetchContent` + hash | 是构建时 vendoring，不是运行时 monkey patch；hash 固定能提高可复现性，也增加安全更新责任 |
| Python 包版本钉住 | `python/pyproject.toml` 的 Transformers、xgrammar、FlashInfer 版本 | 防 API/ABI 漂移；升级需跑 compute、guided、kernel 三类回归 |

**对 lake**：当前不得把 SGLang patch 代码直接复制进三语言项目。若未来确实依赖外部 runtime，应优先使用公开接口并把版本/ABI 约束固化在本仓构建配置；只有被已确认的上游缺陷阻塞时，才加入最小、可删除且有版本守卫的兼容层。

## 代码索引

| 机制 | 文件:符号 |
|------|-----------|
| 本地权重 iterator 选择与 I/O 互斥 | `model_loader/loader.py::DefaultModelLoader._get_weights_iterator` |
| safetensors 多线程滑动窗口 | `model_loader/weight_utils.py::buffered_multi_thread_safetensors_weights_iterator` |
| node-local page-cache 预取 | `model_loader/weight_utils.py::_prefetch_all_checkpoints` |
| 远端实例加载入口 | `model_loader/loader.py::RemoteInstanceModelLoader.load_model` |
| NCCL 远端传参 | `model_loader/loader.py::RemoteInstanceModelLoader.load_model_from_remote_instance_by_nccl` |
| Torch 兼容 patch | `utils/patch_torch.py::monkey_patch_torch_reductions` / `monkey_patch_torch_compile` |
| 原生依赖锁定 | `sgl-kernel/CMakeLists.txt::FetchContent_Declare` |
| Python 依赖钉住 | `python/pyproject.toml` |
| 容器内版本与系统兼容处理 | `docker/Dockerfile` |
