# 昇腾数据底座候选盘点(传输 + KV 存储)

> 2026-09-17 建。为 D001 的"数据面传输 / KV 存储后端"两行选型提供候选清单。
> 前两个项目在 lake 侧已有深度调研(链接见各行);后两个是讨论中新出现、**待核实**的情报。

## 候选清单

| 候选 | 是什么 | 可填的位置 | 状态 |
|------|--------|-----------|------|
| **Ascend/memcache**([gitcode](https://gitcode.com/Ascend/memcache)) | 昇腾分布式 KVCache 对象池:MetaService/LocalService 架构,HBM/DRAM/SSD 三层,MemFabric OneCopy 传输 | KV 存储后端 + 传输 | lake 已调研:[`../research/memcache/`](../research/memcache/)(overview / architecture / pain-points);本仓有 submodule `3rdparty/memcache` |
| **UCM**([ModelEngine-Group/unified-cache-management](https://github.com/ModelEngine-Group/unified-cache-management)) | 统一缓存框架:可插拔 KVStore、vLLM connector、稀疏插件、PD-via-pool | KV 存储后端(经 vLLM connector 接入) | lake 已调研:[`../research/ucm/`](../research/ucm/);本仓有 submodule `3rdparty/ucm` |
| **Yuanrong-DataSystem**([atomgit](https://atomgit.com/openeuler/yuanrong-datasystem)) | openEuler 系:以内存为中心的分布式多级缓存(HBM/DRAM/SSD)+ 异构对象抽象 + NPU D2D 直传 + ETCD 节点发现 | KV 存储后端 + D2D 传输 | **新情报,未调研**;讨论中对其能力的描述(多级缓存、D2D、ETCD 发现)需逐条核实源码 |
| **UB / URMA** | 昇腾硬件互联:UB(Unified Bus)超节点内高带宽互联,URMA(Unified Remote Memory Access)异步编程接口;讨论称其支持跨节点物理地址全局编址、Load/Store 内存语义 | 传输层底座(对标 NIXL 的位置) | **能力边界待官方资料核实**;memcache 的 MemFabric OneCopy 是否已覆盖 URMA 语义,查 `3rdparty/memcache` 的 memfabric_hybrid 嵌套 submodule |

## 与 Dynamo 侧的接口位对应

Dynamo(KVCR 时代)有两个可插拔位,候选按位填入:

1. **传输插件位**:NIXL 后端的位置 → 昇腾侧填 UB/URMA 封装,或直接复用 memcache 的 MemFabric。
2. **存储后端位**:KVCR 的二级存储后端(DRAM/SSD/对象存储)→ 昇腾侧填 memcache / UCM / Yuanrong 之一。

## 待办

- [ ] 核实 Yuanrong-DataSystem 的真实能力、license、社区活跃度,决定是否需要引入 `3rdparty/` 做参考。
- [ ] 核实 UB/URMA 的编程接口形态与全局编址能力(官方文档/头文件),判断"内存语义 KV 池化"是否成立。
- [ ] 确认 memcache 的 MemFabric OneCopy 与 URMA 的关系(已封装?还是另一套)。
- [ ] 选型决策后立 D003(数据底座选型)。
