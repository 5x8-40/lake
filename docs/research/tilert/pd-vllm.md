# TileRT — vLLM Prefill + TileRT Decode（PD）

> 源码：`3rdparty/tilert/tilert/pd_vllm/`（HEAD `a8368a6` / v0.1.5）。  
> 总览见 [overview.md](overview.md)。对照 vLLM connector 契约见 [`../vllm/compute.md`](../vllm/compute.md)；传输对照 [`../mooncake/transfer-engine.md`](../mooncake/transfer-engine.md)。

## 一句话

v0.1.5 把 TileRT 接到 vLLM V1 **`KVConnectorBase_V1`**：stock vLLM 做 prefill（paged KV + 可选 MTP draft 层），经 NIXL/Mooncake **GPU 直传** 到 TileRT decode 单槽，再由 OpenAI 兼容 `pd_router` 出流。不改 vLLM 源码。

## 拓扑

**A — 专用 TileRT decode 池**

```
Client → pd_router:23333
           ├─ HTTP prefill → vLLM:8000 (TileRTConnector, kv_producer)
           └─ ctrl/http → TileRT decode (ReceiveServer + decode_server)
                 ▲ RDMA payload
```

**B — MultiConnector 分流**

同一 prefill 挂 `NixlConnector` + `TileRTConnector`：带 `tilert_host` 的请求进 TileRT；其余走原生 vLLM decode。TileRT 侧 claim 逻辑保证互不抢同一请求。

## 模块地图

| 模块 | 职责 |
|------|------|
| `prefill_connector.py`::`TileRTConnector` | Scheduler/Worker 两侧 producer；claim、攒 block_ids、抽 KV、异步发送 |
| `profiles/{base,mla_nsa,glm5,dsv32}.py` | 模型相关：层数、MLA/NSA 平面、FP8 转换、引擎适配 |
| `transport.py` | `MooncakeTransport` / `NixlTransport` |
| `receive_server.py`::`ReceiveServer` | TCP 控制 + **单一** GPU 接收 arena |
| `decode_server.py` | HTTP：收齐 → `inject_cache` → 生成 → 流式返回 |
| `pd_router.py`::`Pool` | 选空闲 decode；全忙 **429** |
| `wire.py` | 定长控制帧；`NUM_RANKS=8` |
| `engine_iface.py` | decode 引擎协议（含 CPU stub） |

## `TileRTConnector` 行为（相对 vLLM 契约）

参考实现：`tilert/pd_vllm/prefill_connector.py`::`TileRTConnector`；对照 vLLM `KVConnectorBase_V1`。

值得参考：

1. **选择性 claim** — 只认 `kv_transfer_params.tilert_host`，便于 `MultiConnector` 共存。  
2. **攒齐 chunked prefill** — 按 block_id 累积到 prompt 算完再抽送。  
3. **控制面 / 数据面分离** — TCP/HTTP 握手与元数据；KV 字节走 RDMA。  
4. **握手字段** — host/port、transport、`max_seq_len`、wire layout、远端内存区。  
5. **MTP-aware** — prefill 须开 speculative MTP，使 draft 层 KV 一并物化；profile 层数 = 主层 + 1 MTP（DeepSeek 62 / GLM 79）。

关键差异（相对 lake）：

- Connector **只实现 producer**；`start_load_kv` / `wait_for_layer_load` / `save_kv_layer` 为空——单向 P→D，无 decode 回写池。  
- 抽 KV 依赖 **vLLM 实例内 block_id**，不是全局内容寻址 `block_hash`。  
- 接收端是 **单请求 arena**，不是池化 L0 放置视图。  
- lake 目标：引擎经 agent/fence 访问池；**不**把引擎私有 block 表当集群权威。

## KV 抽取与布局

`MlaNsaProfile.extract`（`profiles/mla_nsa.py`）：

- 按 vLLM block 表 gather → 连续 staging（含 MLA latent、RoPE、NSA key-index 等）。  
- `PAGE_SIZE=64` 用于映射 paged 行；与 `ModelArgs.block_size=128`（FP8 量化粒度）不是同一概念。  
- decode 侧 `convert`：FP8 反量化、KI Hadamard 等。  
- **仅 rank 0 发送**（MLA latent 视为副本）——省带宽，也引入单点。

GLM / DeepSeek 各有 `_build_engine` 适配（`profiles/glm5.py`、`dsv32.py`）。

## 传输

| 后端 | 符号 | 要点 |
|------|------|------|
| Mooncake | `transport.py`::`MooncakeTransport` | `TransferEngine`、P2P 握手、批量写 |
| NIXL | `transport.py`::`NixlTransport` | UCX agent、VRAM 注册、轮询完成；多 NIC 需 `UCX_NET_DEVICES` |

两端 `--kv-cache-dtype` 必须一致（例：vLLM `fp8_ds_mla` ↔ TileRT `fp8`），握手校验失败即拒。

## 调度与过载（刻意极简）

- `ModelArgs.max_batch_size = 1`；decode_server 进程锁；ReceiveServer 单 current slot。  
- `pd_router.Pool`：内存 busy 标志；无队列深度信号给上游——满员直接 429。  
- **对照 lake**：过载 shedding 归 gateway；推理侧上报 in-flight / 剩余容量；Router 按位置视图选 PD / 混部 / D-direct。

## 代码索引

| 概念 | 文件:符号 |
|------|-----------|
| Connector | `tilert/pd_vllm/prefill_connector.py`::`TileRTConnector` |
| 抽 KV | `tilert/pd_vllm/profiles/mla_nsa.py`::`MlaNsaProfile` |
| MTP decode 适配 | `tilert/pd_vllm/profiles/mla_nsa.py`::`MlaNsaEngineAdapter` |
| 接收 | `tilert/pd_vllm/receive_server.py`::`ReceiveServer` |
| HTTP decode | `tilert/pd_vllm/decode_server.py`::`build_app` |
| 路由池 | `tilert/pd_vllm/pd_router.py`::`Pool` |
| 灌入生成器 | `tilert/models/*/generator.py`::`inject_cache` |

## 对 lake 的可迁移清单

| 可借鉴 | 不要照搬 |
|--------|----------|
| profile 缝：公共 connector 生命周期 × 模型私有 extract/convert | 引擎私有 block_id 当全球身份 |
| claim 语义支持多 connector 共存 | 单槽 / bs=1 作为架构默认 |
| 握手版本化（layout / max_seq / transport） | 过载只靠小路由器 429 |
| MTP draft 层 KV 一等传 | 单向灌入替代存储池权威 |
| 控制 TCP 与 RDMA 数据面拆分 | rank0 单点发送无故障模型 |

→ [pain-points.md](pain-points.md)
