# SGLang HiCache — 存储后端与统一接口

> 源码:`python/sglang/srt/mem_cache/hicache_storage.py`、`python/sglang/srt/mem_cache/storage/backend_factory.py`。

## 统一接口 `HiCacheStorage(ABC)`

L3 后端统一抽象,位于 `hicache_storage.py:141`。三套接口代际并存:

| 代际 | 方法 | 特点 |
|------|------|------|
| **v2**(新,多 pool) | `batch_exists_v2` / `batch_get_v2` / `batch_set_v2` | 接收 `List[PoolTransfer]`,支持 hybrid/multi-pool(Mamba/SWA/DSA/Draft) |
| **v1**(批量,host_indices) | `batch_get_v1` / `batch_set_v1` | 零拷贝路径用(`_page_get_zero_copy`/`_page_set_zero_copy`) |
| **legacy**(抽象,必须实现) | `get` / `set` / `exists` / `batch_get` / `batch_set` / `batch_exists` | 返回连续命中数 |

辅助:`register_mem_pool_host`、`clear`、`get_stats`。

关键数据类:`HiCacheStorageConfig`(`tp_rank/tp_size/pp_rank/pp_size/attn_cp_rank/attn_cp_size/is_mla_model/tp_lcm_size/should_split_heads/extra_config`)、`PoolTransfer`、`PoolHitPolicy`(`ALL_PAGES`/`TRAILING_PAGES`)、`PoolName`。

> **与 lake 的对照**:SGLang v2 把 Mamba/SWA/DSA/Draft 各开独立 pool,按类型**物理分池**存 + 各自 `PoolHitPolicy`。lake 不物理分池——t-type/r-type 区分**只存在于 HBM(L0)存储形态**(降 r-type 占用),L1–L3 统一按 128-token block 存储池承载,block 内装逐 token KV 还是紧凑 state 快照由布局元数据声明;`TRAILING_PAGES` 语义被吸收为"SWA 落下层存 trailing pages"的命中策略,而非独立 pool。详见 [`../../architecture/storage-layer.md`](../../architecture/storage-layer.md) "KV 类型"节、[`../../architecture/kv-cache-pool.md`](../../architecture/kv-cache-pool.md) "t-type / r-type"。

## 已注册后端

`backend_factory.py` 注册的后端:

| name | 类 | 传输 | 零拷贝 | MLA 去重 | head split |
|------|----|------|--------|----------|------------|
| `file` | `HiCacheFile` | 本地文件 `.bin` | 否(memoryview) | 是(rank 共享 key) | 否 |
| `mooncake` | `MooncakeStore` | RDMA/TCP(Mooncake TransferEngine) | 是(`batch_put_from`/`batch_get_into`) | 是(rank-agnostic key) | 是(`tp_lcm_size`) |
| `hf3fs` | `HiCacheHF3FS` | 3FS filesystem(usrbio) | 是(page_first/direct) | 是(rank 0 only) | 否 |
| `nixl` | `HiCacheNixl` | NIXL 插件(POSIX/GDS/3FS/OBJ) | 是(整 buffer 注册) | 是(`backup_skip`) | 否 |
| `aibrix` | `AibrixKVCacheStorage` | AIBrix KVCache(进程内) | 否 | **不支持 MLA** | 否 |
| `eic` | `EICStorage` | EIC KV(GPU-direct RDMA) | 是(page_first) | 否(全 rank 写) | 否 |
| `simm` | `HiCacheSiMM` | SiMM RDMA(NUMA-aware) | 是(register_mr) | 否 | 否 |
| `mori`(umbp) | `UMBPStore` | UMBP(DRAM+SSD,SPDK/io_uring) | 是(RDMA IOEngine) | 是(SharedSSDLeader/Follower) | 是 |
| `dynamic` | 用户自定义 | 任意 | 取决实现 | 取决实现 | 取决实现 |

## 非后端注册的替代方案

- `lmcache`(`LMCRadixCache` 继承 `RadixCache`,非 `HiCacheStorage`)— radix-cache 层集成,`--enable-lmcache` + `--lmcache-config-file`。见 [../lmcache/overview.md](../lmcache/overview.md)。
- `flexkv`(`FlexKVRadixCache` 继承 `RadixCache`)— `--enable-flexkv`,rank-0 leader 模式 + eventfd layerwise。源码与 HBM 角色见 [`../flexkv/`](../flexkv/)。

## 异构 TP(`tp_lcm_size`)

