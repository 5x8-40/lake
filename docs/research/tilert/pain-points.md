# TileRT — 痛点与 lake 对照

> 调研快照：2026-07-24；submodule `3rdparty/tilert` @ `a8368a6`（v0.1.5）。  
> 总览 [overview.md](overview.md)；PD [pd-vllm.md](pd-vllm.md)。

## 1. 闭源核 vs lake 可演进性

| 现象 | 证据 | lake |
|------|------|------|
| tile runtime / 融合核不可审 | 无 `.cu/.cpp`；`pyproject.toml` 写明 wheel 在私有仓构建；`load_backend` 只加载 `.so` | 计算层以 vLLM/SGLang **可改源码** 为骨架；TileRT 仅作延迟上限与 PD 胶水样板，不进实现依赖 |
| ABI 钉死 | README：B200×8、py312、torch 2.11 cu130 | lake 不把产品绑单一 GPU 世代；SLO 校准（P7）另议 |

## 2. 无「推理系统」级状态面

| 现象 | 证据 | lake |
|------|------|------|
| 无 paged 分配器 / radix / 前缀缓存 | 公开树无 BlockPool/HiRadix；`block_size=128` 是量化粒度 | 状态面归存储池（P4）；计算层不拥有 KV |
| PD 是一次性手递手 | `ReceiveServer` 单槽；`inject_cache` 写入生成器私有张量 | 需要 L0 预放置、D-direct、写回屏障；不是 peer arena |
| 无跨请求复用 | 无内容寻址发布 | F1 前缀复用 + 方案 Z 放置 |

## 3. 吞吐与过载模型

| 现象 | 证据 | lake |
|------|------|------|
| `max_batch_size=1` | `ModelArgs` / `ModelArgsGLM5` | continuous batching 仍以 SGLang/vLLM 为参考 |
| 过载 = 429 | `pd_router.Pool`、decode 锁 | **过载归 gateway**；worker 不上自行丢请求（nonfunctional / slo） |

## 4. PD 专用约束

| 现象 | 证据 | lake |
|------|------|------|
| 仅 producer | connector 空实现 load 侧 | lake 要双向：满块 RegisterBlocks、续推、故障恢复 |
| MTP 强耦合 | prefill 必须 speculative MTP 才能填 draft KV | lake DRAFT 池与 target 同机制；不绑单一引擎 MTP 配置 |
| rank0 独发 | MLA 副本假设 | 多 TP 写回/故障要有明确策略（对照 SGLang MLA 去重痛点） |
| dtype/wire 严格配对 | 握手校验 | 池侧应版本化 layout，但身份仍是 `(model_id, block_hash, …)` |

## 5. 运维与多模型

| 现象 | 证据 | lake |
|------|------|------|
| 一进程一 backend `.so` | `load_backend` 拒第二模型 | 多 `(model_id, revision)` 共存是存储池硬需求（F11） |
| 权重必须 8 shard 转换 | `weight_converter` | 权重缓存层可借鉴「按设备切分」，但不绑 8 卡常量 |

## 6. 与 vLLM / SGLang 分工（避免重复调研）

| 需求 | 首选参考 | TileRT 角色 |
|------|----------|-------------|
| Worker / Scheduler / paged KV | vLLM / SGLang | 不替代 |
| HiCache / 分层 / radix | SGLang | 无对应 |
| Connector 插件形态 | vLLM + **TileRTConnector** | 增强样板（claim / MultiConnector / 双传输） |
| RDMA 传输 | Mooncake / NIXL（经 TileRT transport） | 落地用法参考 |
| 超低延迟核技巧 | TileRT（闭源） | 仅外部对标，不进仓库实现 |

## 7. 建议跟踪（上游）

- TileLang / TileScale 开源进度（README：编译技术外溢）。  
- 是否开放非 B200 / 多 batch / 消费者 connector。  
- `pd_vllm` 是否演进为更通用的「外部 decode」协议（版本化 layout、多槽队列）。

对 lake：**记为「计算加速特种兵 + PD 胶水教科书」，不是存储/控制面蓝图。**
