# 昇腾数据底座候选盘点(传输 + KV 存储)

> 2026-09-17 建,2026-09-17 更新(补 Mooncake、调优先级)。为 D001 的"数据面传输 / KV 存储后端"两行选型提供候选清单。
>
> **当前倾向(用户定):在 Mooncake 和 memcache 之间二选一。** Yuanrong 降为低优先。

## 候选清单

| 候选 | 是什么 | 可填的位置 | 状态 |
|------|--------|-----------|------|
| **Mooncake**([kvcache-ai/Mooncake](https://github.com/kvcache-ai/Mooncake)) | 两部分:Transfer Engine(统一传输库,TCP/RDMA/NVLink/昇腾等多协议)+ Mooncake Store(分布式 KV 对象池,把多机 DRAM/NVMe 汇成一个池) | **传输 + KV 存储两个位置都能填** | **首选评估对象**。昇腾支持已落地,证据见下节 |
| **Ascend/memcache**([gitcode](https://gitcode.com/Ascend/memcache)) | 昇腾分布式 KVCache 对象池:MetaService/LocalService 架构,HBM/DRAM/SSD 三层,MemFabric OneCopy 传输 | KV 存储后端 + 传输 | **首选评估对象**。已有调研:[`../research/memcache/`](../research/memcache/);本仓 submodule `3rdparty/memcache` |
| **UCM**([ModelEngine-Group/unified-cache-management](https://github.com/ModelEngine-Group/unified-cache-management)) | 统一缓存框架:可插拔 KVStore、vLLM connector、稀疏插件、PD-via-pool | KV 存储后端(经 vLLM connector 接入) | 备选。已有调研:[`../research/ucm/`](../research/ucm/);本仓 submodule `3rdparty/ucm` |
| **Yuanrong-DataSystem**([atomgit](https://atomgit.com/openeuler/yuanrong-datasystem)) | openEuler 系:以内存为中心的分布式多级缓存(HBM/DRAM/SSD)+ 异构对象抽象 + NPU D2D 直传 + ETCD 节点发现 | KV 存储后端 + D2D 传输 | **低优先,暂缓核实**。讨论中对其能力的描述(多级缓存、D2D、ETCD 发现)均未验证 |
| **UB / URMA** | 昇腾硬件互联:UB(Unified Bus)超节点内高带宽互联,URMA(Unified Remote Memory Access)异步编程接口;讨论称其支持跨节点物理地址全局编址、Load/Store 内存语义 | 传输层的最底层(不是候选产品,是上面各候选的传输介质) | 能力边界待官方资料核实;注意 Mooncake 的昇腾传输已经建在 hixl(CANN 传输库)之上,hixl 与 URMA 的关系一并查 |

## Mooncake 的昇腾支持证据(已核实,源码在 `3rdparty/mooncake`)

1. **Transfer Engine 有昇腾传输实现**:`mooncake-transfer-engine/tent/include/tent/transport/ascend/ascend_direct_transport.h`,基于 CANN 的 hixl 库(`#include <hixl/hixl.h>`);构建脚本在 `scripts/ascend/`(openEuler + CANN 依赖安装)。
2. **vllm-ascend 已集成 Mooncake TE**:2025-08 官方公告,vllm-ascend 用 Mooncake TE 做 KV 注册与 PD 分离传输([vllm-ascend 文档](https://docs.vllm.ai/projects/ascend/en/latest/developer_guide/feature_guide/disaggregated_prefill.html))。
3. **Mooncake Store 是 vllm-ascend 文档里的分布式 KV 池后端**:2025-09 官方公告([kv_pool 文档](https://docs.vllm.ai/projects/ascend/zh-cn/main/user_guide/feature_guide/kv_pool.html))。
4. **NIXL 自己也用 Mooncake TE 当后端**(Mooncake README 列 NIXL 为 TE 用户)。这意味着一条低改造成本路径:**Dynamo 侧不动 NIXL API,让 NIXL 在昇腾上走 Mooncake TE 后端**——NIXL 后端是否已有 Ascend 可用组合,需实测。

## 与 Dynamo 侧的接口位对应

Dynamo(KVCR 时代)有两个可插拔位,候选按位填入:

1. **传输插件位**:NIXL 后端的位置 → 首选路径是 NIXL + Mooncake TE(昇腾传输已由 TE 实现);备选是 memcache 的 MemFabric 封装成 NIXL 后端。
2. **存储后端位**:KVCR 的二级存储后端(DRAM/SSD/对象存储)→ Mooncake Store 或 memcache 二选一(当前倾向)。

## 待办(按优先级)

- [ ] **(高)** Mooncake vs memcache 对比评估:昇腾传输成熟度、KV 池元数据模型、与 KVCR 后端位的契合度、社区活跃度 → 结论立 D003(数据底座选型)。
- [ ] **(高)** 实测 NIXL 的 Mooncake 后端在昇腾是否可用(NIXL API 保留 + TE 提供 Ascend 传输);若可用,传输层改造量大减。
- [ ] **(中)** 确认 memcache 的 MemFabric OneCopy 与 hixl/URMA 的关系(同一套?还是各自封装)。
- [ ] **(中)** 核实 UB/URMA 的编程接口形态与全局编址能力(官方文档/头文件)。
- [ ] **(低)** Yuanrong-DataSystem 能力核实,暂缓。
