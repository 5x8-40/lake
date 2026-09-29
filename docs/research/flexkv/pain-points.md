# FlexKV — 痛点与 lake 对照

> 调研快照：2026-08-28；`3rdparty/flexkv` @ `a5c8f12`（2026-09-29 补充 §6，指针不变）。  
> [overview.md](overview.md) · [architecture.md](architecture.md)。  
> 对照：LMCache、Dynamo KVBM、HiCache、[`../hbm-tier-and-offload.md`](../hbm-tier-and-offload.md)、[`../../architecture/kv-cache-pool.md`](../../architecture/kv-cache-pool.md)。

## 1. 权威归属

| 现象 | 证据 | lake |
|------|------|------|
| GPU 页归引擎 | `request_finished` 注释；驱逐文档 | L0 由池分配/放置 |
| CPU/SSD 树在 FlexKV 进程 | `GlobalCacheEngine` 每层 `CacheEngineAccel` | 树在存储控制面，etcd 降频 checkpoint |
| connector 可选 | vLLM `kv_connector=FlexKVConnectorV1` | 池是必经路径 |
| SGLang 主干两套平行缓存层级 | `FlexKVRadixCache` ∥ `HiRadixCache`；scheduler 钩子须 OR `enable_hierarchical_cache`/`enable_flexkv` 两个 flag，漏了就锁不释放、集群卡死（[sglang#29701](https://github.com/sgl-project/sglang/pull/29701) 合入时实测修复） | 一份位置权威、引擎不持有索引，无平行层级钩子税 |

## 2. 前缀与索引

| 现象 | 证据 | lake |
|------|------|------|
| GPU 命中看引擎 APC | `num_computed_tokens`；stats 分 `num_gpu_matched_tokens` / `num_flexkv_matched_tokens` | 位置只在控制面 |
| CPU/SSD/REMOTE 各一棵树 | `cpu_cache_engine` / `ssd_cache_engine` | 一份 radix，节点上 `locations` |
| 跨节点靠 Redis 快照 | `HierarchyLRCacheEngine` local + remote index | 控制面内存权威，Router 读镜像 |
| Dynamo events 默认 medium=`CPU` | `KVEventCollector.publish_stored` | 不靠 best-effort 事件当 F4 依据 |

## 3. 分层与冷热

| 现象 | 证据 | lake |
|------|------|------|
| 层间尽力 inclusive，驱逐不 demote | 驱逐文档 | L0/L1 缓存副本；L2/L3 按移动；主动 promotion |
| 无「把前缀放到从未算过它的 GPU」 | GPU 只注册本进程页 | 池侧预放置 L0 |
| GDS / Mooncake 是传输优化 | GDS、TE、store adapter | 可作 Transfer Bus 后端对照，不替代位置权威 |

## 4. 故障与弹性

| 现象 | 证据 | lake |
|------|------|------|
| worker 退出后本机树和 GPU 页一起没 | 进程内引擎 + FlexKV | F4 从 L2 续推，位置仍在控制面 |
| Redis 条目可指向已死节点 | GMS + lease/TTL | 权威视图随放置/下线改，不留过期 Device overlap 当真相 |
| `reset_cache` 清 radix | `vllm_v1_adapter.py`::`reset_cache` | 权重/revision 换树，不是清插件索引了事 |

## 5. 执行模式

| 现象 | 证据 | lake |
|------|------|------|
| 卸载命中 = 从 CPU/SSD 拉回引擎已分好的槽 | `set_gpu_blocks` + H2D | Pool 命中 ≠ 本地 HBM 命中 |
| 无 D-direct | 路由若用 Dynamo，是 worker overlap | 前缀已在目标 HBM 则直跳 |
| 与 Dynamo P2P 复用互斥 | `docs/dynamo_integration` CAUTION | 一份位置，一种选路输入 |

## 6. 异构 KV 与 state 边界

| 现象 | 证据 | lake |
|------|------|------|
| 层 shape 异构要注册 layer group | Gemma4：50×SWA(16h/256d) + 10×full(4h/512d)，`LayerGroupSpec` 按 `(num_kv_heads, head_size)` 分组（`docs/gemma4_support.md`） | 池存不透明字节块，不解释布局；异构描述只出现在 connector 边缘侧 |
| 统一切块靠"块内分组拼接"保住 | 单一 BLOCKFIRST buffer，block 内按组顺序排各层 K/V，`token_size_in_bytes` 按组求和，传输按组各调一次 `transfer_kv_blocks()` | lake block 同样定长；若边缘侧要承载异构层模型，这是候选打包模式 |
| 布局假设会踩坑 | triton `[N,2,B,H,D]` vs flash_attn `[2,N,B,H,D]` dim0/1 互换，信配置算错 stride → 改为从 tensor 实际 `stride()` 探测 | 布局元数据必须由注册方实测上报，控制面不内置假设 |
| 注册前不知道组 → buffer 估算错 | max(head)×max(dim) 高估 2.18 倍，16GB 分 546 块 vs 正确 1191；修复为延迟创建 StorageEngine | 池按注册配额算账，容量视图以注册后实测为准 |
| Mamba/线性注意力 state 不支持 | 全仓 `mamba|conv_state|recurrent|linear_attn` 零命中（2026-09-29）；抽象是 per-token 块，递归 state 需块边界快照 | lake 已声明 t-type/r-type：block 内装逐 token KV 还是紧凑 state 快照由布局元数据声明（[`../../architecture/storage-layer.md`](../../architecture/storage-layer.md)） |
| 引擎侧 hybrid 管理被旁路 | vLLM 开 `--kv-transfer-config` 即禁用 Hybrid KV Cache Manager，SWA 窗口外 KV 照存 | connector 是必经路径时，窗口/稀疏语义应由布局元数据保留，而不是靠引擎管理器兜底 |

## 可直接借鉴

1. **connector 时序**：match → alloc → `set_gpu_blocks` → 异步传输；结束 delay-free。P5 对接引擎时要对齐「引擎槽已有、再填传输图」。
2. **GPU 只映射**：IPC/fabric handle，不自建 HBM 池。和 lake「计算不拥有 HBM」方向相反，但把「句柄 vs 池」划清了，可对照 Dynamo G1。
3. **每层 radix + mempool + lock/ready**：`CRadixTreeIndex` 的 ready/lock/evict 和 Dynamo kvbm-logical（前缀树 + 每层块池）同类。lake 合成一份树 + 多层 `locations`，不要按介质拆三套进程内树。
4. **布局探测**：从 GPU tensor shape 推 LAYERFIRST / LAYERBLOCK，避免写死 vLLM 版本布局。Gemma4 案例进一步：连"配置声明的布局类型"都不可信，要从 tensor 实际 `stride()` 反推（`_get_gpu_strides_from_tensor`）。
5. **SWA 挂在 Full 节点上**：与 HiCache 同思路，避免 Full/SWA 两棵树漂移。
6. **统一切块 + 块内分组拼接**：异构层模型不拆多种 block 大小，单块定长、块内按 layer group 顺序拼接、按组记 stride——保持分配/匹配/传输全链路"一种块"的简化假设。

## 明确不照搬

- 把 HBM 排除在卸载栈索引之外，另留引擎 APC。
- Redis 周期快照当集群位置真相。
- 驱逐不写下一层。
- 用 connector 可选插件代替存储池。
