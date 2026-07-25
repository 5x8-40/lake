# Ascend MemCache — 总览

> 源码:`3rdparty/memcache`(submodule,HEAD `14b4e35`,2026-06-02)。上游 [Ascend/memcache](https://github.com/Ascend/memcache)（镜像常在 gitcode.com/Ascend/memcache）。PyPI：`memcache-hybrid`。许可：**Mulan PSL v2**。  
> 传输底座 [MemFabric Hybrid](https://gitcode.com/Ascend/memfabric_hybrid) 为**嵌套 submodule**（`3rdparty/memfabric_hybrid`），默认浅克隆未 init；审计 OneCopy 内部需另行拉取。  
> 分层/淘汰/元数据细节见 [architecture.md](architecture.md)；与 lake / Mooncake / LMCache 对照见 [pain-points.md](pain-points.md)。

## 一句话定位

MemCache 是面向 **LLM / GR 推理** 的**高性能分布式 KVCache 对象存储引擎**（昇腾生态优先）：`MetaService` 管集群池空间与对象元数据，`LocalService` 既贡献 HBM/DRAM（及 SSD）又作为进程内客户端；数据面经 **MemFabric** 做跨机跨介质 OneCopy（RH2D / D2RH 等）。已作为 **vLLM-Ascend KV Pool backend** 对接（集成代码在 vllm-ascend，不在本仓）。

## 与本系统的关系

| MemCache 概念 | 本系统对应 | 关系 |
|---------------|-----------|------|
| MetaService（池分配 + 对象元数据 + LRU） | 存储控制面位置视图 / 配额·GC | **形态相近**（独立元数据面）；语义是 **exact-key 对象**，非 radix 内容寻址 |
| LocalService 贡献 HBM/DRAM | L0/L1 池化载体 | **值得对照**：节点贡献设备/主机内存进池；lake 更彻底（HBM 亦池权威、方案 Z） |
| MemFabric OneCopy（device_rdma/sdma、host_rdma…） | Transfer Bus | **Ascend 路径参考**；NVIDIA 侧仍以 Mooncake TE / NIXL 为主 |
| HBM→DRAM→SSD 多层 + 高低水位淘汰 | L0–L3 冷热 | **分层思想可借鉴**；lake 另有 L3 对象 SSOT、前缀亲和、引用冻结 |
| 多副本 put（≤8） | 池副本策略 | 参考；lake 副本与冷热/配额统一编排 |
| ObjectStore put/get/exist/remove | kv-pool 字节 API | API 形近 Mooncake store；**无** `(model_id, block_hash)` 命名空间与链式前缀 |
| vLLM-Ascend KV pool backend | worker↔池 | 外部集成样板；本仓无 connector 源码 |

**核心结论**：MemCache 是 **昇腾侧「分布式 KV 对象池 + 异构直传」** 的工业参考，与 Mooncake store **同层**（字节/对象池），**不是** HiCache/radix 控制面。lake 借鉴其 **Meta/Local 拆分、多层介质、OneCopy 路径分类**；拒绝其 **应用自管 string key、无内容寻址前缀树、尽力而为 HA** 作为权威模型。

## 设计哲学

- **对象级 KVCache**：调用方提供 key；支持批量与多层 blob（多地址离散 KV block）。
- **池化底座外置**：传输与跨介质拷贝下沉 MemFabric，MemCache 专注元数据、放置、淘汰与 API。
- **软硬件共优化**：A2/A3 上 device_rdma / device_sdma 等路径；鲲鹏 host_urma；同机 host_shm。
- **框架旁路集成**：对 vllm-ascend / sglang / mindie 等以 backend 形式接入，而非自带推理引擎。

## 架构

```
推理进程 (vLLM-Ascend / …)
  └─ LocalService 客户端 (whl/so) ──RPC──► MetaService
         │                              │ 全局分配 / 对象元数据 / LRU·降层
         │ 贡献本地 HBM/DRAM(/SSD)       │
         └──── MemFabric OneCopy ◄──────┘
                 (RH2D / D2RH / H2D / D2H …)
```

| 组件 | 职责 |
|------|------|
| **MetaService** | 独立进程；池空间分配、LocalService 加入/退出、对象元数据、多层淘汰；单点或 K8s HA（多活 + Lease，尽力而为） |
| **LocalService** | 进程内库：客户端 API + 内存提供者（连续区域进全局池，可被他节点按地址访问） |
| **MemFabric Hybrid** | 多级内存与异构网络；OneCopy 跨机跨介质 |

配置模板：`config/mmc-meta.conf`、`config/mmc-local.conf`（键前缀 `ock.mmc.*`）。

## 技术栈

- **语言**：C++ 主体（`src/memcache/csrc/`）+ C API + Python 绑定（`python_wrapper` / `memcache_hybrid`）+ RESTful 管理面。
- **构建**：CMake；文档区分 whl 用户与 `run` 编译安装。
- **嵌套依赖**：`memfabric_hybrid`（传输）、spdlog、httplib、nlohmann/json、prometheus-cpp-lite；测试用 googletest/mockcpp。
- **硬件**：昇腾 A2/A3（及文档中的 A5 路径名）、鲲鹏；非 NVIDIA CUDA 一等路径。

## 代码索引

| 概念 | 文件:符号 |
|------|-----------|
| C++ 对象存储门面 | `src/memcache/include/cpp/mmcache.h`::`ObjectStore` |
| 副本配置 | `mmcache.h`::`ReplicateConfig` |
| C 客户端 put/get | `src/memcache/include/mmc_client.h`::`mmcc_put` / `mmcc_batch_put` |
| Meta 服务入口 | `src/memcache/csrc/meta_service/mmc_meta_service.cpp` |
| Meta 代理 Get/BatchGet | `…/mmc_meta_mgr_proxy.cpp`::`MmcMetaMgrProxy::{Get,BatchGet}` |
| 多层淘汰 | `…/mmc_meta_container_lru.cpp`::`MultiLevelElimination` |
| 本地 BM / 介质指针 | `…/local_service/mmc_bm_proxy.cpp`::`MmcBmProxy`（`MEDIA_HBM`/`MEDIA_DRAM`） |
| LocalService 默认实现 | `…/local_service/mmc_local_service_default.cpp` |
| Python 绑定 | `…/python_wrapper/pymmc.cpp` |
| 配置常量 | `…/config/mmc_config_const.h`（`ock.mmc.local_service.dram.size` 等） |
| SSD 用法 | `doc/memcache_ssd_usage.md` |
| Meta HA | `doc/memcache_metaservice_HA.md` |
| Python/C++ API 文档 | `doc/memcache_python_api.md`、`doc/memcache_c++_api.md` |

## 优势

1. **Meta / Local 清晰拆分** — 元数据面与贡献内存的数据面分离，贴近「控制面 + 池节点」。  
2. **异构 OneCopy** — 强调 RH2D/D2RH，减少主机中转；有 A2/A3 公开性能数字。  
3. **多层介质** — HBM/DRAM/SSD 与高低水位淘汰在产品叙事与代码中均有位置。  
4. **多语言 API** — C++/C/Python/REST，便于框架嵌入。  
5. **已进 vLLM-Ascend KV Pool** — 与 Mooncake/Yuanrong 并列 backend，生态验证路径清楚。

## 劣势

1. **Exact-key，无 radix / 链式内容寻址** — 前缀复用与命中语义在引擎侧，不在 MemCache。  
2. **无 lake 式 `(model_id, revision)` 命名空间与池内配额模型**（公开文档未一等建模）。  
3. **HA 自承尽力而为** — 非 etcd 强一致位置视图。  
4. **昇腾/MemFabric 绑定** — 传输栈不可直接当 NVIDIA 集群默认。  
5. **嵌套 MemFabric 需另拉** — 浅克隆本仓看不到传输实现细节。

## 与本系统的关键对比

| 维度 | MemCache | lake |
|------|----------|------|
| 键模型 | 应用 string key | 内容寻址 `block_hash` + radix |
| 层 | HBM/DRAM/SSD 池 | L0–L3 统一编址 + L3 SSOT |
| 元数据 | MetaService | 控制面内存权威 + etcd checkpoint |
| 传输 | MemFabric（Ascend） | Transfer Bus（Mooncake TE 等） |
| 引擎关系 | KV pool backend | 彻底存算分离；混合执行模式 |

下一篇：[architecture.md](architecture.md)。
