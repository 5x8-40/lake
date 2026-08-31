# Unified Cache Management (UCM) — 总览

> 源码:`3rdparty/ucm`(submodule,HEAD `37af15e`,2026-07-24)。上游 [ModelEngine-Group/unified-cache-management](https://github.com/modelengine-group/unified-cache-management)。PyPI 包名：`uc-manager`。许可：**MIT**（附加条件，见 `LICENSE`）。  
> 文档站点：[ucm.readthedocs.io](https://ucm.readthedocs.io/en/latest)；路线图 [issue #679](https://github.com/ModelEngine-Group/unified-cache-management/issues/679)。  
> 架构/插件面见 [architecture.md](architecture.md)；与 lake 对照见 [pain-points.md](pain-points.md)。

## 一句话定位

UCM（华为 ModelEngine）是挂在 **vLLM / vLLM-Ascend / SGLang / MindIE** 上的**统一缓存框架**：用可插拔 **KVStore** 持久化/复用 KV（前缀缓存），并用 **UcmSparse*** 插件稀疏注意力与卸载。PD 文档同时列出 HBM 直传、经 DRAM 中介与经统一存储池三种拓扑——**统一池是首选而非唯一**；产品叙事主推池中转以换解耦与无状态。三种路径的语义与 lake 对照见 [architecture.md](architecture.md) §4。

## 与本系统的关系

| UCM 概念 | 本系统对应 | 关系 |
|----------|-----------|------|
| `UcmKVStoreBaseV1`（lookup/prefetch/load/dump） | kv-pool 字节 API + Transfer Bus | **API 形态可借鉴**；UCM 键多为 vLLM block hash，lake 另有 radix + `(model_id, block_hash)` |
| `UCMConnector` / `KVConnectorBase_V1` | worker↔存储池 client | **接入样板**（引擎侧插件）；lake 要将 connector 升为必经集群路径 |
| NFS / Mooncake / POSIX / DS3FS… store | L2/L3 后端 | 多后端工厂模式对照 LMCache；生产默认仍可复用 Mooncake TE |
| `UcmSparseBase`（scheduler/worker hooks） | （远期）长上下文稀疏 | **lake 当前非核心**；对照「算法与存储解耦」 |
| PD via unified storage pool | 存算分离 + PD/混部/D-direct | **叙事最接近 lake**：实例无状态、池作中间态；湖仍更彻底（HBM 归池、池放置·调度读视图、三模式选路） |
| Cache Blend / window extrapolate | — | 应用侧能力；非 lake P0–P4 主线 |

**核心结论**：UCM 是 **「vLLM 生态上的 KV 中心化框架」**——前缀缓存 + 多 store + 稀疏插件 + 以池中转做 PD。与 LMCache 同属 **引擎插件层**；与 Mooncake/MemCache 的关系是 **消费其 store/传输**。lake 借鉴其 **store 抽象、connector 集成、PD-via-pool 叙事**；拒绝把 **引擎私有 HBM + 可选 connector** 当作权威模型。

## 设计哲学

- **KVCache-centric**：冗余计算用检索/命中替代；PC 与稀疏共用「block id + offset」寻址思路。
- **算法与存储解耦**：`UcmSparse*` 不绑死某一种后端；`UcmKVStoreBaseV1` 可换 NFS/Mooncake/…。
- **PD 首选经池传输**：在直传 / DRAM 中介 / 统一池三者中主推 P 写池、D 读池（复用 Prefix Cache），换解耦与异构部署；另两种拓扑仍在文档中保留。
- **框架旁路**：以 patch/connector 切入推理引擎，而非自研完整 serving 引擎。

## 架构

```
vLLM / Ascend / SGLang / MindIE
  ├─ UCMConnector (KVConnectorBase_V1) ──► UcmKVStoreBaseV1
  │         │                                ├─ NFSStore / POSIX / …
  │         │                                ├─ Mooncake store connector
  │         │                                └─ DS3FS / pipeline / …
  └─ UcmSparseBase (可选) ── hooks ──► SparseKV 分配 / load·dump
              │
              └─ PD：直传 / DRAM 中介 / 统一池（文档主推后者：P dump → 池 → D load）
```

## 分布式模型

> 跨项目汇总对比见 [../distributed-models.md](../distributed-models.md)。

- **拓扑**：无自有分布式控制面。UCM 是引擎插件框架（connector + store 抽象 + 稀疏插件），集群级语义全部继承自所挂 store 后端：挂 Mooncake store 则拓扑/一致性/HA 是 Mooncake 的（星型 + leader 内存权威），挂 NFS/DS3FS 则为文件系统语义。UCM 自身只有引擎侧的 `UCMConnector` 与 store 侧的 `UcmKVStoreBaseV1` 两点。
- **元数据权威**：无。前缀命中靠引擎 APC/block hash + store `lookup_on_prefix` 探测；无全局位置视图。
- **同步机制**：无自有机制；PD 场景下 P dump → 池 → D lookup/load，同步语义即后端 store 的读写可见性。
- **一致性**：继承后端。多引擎并发写同前缀块的去重/覆盖语义由后端决定，UCM 不介入。
- **HA 与故障**：继承后端；引擎崩溃则引擎侧状态丢失（KV 在后端的留存由后端策略决定）。
- **集群级唯一的设计选择**：PD 三拓扑（HBM 直传 / DRAM 中介 / 统一池，主推池），属传输拓扑选择，不是元数据权威设计（见 [architecture.md](architecture.md) §4）。
- **与 lake 对照**：UCM 的 PD-via-pool 叙事（实例无状态、池作中间态）与 lake 同向，但权威问题交由后端处理；lake 的池自带控制面权威（radix + 位置视图 + 配额/GC），不依赖后端提供这些语义。

## 技术栈

- **语言**：Python（集成/稀疏/工厂）+ C++（`ucm/store/*/cc`、`CCStore`/`StoreV1`）。
- **构建**：CMake + setuptools；`pyproject.toml` 包名 `uc-manager`。
- **对接引擎**：vLLM 0.17（main/develop）；含大量版本化 `integration/vllm/patch/`。
- **生态**：可挂 Mooncake store；另有 Ascend / MindIE 路径。

## 代码索引

| 概念 | 文件:符号 |
|------|-----------|
| Store 抽象 (v1) | `ucm/store/ucmstore_v1.py`::`UcmKVStoreBaseV1` |
| Store 抽象 (旧) | `ucm/store/ucmstore.py`::`UcmKVStoreBase` |
| C++ Store 门面 | `ucm/store/ucmstore_v1.h`::`StoreV1` |
| Store 工厂 | `ucm/store/factory_v1.py`::`UcmConnectorFactoryV1` |
| Mooncake 后端 | `ucm/store/mooncakestore/mooncake_connector.py` |
| NFS 后端 | `ucm/store/nfsstore/nfsstore_connector.py` |
| vLLM 主 connector | `ucm/integration/vllm/ucm_connector.py`::`UCMConnector` |
| Layer-wise / PD connector | 同文件 `UCMLayerWiseConnector` / `UCMPDConnector` |
| 稀疏基类 | `ucm/sparse/base.py`::`UcmSparseBase` |
| PD 设计文档 | `docs/source/user-guide/pd-disaggregation/` |
| 扩展 store | `docs/source/developer-guide/extending_store.md` |

## 优势

1. **PD-via-pool 写进产品文档** — 与 lake「池为中间态、实例无状态」同向，便于对照调度/异常解耦论点。  
2. **Store 可插拔** — 工厂 + `lookup`/`prefetch`/`load`/`dump` 边界清晰。  
3. **稀疏与 PC 同框** — 长上下文算法插件化，存储侧不绑死。  
4. **多引擎补丁成熟度高** — vLLM/Ascend 版本化 patch 路径可作集成成本对照。

## 局限（相对 lake）

1. **仍是引擎插件**：HBM/调度权威在 vLLM；非集群位置视图 + 池放置·调度读视图。  
2. **无独立 radix 控制面**：前缀依赖引擎 APC/block hash + store `lookup_on_prefix`。  
3. **无 D-direct / 三模式 Router**：PD 叙事主推池中转，不覆盖本地命中直跳选路。  
4. **稀疏/Blend 等能力宽** — lake 近期不必照搬，避免范围膨胀。

## 何时查阅本目录

- 设计 worker↔池 client / `KVConnector` 形态时。  
- 讨论「PD 是否必须 HBM 直传 vs 经池」时。  
- 评估多 store 后端工厂、NFS/Mooncake 挂接时。  
- 长上下文稀疏若进入 Could 特性时再深挖 `ucm/sparse/`。
