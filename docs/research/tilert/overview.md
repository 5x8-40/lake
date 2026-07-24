# TileRT — 总览

> 源码:`3rdparty/tilert`(submodule,HEAD `a8368a6`, tag 对齐 v0.1.5,2026-07-14)。PyPI 包名 `tilert`；仓库 [tile-ai/TileRT](https://github.com/tile-ai/TileRT)。  
> 本仓库是**公开呈现副本**：原生 tile runtime / 融合 CUDA 在私有构建仓打成 `libtilert_*.so` wheel，本树以 Python API + PD 胶水为主。  
> PD 分离与 vLLM connector 细节见 [pd-vllm.md](pd-vllm.md)；局限与 lake 对照见 [pain-points.md](pain-points.md)。

## 一句话定位

TileRT 是**超低延迟导向的 tile-level LLM 推理 runtime**：把算子拆成细粒度 tile 任务，在多卡上动态重叠计算 / I/O / 通信，优先压 **TPOT（单请求响应）**，而非高吞吐 continuous batching。公开产品形态是「8×B200 上跑 DeepSeek-V3.2 / GLM-5」的预编译引擎 + MTP，以及 **vLLM prefill → TileRT decode** 的 PD 插件路径。

## 与本系统的关系

lake 计算层选型 = **Python + Triton**；控制面 / 存储池做彻底存算分离。TileRT **不是**又一个可替换的全栈引擎参考（核与调度闭源），而是：

| TileRT 概念 | 本系统对应 | 关系 |
|-------------|-----------|------|
| 超低延迟 decode（闭源 tile runtime） | 计算层 Prefill/Decode worker | **性能上限与核编排参考**；实现不可 fork，只能对照 SLO（TTL/ITL）与共置/分离取舍 |
| `TileRTConnector`（vLLM `KVConnectorBase_V1`） | worker ↔ 存储池 / Transfer Bus | **直接参考 PD 胶水形态**：claim 请求、握手字段、profile 抽 KV、RDMA 旁路控制面 |
| Mooncake / NIXL transport | Transfer Bus | 与既有 Mooncake 调研叠加；TileRT 证明 **同一 connector 可插两套传输** |
| `inject_cache` 一次性灌入 decode HBM | 存储池放置 + agent fence | **形态相反**：TileRT 是引擎私有槽；我们是池权威 + 多模式（含 D-direct） |
| MTP（`with_mtp` / draft 层 KV） | 投机解码 + `pool_kind=DRAFT` | **PD 必须带 draft 层 KV** 的约束可借鉴；编排仍以 SGLang/vLLM 为主参照 |
| `pd_router` 忙闲 + 429 | gateway 过载 / Router 选路 | **职责对照**：TileRT 把准入做进小路由器；lake 过载归 gateway，模式选择归 Router |

**核心结论**：TileRT 是**专用超低延迟 decode 引擎 + 成熟的 vLLM PD 插件样板**；**不是**分层 KV 池 / radix / 集群调度参考。lake 借鉴其 **connector 剖面、传输握手、MTP-aware 传 KV**，拒绝其「单请求单槽、引擎私有 HBM、无池」模型。

## 设计哲学

- **Latency-first**：面向交易 / agent / 交互对齐场景；README 明确对标高吞吐 batch 系统。
- **Compiler-driven tiles**：算子 → tile 任务 → 跨设备重叠调度（细节在闭源 `.so`；公开路线图称将沉淀到 TileLang / TileScale）。
- **模型–系统共设计**：权重按 8 卡切分（`*_dev_{0..7}`）、DSA/MLA/NSA 布局写死、FP8 KV 与量化 block 对齐。
- **PD 借力 vLLM**：prefill / continuous batch / paged KV 仍交给 vLLM；TileRT 只吃「已算完的注意力状态」做低延迟 decode。

## 架构（公开可见部分）

```
CLI / API
  tilert.generate / DSAv32Generator | GLM5Generator
      → tilert.load_backend(model)     # 互斥加载 libtilert_dsv32.so | libtilert_glm5.so
      → ShowHandsDSALayer / MTP        # Python 编排 + torch.ops.tilert.* 原生核
      → inject_cache / generate        # 可选：外部灌 KV 后自回归 / MTP

PD 拓扑（v0.1.5）
  Client → pd_router (OpenAI)
              → vLLM prefill (TileRTConnector, kv_producer)
              → RDMA (NIXL | Mooncake) → TileRT ReceiveServer (单槽)
              → decode_server → Generator.inject_cache → stream tokens
```

核心组件（开源树内）：

| 组件 | 文件 | 职责 |
|------|------|------|
| `load_backend` | `tilert/__init__.py` | ctypes + `torch.ops.load_library` 加载单模型 `.so` |
| `get_generator` | `tilert/generate.py` | CLI 工厂：模型 / MTP / 权重路径 |
| `DSAv32Generator` / `GLM5Generator` | `tilert/models/*/generator.py` | 加载权重、生成、`inject_cache` |
| `ShowHandsDSALayer` | `tilert/models/*/modules/end2end.py` | 图/端到端编排入口（含 MTP 开关） |
| `TileRTConnector` | `tilert/pd_vllm/prefill_connector.py` | vLLM V1 producer connector |
| `MlaNsaProfile` | `tilert/pd_vllm/profiles/mla_nsa.py` | 从 vLLM paged KV 抽平面布局 |
| `ReceiveServer` / `decode_server` | `tilert/pd_vllm/receive_server.py`、`decode_server.py` | 单槽 RDMA 收包 + HTTP decode |
| `MooncakeTransport` / `NixlTransport` | `tilert/pd_vllm/transport.py` | GPU 直传适配 |
| `Pool`（router） | `tilert/pd_vllm/pd_router.py` | 进程内忙闲，满则 429 |

## 技术栈

- **公开语言**：Python（API / 权重转换 / PD 胶水）。**性能关键路径在闭源 `.so`**（CUDA/C++ tile runtime），经 `torch.ops.tilert.*` 调用。
- **硬件/ABI 钉死（README hard requirements）**：8× NVIDIA **B200**；Python **3.12**；`torch==2.11.0+cu130`；CUDA 13.x runtime；manylinux_2_28。
- **依赖**：transformers/tokenizers 钉版本；PD 路径另需可装 NIXL/Mooncake 与带 V1 disagg 的 vLLM。
- **构建**：本 git 树**故意无** `[build-system]`——`pip wheel .` 打不出 runtime；运行靠 PyPI/GHCR wheel + Docker。

## 代码索引

> 符号名稳定锚定；行号会漂移——`grep -n "符号名" 3rdparty/tilert/<路径>`。

| 概念 | 文件:符号 |
|------|-----------|
| 后端加载（互斥） | `tilert/__init__.py`::`load_backend` |
| CLI 工厂 | `tilert/generate.py`::`get_generator` |
| DeepSeek 生成 / 灌 KV | `tilert/models/deepseek_v3_2/generator.py`::`DSAv32Generator` |
| GLM-5 生成 | `tilert/models/glm_5/generator.py`::`GLM5Generator` |
| 端到端层 + MTP 挂载 | `tilert/models/deepseek_v3_2/modules/end2end.py`::`ShowHandsDSALayer` |
| MTP 模块 | `tilert/models/deepseek_v3_2/modules/mtp.py`::`MTP` |
| vLLM producer connector | `tilert/pd_vllm/prefill_connector.py`::`TileRTConnector` |
| paged→平面抽 KV | `tilert/pd_vllm/profiles/mla_nsa.py`::`MlaNsaProfile` |
| 单槽接收 | `tilert/pd_vllm/receive_server.py`::`ReceiveServer` |
| 传输 | `tilert/pd_vllm/transport.py`::`MooncakeTransport` / `NixlTransport` |
| 权重切分 | `tilert/models/preprocess/weight_converter`（`python -m`） |
| 设计/用法 | 仓库根 `README.md`；PD 博文 [tilert-vllm-disaggregation](https://www.tilert.ai/blog/tilert-vllm-disaggregation.html) |

## 优势

1. **极端 TPOT**：公开 benchmark / 生产（Z.ai GLM-5.1-highspeed、小米 MiMo 共设计）证明低延迟路线可行。  
2. **PD 插件干净**：不 fork vLLM；`kv_connector_module_path` 热加载；可与 `MultiConnector` 共存（按 `tilert_host` claim）。  
3. **传输可插拔**：NIXL / Mooncake 同一控制协议下切换。  
4. **MTP 一等公民**：生成与 PD 传 KV 都显式带 draft 层。  
5. **模型共设计**：权重布局与 DSA/MLA 拓扑对齐，减少通用引擎抽象税。

## 劣势

> 机制层摘要。组合痛点与 lake 对照见 [pain-points.md](pain-points.md)。

1. **核心闭源** — tile 调度 / 融合核不可审、不可改、不可 re-vendor 进 lake。  
2. **硬件与模型钉死** — 8×B200 + 两家前沿模型；非通用计算层。  
3. **`max_batch_size=1` / 单槽 PD** — 无 continuous batching；过载即 429。  
4. **无 radix / 无分层池 / 无跨请求前缀复用** — KV 生命周期止于「一次 P→D 灌入」。  
5. **进程互斥后端** — DeepSeek 与 GLM 的 `.so` 不能同解释器共存。  
6. **控制面极薄** — router 忙闲在进程内存，无集群位置视图。

## 与本系统的关键对比

| 维度 | TileRT | lake |
|------|--------|------|
| 优化目标 | 单请求 TPOT | TTFT/ITL + 集群复用 + 故障续推 |
| KV 权威 | decode 引擎私有 HBM | 存储池 L0–L3 |
| 前缀复用 | 无（公开树内） | radix + 位置视图 |
| PD | vLLM→TileRT 单向灌入 | 混合执行（PD / 混部 / D-direct） |
| 过载 | 小路由器 429 | gateway；推理系统上报容量 |
| 可演进性 | wheel ABI 钉死 | 三语言自研 + 可替换 worker |

下一篇：[pd-vllm.md](pd-vllm.md)（connector / 传输 / 拓扑）。
