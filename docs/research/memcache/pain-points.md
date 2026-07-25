# Ascend MemCache — 痛点与 lake 对照

> 调研快照：2026-07-24；`3rdparty/memcache` @ `14b4e35`。  
> [overview.md](overview.md) · [architecture.md](architecture.md)。  
> 对照实现：Mooncake `mooncake-store`；LMCache `StorageManager`；lake [`../../architecture/kv-cache-pool.md`](../../architecture/kv-cache-pool.md)。

## 1. 键与前缀复用

| 现象 | 证据 | lake |
|------|------|------|
| Exact-key 对象存储 | `ObjectStore` / `mmcc_put(key, …)` | 前缀复用在控制面 radix + 链式 `block_hash`；池按不透明字节 |
| 无 HiRadix / 位置视图一跳 | 公开树无 radix | Router 5ms 预算靠控制面视图，不靠逐 key exist 探测 |

## 2. 分层语义

| 现象 | 证据 | lake |
|------|------|------|
| HBM/DRAM/SSD + LRU/水位 | `MultiLevelElimination`、ssd 文档 | 对齐「多层」；补 LFU-Aging、前缀亲和、`ref>0` 冻结 |
| 无 L3 对象 SSOT / L2=F4 恢复点产品叙事 | README 重在池与 OneCopy | lake 持久语义分层写死（F4 / SSOT） |
| HBM 由 LocalService 贡献 | `MmcBmProxy` MEDIA_HBM | 方向同「HBM 进池」；放置权威与 D-direct 仍按方案 Z |

## 3. 一致性与 HA

| 现象 | 证据 | lake |
|------|------|------|
| Meta 单点或「尽力而为」HA | README + `memcache_metaservice_HA.md` | 位置视图权威在控制面进程内存；etcd 降频 checkpoint（P6） |
| 无强一致「集群内存图」事件模型 | — | 对照 Dynamo KV Events / 我们的视图订阅 |

## 4. 传输可移植性

| 现象 | 证据 | lake |
|------|------|------|
| MemFabric / 昇腾路径一等 | 嵌套 `memfabric_hybrid`；protocol 枚举 | Transfer Bus 抽象多 backend；默认 NVIDIA 路径仍 Mooncake TE |
| 浅克隆不见传输源码 | submodule 未 init | 深研 OneCopy 时再拉 MemFabric；本调研以 MemCache API/元数据为准 |

## 5. 多模型 / 配额

| 现象 | 证据 | lake |
|------|------|------|
| 无一等 `model_id`/`revision` 配额 | 配置偏容量与 protocol | F11：按模型软硬配额、借用、背压 |

## 6. 与 Mooncake / LMCache 分工

| 需求 | Mooncake | LMCache | MemCache | lake 态度 |
|------|----------|---------|----------|-----------|
| RDMA 字节搬迁（NVIDIA） | TE 首选 | 多 backend | MemFabric（Ascend） | A=Mooncake TE |
| 对象 Put/Get 池 | store | L2 adapters | **本仓核心** | 形近；寻址归控制面 |
| 内容寻址 / 跨引擎 | 弱 | chunk hash | 无（调用方 key） | radix + block_hash |
| 昇腾 KV pool 落地 | 有 NPU 变体 | 有 | **vLLM-Ascend 一等 backend** | 多芯片时对照 |

## 7. 建议跟踪

- MemFabric Hybrid 开源深度与非昇腾后端；  
- Meta HA 是否走向强一致日志/etcd；  
- 是否暴露「按前缀/会话」的元数据钩子（便于外置 indexer）；  
- vllm-ascend KV pool 与 MemCache 的 layerwise put/get、SSD 分层 roadmap（上游 RFC）。

**定位一句话**：Ascend 上的 **Mooncake-store 级对象池 + 异构 OneCopy**；lake 存算分离蓝图仍以自研控制面 +（NVIDIA）Mooncake TE 为主，MemCache 作 **异构与多层池化** 对照，不替代 radix 权威。
