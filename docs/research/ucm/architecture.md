# UCM — 架构与插件面

> 源码：`3rdparty/ucm` @ `37af15e`。总览 [overview.md](overview.md)。  
> 对照：vLLM connector [`../vllm/compute.md`](../vllm/compute.md)；Mooncake store [`../mooncake/kv-store.md`](../mooncake/kv-store.md)；LMCache [`../lmcache/sharing-and-backends.md`](../lmcache/sharing-and-backends.md)。

## 1. Store 层（`UcmKVStoreBaseV1`）

职责：与外部 KV 持久化/池通信；稀疏算法与 PC 共用。

典型原语（Python ABC）：

| 方法 | 含义 |
|------|------|
| `lookup` / `lookup_on_prefix` | 块是否存在；前缀最长命中下标 |
| `prefetch` | 异步预取到高速缓存 |
| `load` / `dump`（及 batch/task） | 与设备内存之间搬 KV |
| `create` / `wait` / `commit` 等 | 空间与异步任务（版本间略有差异） |

C++ 侧 `StoreV1` / `CCStore` 承载热路径；Python 工厂 `UcmConnectorFactoryV1` 按名懒加载后端。

已见后端目录：`nfsstore`、`mooncakestore`、`posix`、`ds3fs`、`pcstore`、`pipeline`、`compress`、`cache`、`fake`、`empty`。

**对 lake**：字节层多后端可对照；**内容寻址 + radix + 位置视图**仍归控制面/B 复用路径，不在 UCM store 内。

## 2. Connector 层（vLLM）

`ucm/integration/vllm/ucm_connector.py` 实现 `KVConnectorBase_V1`（部分类带 `SupportsHMA`）：

| 类 | 角色（概略） |
|----|----------------|
| `UCMConnector` | 主入口，组合 store + HMA 等 |
| `UCMDirectConnector` | 直接路径基类 |
| `UCMLayerWiseConnector` | layer-wise 传输 |
| `UCMPDConnector` | PD 相关路径 |
| `UCMLiteConnector` / `UCMMockConnector` | 轻量 / 测试 |

另有 `hla_connector.py`、`hma_connector.py`、`blend_connector.py`。版本差异靠 `integration/vllm/patch/v0xxx/` 补丁树维护。

**对 lake**：scheduler/worker 双侧 metadata、layer-wise、PD connector 分裂方式可作 **P5 接入形态**对照；权威仍应是池 + 控制面，而非补丁后的引擎内状态。

## 3. 稀疏框架（`UcmSparseBase`）

Scheduler 侧：槽位估计、分配后状态、请求结束元数据。  
Worker 侧：`execute_*` / `attention_*` hooks，负责检索与 load/dump 稀疏块。

`SparseKVManager`（文档叙事）允许算法自定义块分配；与 store 通过 id/offset 解耦。子树含 `esa`、`gsa`、`kvstar`、`blend`、`rerope` 等。

**对 lake**：接口分 scheduler/worker 的做法与 vLLM connector 对称；**近期不必实现稀疏**，只记「算法插件 ≠ 存储权威」。

## 4. PD 分离：三种 P↔D KV 传输拓扑

上游用户指南（[PD Disaggregation](https://ucm.readthedocs.io/en/latest/user-guide/pd-disaggregation/)，源码树 `docs/source/user-guide/pd-disaggregation/`）把 **Prefill 节点 → Decode 节点怎么搬 KV** 分成三种拓扑。**统一池是首选而非唯一**——勿误读为 UCM 只支持经池 PD。

| 拓扑 | 路径（语义） | 特点（UCM 叙事） |
|------|--------------|------------------|
| **HBM 直传** | P 的 HBM ──高速互联 / 直通协议──→ D 的 HBM | 路径最短、效率高；适合 1P1D、同构 P/D。调度常需请求一开始就绑死 P/D，以便 prefill 阶段做 layer-wise 边算边传。大规模集群要全连接或再分组，扩缩与组网更重。 |
| **DRAM 中介** | P HBM → P DRAM →（网络）→ D DRAM → D HBM | DRAM 作逻辑中转；HBM 只在最短必要窗口被占，减轻 HBM 压力，调度更灵活。代价是多跳拷贝，延迟通常高于直传。 |
| **统一存储池**（主推） | P dump → 统一池（复用 Prefix Cache）→ D lookup/load | 逻辑最简、解耦最强：P/D 角色可弱化甚至不必严格区分；实例无状态；异构卡/精度更易；大规模不必 P–D 全连接。UCM 选此为默认叙事。 |

主推池的论据（上游原文要点）：P/D 完全解耦 → 调度与异常处理变简单；复用 Prefix Cache 代码路径、少写一套 PD 专用异常；存储作实例状态 → 实例可无状态；异构近零额外成本；大集群比「直传全连接」更易扩。示例代理：`ucm/pd/toy_proxy_server.py`。

```
HBM 直传:     P[HBM] ──────────── RDMA/直通 ────────────► D[HBM]

DRAM 中介:    P[HBM] → P[DRAM] ── 网络 ──► D[DRAM] → D[HBM]

统一池:       P[HBM] ──dump──► 统一存储池 ──lookup/load──► D[HBM]
                              （Prefix Cache 命中路径）
```

### 与 lake 对照（勿混为一谈）

UCM 的三分法是**产品级传输拓扑选项**；lake 在 PD 分离路径上按时序做 **「L0→L0 直传 vs 经池中转」** 路由（见 [`../../architecture/kv-cache-pool.md`](../../architecture/kv-cache-pool.md)「跨实例 KV 传输」），外加执行模式上的混部 / **D-direct**。

| UCM 拓扑 | lake 大致对应 | 说明 |
|----------|---------------|------|
| HBM 直传 | PD 时序重叠时的 **L0→L0 直传** | 都是 GPU HBM 间最短路径；lake 源/目 slot 归池、in-flight 冻结，不是引擎私有 peer 手递手。 |
| DRAM 中介 | **无单独一等拓扑名** | 若 RDMA 不能直读 HBM，传输引擎可能经 pinned host（L1）bounce——属实现退化，不是对外第三种产品模式。 |
| 统一存储池 | PD 的 **经池中转**（源 = L1/L2 池段） | 叙事同向；lake 另有 L3 SSOT、写回屏障、F4 从 L2 续推。 |
| （UCM 无） | **D-direct** | 前缀已在目标节点 HBM → 零/极小传输直跳；UCM 文档三种拓扑都不覆盖「本地命中免传」。 |

**对 lake 的其余差异**：

- UCM 仍嵌在引擎 + connector；池命中 ≠ 本地 HBM 命中 → **无 D-direct 一等公民**。  
- lake Router 在 PD / 混部 / D-direct 间按位置视图选路；失败走 F4 重跑纯函数，不设 mode 阶梯。

## 5. 集成面

| 路径 | 内容 |
|------|------|
| `ucm/integration/vllm/` | 主战场：connector + 版本 patch |
| `ucm/integration/sglang/` | SGLang 对接 |
| `ucm/integration/mindie/` | MindIE |
| Ascend | 文档 quickstart_vllm_ascend；patch 树含 `vllm_ascend` |

## 代码索引（补充）

| 概念 | 文件:符号 |
|------|-----------|
| Store 工厂注册 | `factory_v1.py`::`UcmConnectorFactoryV1.register_connector` |
| 前缀 lookup | `ucmstore_v1.py`::`lookup_on_prefix` |
| 稀疏角色枚举 | `sparse/base.py`::`UcmSparseRole` |
| PD toy 代理 | `pd/toy_proxy_server.py` |
