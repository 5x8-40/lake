# UCM — 痛点与 lake 对照

> 调研快照：2026-07-24；`3rdparty/ucm` @ `37af15e`。  
> [overview.md](overview.md) · [architecture.md](architecture.md)。  
> 对照：LMCache、vLLM `KVConnectorBase_V1`、Mooncake store、lake [`../../architecture/kv-cache-pool.md`](../../architecture/kv-cache-pool.md)。

## 1. 权威归属

| 现象 | 证据 | lake |
|------|------|------|
| KV/调度仍以 vLLM 实例为中心 | connector + 引擎 patch | HBM/位置归存储池；worker 无状态 |
| Store 是外部加速层 | `UcmKVStoreBaseV1` 由 connector 调用 | 池是必经路径，非可选插件 |

## 2. 前缀与索引

| 现象 | 证据 | lake |
|------|------|------|
| 块键跟 vLLM block hash | `lookup(block_ids: List[bytes])` | 链式 `block_hash` + 控制面 radix |
| `lookup_on_prefix` 在 store 侧 | ABC 方法 | 命中应一跳读位置视图，避免热路径反复 probe |

## 3. PD / 执行模式

| 现象 | 证据 | lake |
|------|------|------|
| 文档列三种 PD 拓扑，主推经统一池（非唯一） | `pd-disaggregation` 用户指南 | 同向；另补混部与 **D-direct** |
| 无集群级三模式 Router | toy_proxy + 引擎调度 | `f(请求, 集群状态) → (模式, 节点)`；失败 F4 重选 |

## 4. 分层与冷热

| 现象 | 证据 | lake |
|------|------|------|
| 分层能力分散在各 store/稀疏卸载 | NFS/Mooncake/… + sparse offload | L0–L3 统一由池管；方案 Z 主动预放置 |
| 无 F11 多模型配额/GC 叙事 | 配置偏 connector/store | 软硬配额、借用、背压上送 gateway |

## 5. 工程成本

| 现象 | 证据 | lake |
|------|------|------|
| 随 vLLM 小版本维护大片 patch | `integration/vllm/patch/v0*` | 降低「贴引擎打补丁」依赖；接口稳定在池 client |
| 稀疏/Blend 范围大 | `ucm/sparse/*` | 不进 P0–P4 必做；Could 再评估 |

## 可直接借鉴（短清单）

1. **PD-via-pool** 产品论证（解耦、无状态、异构）— 写进 lake 对外叙事时可对照引用。  
2. **`UcmKVStoreBaseV1` 原语集合** — lookup/prefetch/load/dump 与异步 `Task`。  
3. **工厂注册多后端** — 对照 `UcmConnectorFactoryV1`。  
4. **Layer-wise connector 拆分** — `UCMLayerWiseConnector` 对 P5 流水线。

## 明确不照搬

- 以引擎补丁为中心的演进方式。  
- 把稀疏注意力框架当作存储池必选项。  
- 仅有「池命中」而无「本地 HBM 命中 → D-direct」的选路模型。
