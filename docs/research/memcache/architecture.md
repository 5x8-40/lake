# Ascend MemCache — 架构与数据面

> 源码：`3rdparty/memcache` @ `14b4e35`。总览 [overview.md](overview.md)。  
> 对照：Mooncake store [`../mooncake/kv-store.md`](../mooncake/kv-store.md)；LMCache [`../lmcache/sharing-and-backends.md`](../lmcache/sharing-and-backends.md)。

## MetaService

职责（README + `mmc_meta_service.cpp`）：

- 集群**内存池空间**分配与回收；
- **LocalService** 加入 / 退出；
- **对象元数据**（key → 副本位置、介质类型、GVA 等）；
- 触发 / 协调**多层淘汰**（与 `MmcMetaContainerLRU` / `MultiLevelElimination` 配合）。

部署：

| 模式 | 说明 |
|------|------|
| 单点 | 单 Meta 进程；简单；进程挂则服务停 |
| HA | K8s ClusterIP + Lease 多活；元数据恢复；文档称**尽力而为**（`doc/memcache_metaservice_HA.md`） |

启动：Python API 或二进制（`doc/install_whl.md` / `install_run.md`）。

## LocalService

双重角色：

1. **客户端**：whl/so 链进推理进程，调 `ObjectStore` / `mmcc_*`；
2. **内存提供者**：贡献连续 HBM/DRAM（及配置下的 SSD 路径），他节点可经 MemFabric **按地址**访问。

`MmcBmProxy`（`mmc_bm_proxy.cpp`）按 `MEDIA_HBM` / `MEDIA_DRAM` 取 MemFabric BM 指针与本地容量，并选择 H2G/G2H/L2G 等 copy 类型——即「本地介质 ↔ 全局可见地址」的粘合层。

扩缩：支持 LocalService 动态加入/移除（README）；配置含 `world_size`、protocol、每卡 dram/hbm size（`mmc_config_const.h` / `doc/memcache_config.md`）。

## 对象 API 形态

`ObjectStore`（`include/cpp/mmcache.h`）：

- 生命周期：`CreateObjectStore` → `Setup` → `Init(deviceId, initBm)` → `TearDown`；
- 零拷贝：`RegisterBuffer` / `UnRegisterBuffer`；
- 读写：`GetInto` / 批量变体；put 侧配合 `ReplicateConfig`（`replicaNum`≤8、可选 preferred LocalService）；
- 对象可含**多个 blob**（`blobNum_`、每 blob 的 `loc_` / `type_` / `gva_`）——适配「一层一地址」的离散 KV 布局（README 性能节 DeepSeek-R1 块：多段离散地址）。

C API：`mmc_client.h`::`mmcc_put` / `mmcc_batch_put` 等。  
Python：`pymmc.cpp` + `doc/memcache_python_api.md`。  
管理：`doc/memcache_restful_api.md`。

**与 lake**：形近「dumb 字节 put/get」；lake 在控制面另建 radix / `RegisterBlocks`，池不解释张量布局。

## 分层与淘汰

- 介质：HBM、DRAM；SSD 见 `doc/memcache_ssd_usage.md`（本地持久层用法）。
- 元数据容器：`mmc_meta_container_lru.cpp` 实现 LRU，并暴露 `MultiLevelElimination(high, low, …)`——高低水位驱动跨层淘汰/交换（与 README「多层缓存池、淘汰和预取」一致）。
- **对照 lake**：我们冷热 = 引用冻结 + LFU-Aging + 前缀亲和；L2=F4 恢复点、L3=SSOT；MemCache 未见公开 radix/前缀保护一等模型。

## 传输（MemFabric）

本仓**不内嵌**传输实现细节（嵌套 submodule 未默认检出）。产品文档声称路径包括：

| 协议（配置名） | 场景（文档） |
|----------------|--------------|
| `device_rdma` | A2/A3，设备 RoCE |
| `device_sdma` | A3 HCCS |
| `host_rdma` | A2/A3 主机 RDMA |
| `device_urma` / `device_uboe` | A5（文档路线） |
| `host_urma` | 鲲鹏 K5 |
| `host_shm` | 同节点共享内存 |

能力口号：**RH2D / D2RH** OneCopy（远端主机/设备内存 ↔ 本地设备），相对「先落本机再拷」减跳数。

**对照 lake**：NVIDIA 集群数据面优先 Mooncake TE；MemCache 证明「元数据服务 + 贡献内存节点 + 异构直传」在 Ascend 上可量产。若未来多芯片，Transfer Bus 可抽象多 backend，MemFabric 为候选之一而非唯一。

## 与 vLLM-Ascend 的边界

- README 指向 vllm-ascend `kv_pool` 文档：backend 枚举含 `mooncake` / `memcache` / `yuanrong`。  
- **本 submodule 无** `KVConnector` / AscendStoreConnector 源码；集成与配置键（`ock.mmc.meta_service_url` 等）在 vllm-ascend。  
- lake 若对照「引擎侧 KV pool worker」，应同时打开 vllm-ascend 文档，而不是只读本仓。

## 代码索引（架构向）

| 概念 | 文件:符号 |
|------|-----------|
| ObjectStore | `include/cpp/mmcache.h`::`ObjectStore` |
| Meta 服务 | `csrc/meta_service/mmc_meta_service.cpp` |
| Meta Get | `csrc/meta_service/mmc_meta_mgr_proxy.cpp`::`MmcMetaMgrProxy::Get` |
| 多层淘汰 | `csrc/meta_service/mmc_meta_container_lru.cpp`::`MultiLevelElimination` |
| BM/介质 | `csrc/local_service/mmc_bm_proxy.cpp`::`MmcBmProxy` |
| Local 默认 | `csrc/local_service/mmc_local_service_default.cpp` |
| 配置键 | `csrc/config/mmc_config_const.h` |

→ [pain-points.md](pain-points.md)