`_generate_storage_config`:`tp_lcm_size` 为所有共享存储的 TP size 的 LCM;`should_split_heads = not is_rank_replicated and layout=="page_head" and tp_lcm_size > tp_size`。Mooncake/UMBP 据此 `split_factor = tp_lcm_size // tp_size` 生成 `2*split_factor` 个 key/页,使不同 TP size 集群可复用 KV。

## 运行时 attach/detach

HTTP 端点 `PUT/DELETE/GET /hicache/storage-backend`,经 HTTP Server → TokenizerManager → Scheduler(严格 idle 检查 `is_fully_idle`)→ `HiRadixCache.attach_storage_backend`/`detach_storage_backend`。无需重启;DP 时全体 rank 须成功。

## 关键参数

| 参数 | 语义 |
|------|------|
| `--enable-hierarchical-cache` | 总开关 |
| `--hicache-ratio` | host pool / device pool 容量比,必须 >1 |
| `--hicache-size` | host pool GB 数(1GB=1e9 bytes),per rank,覆盖 ratio |
| `--page-size` | 每页 token 数,决定存储/检索粒度 |
| `--hicache-mem-layout` | `layer_first`/`page_first`/`page_first_direct` |
| `--hicache-io-backend` | `direct`/`kernel` |
| `--hicache-write-policy` | `write_back`/`write_through`/`write_through_selective` |
| `--hicache-storage-prefetch-policy` | `best_effort`/`wait_complete`/`timeout` |
| `--hicache-storage-backend` | `file`/`mooncake`/`hf3fs`/`nixl`/`aibrix`/`eic`/`simm`/`mori`/`dynamic` |
| `--hicache-storage-backend-extra-config` | JSON 串或 `@file`(toml/yaml/json) |

## 代码索引

> 沿代码回溯用。符号名锚定,行号会漂移——找不到时 `grep -n "符号名" <文件>`。

| 机制 | 文件:符号 |
|------|-----------|
| L3 后端统一抽象 | `mem_cache/hicache_storage.py`::`HiCacheStorage` (L141) |
| v2 多 pool 接口 | `HiCacheStorage.batch_exists_v2` / `batch_get_v2` / `batch_set_v2` |
| v1 批量零拷贝接口 | `HiCacheStorage.batch_get_v1` / `batch_set_v1` |
| legacy 抽象接口 | `HiCacheStorage.get` / `set` / `exists` / `batch_get` / `batch_set` / `batch_exists` |
| 配置/数据类 | `hicache_storage.py`::`HiCacheStorageConfig` / `PoolTransfer` / `PoolHitPolicy` / `PoolName` |
| 后端注册 + 惰性加载 | `mem_cache/storage/backend_factory.py`::`create_storage_backend`(name→class 映射) |
| `file` 后端 | `storage/file_storage.py`::`HiCacheFile` |
| `mooncake` 后端 | `storage/mooncake_store/`(MooncakeStore,`batch_put_from`/`batch_get_into`/`_batch_exist`) |
| `hf3fs` 后端 | `storage/hf3fs_storage.py`::`HiCacheHF3FS` |
| `nixl` 后端 | `storage/nixl_storage.py`::`HiCacheNixl` |
| `aibrix` 后端 | `storage/aibrix.py`::`AibrixKVCacheStorage` |
| `eic` 后端 | `storage/eic.py`::`EICStorage` |
| `simm` 后端 | `storage/simm.py`::`HiCacheSiMM` |
| `mori`/umbp 后端 | `storage/umbp.py`::`UMBPStore` |
| `dynamic` 自定义 | `storage/dynamic.py` |
| LMCache 集成(radix 层,非 HiCacheStorage) | `mem_cache/lmcache_radix_cache.py`::`LMCRadixCache` |
| FlexKV 集成 | `mem_cache/flexkv_radix_cache.py`::`FlexKVRadixCache` |
| 异构 TP(tp_lcm_size/head split) | `hicache_storage.py`::`_generate_storage_config`(`should_split_heads`/`split_factor`) |
| 运行时 attach/detach | `hiradix_cache.py`::`attach_storage_backend` / `detach_storage_backend`(经 HTTP `/hicache/storage-backend` → Scheduler `is_fully_idle` 检查) |
| 相关 server args | `server_args.py`(`--enable-hierarchical-cache`/`--hicache-ratio`/`--hicache-size`/`--page-size`/`--hicache-mem-layout`/`--hicache-io-backend`/`--hicache-write-policy`/`--hicache-storage-prefetch-policy`/`--hicache-storage-backend`) |
