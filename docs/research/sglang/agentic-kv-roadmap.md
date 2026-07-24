# SGLang #21846 — 子方案逐项调研

> **上游**：[sgl-project/sglang#21846](https://github.com/sgl-project/sglang/issues/21846) `[Roadmap]: SGLang Distributed KVCache System For Agentic Workload`。  
> **调研快照**：2026-07-24（二次深挖）· submodule `3rdparty/sglang` @ `37f94cb7a0`。  
> **机制基线**：[overview.md](overview.md) · [hicache.md](hicache.md) · [pain-points.md](pain-points.md)。  
> **范围**：issue 正文勾选的**每一个子项**各一节（含无独立 PR 链接的条目）；状态分三列——roadmap 勾选 / GitHub / 本仓 submodule。

## 0. 总览

动机：agentic 负载下 KV 存储与传输暴涨；PD + HiCache 撞墙；hybrid 兼容不足。目标：PD 增量传输 + HiCache/HiSparse + PP + Eagle 端到端，在 DeepSeek DSA（V3.2/GLM5）与 Linear（Qwen3.5）上规模验证。

设计哲学（跨子项共性）：

1. **L1/L2 仍实例私有**，跨机靠 L3 + 直传 + 路由，不做全局 HBM 池。  
2. **编排知意图、引擎管内存**（hint 可拒绝）。  
3. **Hybrid 统一树**（FULL/SWA/Mamba → `UnifiedRadixCache`）。  
4. **组合正确性**（PP×L3、MTP×HiCache、异构 TP）是主债。

对 lake 总读数见 [§Z](#z-对-lake-总对照)。

---

## 1. 完整清单（与 issue 勾选 1:1）

| ID | 子项 | roadmap | GitHub | submodule@37f94cb | 专节 |
|----|------|---------|--------|-------------------|------|
| Q3-A1 | Session-aware RadixTree/HiCache | ☐ | [#29173](https://github.com/sgl-project/sglang/pull/29173) open | partial（legacy session mixin；非 #29173 驱逐） | [§2.1](#21-session-aware-radixtree--hicache-29173) |
| Q3-A2 | KV orchestrator + PREFETCH/DEMOTE/PIN | ☐ | [#25760](https://github.com/sgl-project/sglang/issues/25760) + [#27574](https://github.com/sgl-project/sglang/issues/27574) | absent（无 KvHint） | [§2.2](#22-kv-orchestrator--prefetchdemotepin) |
| Q3-A3 | Direct L3 cache mode | ☐ | [#20535](https://github.com/sgl-project/sglang/pull/20535) open（实质载体） | absent（无 `buffer_only` 旗标） | [§2.3](#23-direct-l3-cache-mode--buffer_only-20535) |
| Q3-A4 | CPU-only KV simulator | ☐ | [#21891](https://github.com/sgl-project/sglang/issues/21891) | partial（schedule sim ≠ 全量） | [§2.4](#24-cpu-only-kv-cache-simulator-21891) |
| Q3-A5 | Sequence Split | ☐ | [#30501](https://github.com/sgl-project/sglang/pull/30501) open | partial（CP layer-split 有；整方案未入树） | [§2.5](#25-kvcache-sequence-split-30501) |
| Q3-U1 | TreeCore → Rust | ☐ | [#29901](https://github.com/sgl-project/sglang/issues/29901) | **absent** | [§3.1](#31-treecore-解耦--rust-29901) |
| Q3-U2 | Mamba offload 产线（Qwen/Kimi） | ☐ | 无独立 PR | partial（控制器有；产线缺口） | [§3.2](#32-mamba-offloading-生产化-qwen--kimi) |
| Q3-U3 | KVCache-Canary | ☐ | 无独立 issue | **present**（`kv_canary/`）未与树强制集成 | [§3.3](#33-kvcache-canary) |
| Q3-P1 | L2 DecodeRadixTree | ☐ | 无独立 PR；见 #28874 | partial（decode HiCache mixin） | [§4.1](#41-l2-decoderadixtree) |
| Q3-P2 | Prefill-as-a-Service PoC | ☐ | 无独立 PR | **absent** | [§4.2](#42-prefill-as-a-service-poc) |
| Q3-H1 | HiSparse 生产就绪 | ☐ | [#28874](https://github.com/sgl-project/sglang/issues/28874) | present 路径 / 生产 checklist open | [§5](#5-hisparse-生产就绪-28874) |
| Q2-H1 | HybridCacheController + Linear | ☑ | [#20457](https://github.com/sgl-project/sglang/pull/20457) merged | present | [§6.1](#61-hybrid-cache-controller--linear-20457) |
| Q2-H2 | Mooncake × hybrid | ☑ | [#21259](https://github.com/sgl-project/sglang/pull/21259) merged | present | [§6.2](#62-mooncake-backend--hybrid-21259) |
| Q2-H3 | MLA+Mamba KeLing | ☐ | [#22957](https://github.com/sgl-project/sglang/pull/22957) open | open | [§6.3](#63-mla--mamba-hybrid-keling-22957) |
| Q2-H4 | 3FS × hybrid | ☑ | [#23241](https://github.com/sgl-project/sglang/pull/23241) merged | present | [§6.4](#64-3fs-backend--hybrid-23241) |
| Q2-H5 | Unified Hybrid Radix | ☑ | [#20415](https://github.com/sgl-project/sglang/issues/20415) | present（主路径） | [§6.5](#65-unified-hybrid-radix-cache-20415) |
| Q2-H6 | DeepSeek V4 HiCache | ☑ | [#24691](https://github.com/sgl-project/sglang/pull/24691) merged | present | [§6.6](#66-deepseek-v4-hicache-24691) |
| Q2-H7 | MiMo-V2 HiCache | ☑ | [#27378](https://github.com/sgl-project/sglang/pull/27378) merged | present | [§6.7](#67-mimo-v2-hicache-27378) |
| Q2-H8 | MLA host TP 去重 | ☐ | [#26691](https://github.com/sgl-project/sglang/pull/26691) open | **absent**（方案未入树） | [§6.8](#68-mla-host-kv-跨-tp-去重-26691) |
| Q2-D1 | Decode Side RadixTree | ☑ | [#19746](https://github.com/sgl-project/sglang/pull/19746) 等 | present（非 hybrid） | [§7.1](#71-decode-side-radixtree) |
| Q2-D2 | Decode HybridRadix SWA/Mamba | ☐ | [#27770](https://github.com/sgl-project/sglang/pull/27770) open | partial（仍拒 hybrid） | [§7.2](#72-decode-side-hybridradixtree-swamamba-27770) |
| Q2-D3 | Storage Prefetch Interface | ☑ | （勾选无独立 PR） | present | [§7.3](#73-storage-prefetch-interface-decode) |
| Q2-D4 | L2 RadixTree 提命中 | ☐ | 无独立 PR | partial（host 节点已有） | [§7.4](#74-l2-radixtree-提命中率) |
| Q2-D5 | Storage groups | ☐ | Mooncake [#1887](https://github.com/kvcache-ai/Mooncake/issues/1887)/[#2127](https://github.com/kvcache-ai/Mooncake/issues/2127) | present（mooncake_store group） | [§7.5](#75-storage-group-语义) |
| Q2-D6 | 异构 TP × PD+HiCache | ☐ | 无独立 PR；见 #21703 | partial（hetero staging ≠×HiCache） | [§7.6](#76-异构-tp--pd--hicache) |
| Q2-P1 | Incremental KV Transfer | ☑ | （协议扩展） | present | [§8.1](#81-incremental-kv-transfer) |
| Q2-P2 | PD Host TransferMode | ☑ | [#21591](https://github.com/sgl-project/sglang/pull/21591) merged | present | [§8.2](#82-pd-host-transfermode-21591) |
| Q2-P3 | Large decode batches | ☐ | 无独立 PR | present（mixin 状态机）/ 产品化 open | [§8.3](#83-large-decode-batches) |
| Q2-G1 | Agent-Aware Phase 1 | ☐ | [#24656](https://github.com/sgl-project/sglang/issues/24656) | absent | [§9.1](#91-agent-aware-kv-phase-1-24656) |
| Q2-G2 | Programmatic KV hints | ☐ | [#27574](https://github.com/sgl-project/sglang/issues/27574) | absent | [§9.2](#92-programmatic-kv-cache-hints-27574) |
| Q2-C1 | PP × HiCache | ☐ | [#22607](https://github.com/sgl-project/sglang/issues/22607) | partial | [§10.1](#101-pp--hicache-22607) |
| Q2-C2 | MTP × HiCache | ☐ | [#21125](https://github.com/sgl-project/sglang/pull/21125) merged · [#30393](https://github.com/sgl-project/sglang/pull/30393) | partial | [§10.2](#102-mtp--hicache-21125--30393) |
| Q2-C3 | EP & DP × HiCache | ☑ | （勾选） | present（组合仍脆） | [§10.3](#103-ep--dp--hicache) |
| Q2-C4 | CP × HiCache | ☑ | [#20460](https://github.com/sgl-project/sglang/pull/20460) | present | [§10.4](#104-cp--hicache-20460) |
| Q2-C5 | CP Layer-Wise Split × HiCache | ☑ | [#29421](https://github.com/sgl-project/sglang/pull/29421) | present | [§10.5](#105-cp-layer-wise-split--hicache-29421) |

---

## 2. Q3 — Agentic KV Cache Scheduling

### 2.1 Session-aware RadixTree / HiCache (#29173)

**问题**：多轮 agent / RL rollout 下，标准 radix 只按 LRU/LFU 驱逐，可能干掉**仍被活跃 session 引用**的前缀，逼重算。

**方案**（PR 正文）：

- 请求带顶层稳定 `session_id`；请求结束 → `register_session_ref`；`/close_session` → `release_radix_session`。  
- 仅 **`UnifiedRadixCache`**：FULL / SWA / Mamba 各记 session 可复用区。  
- 驱逐排序：`UNREFERENCED → REFERENCED`（软保护，非硬 pin；空间不够仍可驱逐 referenced）。  
- session generation + 关闭 tombstone，防 close/reopen 后陈旧 in-flight 再挂引用。  
- **不含 L3**。

**状态**：roadmap ☐ · PR open · submodule：仅有 legacy `SessionRadixCacheMixin`（普通 `RadixCache`），**无** `SessionUnifiedRadixCacheMixin` / session-aware 驱逐排序。

**锚点**：`session_radix_cache.py::SessionRadixCacheMixin` · `release_radix_session` · `--enable-session-radix-cache`。

**对 lake**：对齐「`ref>0` 冻结 + 前缀亲和」；lake 引用权威在池，不绑引擎 session API。

### 2.2 KV orchestrator + PREFETCH/DEMOTE/PIN

**问题**：引擎只见 block hash/refcount，看不见 tool gap / subagent / 跨 worker 共享意图。

**方案**：

- [#25760](https://github.com/sgl-project/sglang/issues/25760) SessionAware Router：bucket 分发 → `sticky→cache_aware→load` → 接 agent hint（Step 0–1 已勾；Step 2–5 未完）。  
- [#27574](https://github.com/sgl-project/sglang/issues/27574) soft hint：`SHARE` / `PREFETCH` / `DEMOTE` / `PIN`；编排出策略，引擎可 clip/defer/reject；L3（Mooncake）作共享 retention。  
- Phase：session 打标 → `KvHintEnvelope` → Pin→L3 lease POC → 再生产化 Prefetch/Demote。

**状态**：roadmap ☐ · RFC open · submodule：**无** `KvHint` / `agent_hints` 字段。

**对 lake**：原则同「gateway 可有意图、池/引擎执行」；lake 放置权威更硬（方案 Z），不靠 soft pin 撑全局共享。

### 2.3 Direct L3 cache mode / `buffer_only` (#20535)

**问题**：每 worker 独占巨大 L2，热前缀在各机 DRAM 重复；Mooncake 池反而偏小。

**方案**（#20535，issue 称 Direct L3）：

- `--hicache-host-memory-mode buffer_only`：L2 缩成**瞬态 staging**（`--hicache-buffer-pages`），写完 L3 即释放页。  
- `TreeNode.storage_backed` / `storage_ready`：GPU 可驱逐但树节点仍因 L3 耐久存活。  
- 小 buffer 满时 pending write 队列背压。  
- 实测 toolagent：命中率↑、更多走 storage hit，P99 略降。

**状态**：roadmap ☐ · PR open · submodule：**无** `buffer_only` 旗标（仍有 page_first_direct / 常规 L2 cache）。

**对 lake**：同向「放大共享层、缩小私有缓存」；lake 默认 L0–L3 均池化，比 buffer_only 更彻底。

### 2.4 CPU-only KV cache simulator (#21891)

**问题**：真 GPU 扫「模型×硬件×缓存策略」太贵；要无 GPU 回放 agent 负载。

**方案**：双层 hook（`__build_class__` + `meta_path`）替换 Scheduler/ModelRunner/HiCacheController/Radix/Storage；AIConfigurator 估延迟；Mock KV pool；HTTP 兼容 `bench_serving`；MAPE&lt;5%（H20 校验）。路线：HybridRadix、多实例 PD、Gateway 策略、复杂 HiCache 并发。

**状态**：roadmap ☐ · issue open · submodule：有 `debug_utils/schedule_simulator/`（**调度**仿真），与 #21891 全量 KV/HiCache 模拟器不完全等同。

**对 lake**：P7 性能建模可对照「零 GPU 回放」思路。

### 2.5 KVCache Sequence Split (#30501)

**问题**：Prefill CP 把整段 KV **每 rank 复制一份** → 组级唯一容量≈1×；MLA 在 TP 轴再复制。直接压 radix 命中与 TTFT。

**方案**：

- `--enable-kv-cache-sharding`：每页在 shard group 内**只存一份**。  
- **logical page** = N 个对齐 physical page（每 rank 一页）；所有权纯算术 `owner(loc)=(loc % N·ps)//ps`——**无目录、无跨 rank 共识**。  
- extend attention 用专用 NCCL allgather 提前一层拼 `[prefix|chunk]` scratch。  
- P/D 只发本 rank 拥有页。轴：有 CP → attn-CP；无 CP 的 MLA → attn-TP。  
- v1 范围：PD prefill、fa3、mooncake；HiCache/DSA/spec **先关掉验证**。

**状态**：roadmap ☐ · PR open · submodule：有 DSA `LayerSplitDSATokenToKVPool` / CP filter，**无**完整 sequence-split 旗标与逻辑页抽象。

**对 lake**：同问题域「分片去重」；lake 池侧分片/一致性哈希，不绑引擎 SPMD 算术所有权。

---

## 3. Q3 — UnifiedRadixTree Enhancements

### 3.1 TreeCore 解耦 → Rust (#29901)

**问题**：`UnifiedRadixCache` ~2900 行揉树机制 + component 值逻辑 + 调度/HiCache IO，无法单独换热路径。

**方案**：

```
Scheduler → UnifiedRadixCache（策略+IO+拥有 components）
                │ NodeId API
                ▼
          RadixTreeCore（可 Rust：match/insert/evict/LRU）
                │
          TreeComponents FULL/SWA/Mamba
```

约束：TreeCore 永不碰 cache；副作用以 typed Action 回传后由 cache 按序应用。早期独立 Orchestrator 方案已放弃。

**状态**：roadmap ☐ · issue/PR 讨论中 · submodule：**无** `RadixTreeCoreInterface`。

**对 lake**：与「radix 热路径 Rust」同向；lake 树在控制面/kv-pool，不在 Python 引擎内。

### 3.2 Mamba offloading 生产化（Qwen / Kimi）

**问题**：#20457 已有 Mamba offload / `HybridCacheController`，但产线模型（Qwen、Kimi）上可靠性/布局/与新存储 V2 接口仍缺口。

**方案**（roadmap 叙述，无独立 PR）：补产线模型覆盖、Mooncake/3FS V2 storage interface、PP 等（#20457 future plan 亦列）。

**状态**：roadmap ☐ · submodule：`HybridCacheController` / `MambaPoolHost` **present**；产线认证 open。

**对 lake**：hybrid 状态（Mamba leaf）进池时需独立生命周期，勿当普通 KV page。

### 3.3 KVCache-Canary

**问题**：分层/传输后 KV 正确性难 debug。

**方案**：集成 canary 校验工具链（roadmap 一句；无独立 issue）。

**状态**：roadmap ☐ · submodule：**有** `kv_canary/`（api、radix walker、JIT verify）——能力在，**与 UnifiedRadix/HiCache 强制联调未标完成**。

**对 lake**：池侧亦可做 canary/校验钩子；非主线。

---

## 4. Q3 — PD Disaggregation Integration

### 4.1 L2 DecodeRadixTree

**问题**：Decode 侧要更大 transfer batch、少占 GPU；需强化 **host 层** radix 复用（roadmap 名 `DecodeRadixTree`，**无此 class**）。

**方案**：在 Decode 上把 L2（host）前缀纳入匹配/拼装，配合 HiCache offload；#28874 亦写「Integrate with Decode L2 RadixTree stack」。

**状态**：roadmap ☐ · 无独立 PR · submodule：`decode_hicache_mixin` 的 L1/L2/L3 匹配与 restore **present**；产品化「L2 树」叙事仍 open。

**对 lake**：对应「主机层命中 + 上卷到执行节点」；权威仍应在池位置视图。

### 4.2 Prefill-as-a-Service PoC

**问题**：探索 Prefill 作为独立服务形态（异构资源、弹性）。

**方案**：roadmap 仅「Explore a proof of concept」，无设计文档/PR。

**状态**：roadmap ☐ · **absent**。

**对 lake**：接近「P 池可独立扩缩」；lake 三模式已含分离/混部，不必等 SGLang PoC。

---

## 5. HiSparse 生产就绪 (#28874)

**问题**：长上下文稀疏 decode——GPU 只留热工作集，全量在 pinned host；1MB 级 IO/内核/PD/MTP/模型覆盖未产线就绪。

**方案要点**（issue checklist）：

| 簇 | 内容 | 勾选摘要 |
|----|------|----------|
| 模型 | MiniMax-M3 CFC；DSV4 / GLM-5.x | 部分 ☑ |
| 1MB 性能 | IndexShared IO-prefetch 重叠 #28523；MTP 兼容；kernel；TwoBatch Overlap | 多 ☐ |
| PD Decode | 接 L2 DecodeRadix + HiCache；max len/OOM 修复；NIXL DRAM ☑；TP 共享 CPU #27370 | 多 ☐ |
| 算法 | Quest 等 training-free 扩展点 | ☐ |

**与 #21846 关系**：#21591 Host Transfer 走 HiSparse DRAM 路径；**HiSparse 与 decode radix 互斥**（server_args）。

**锚点**：`hisparse_coordinator.py::HiSparseCoordinator` · `admit_request_direct`。

**对 lake**：长上下文稀疏属 Could；传输「直入 host」可借鉴。

---

## 6. Q2 — HiCache × Hybrid Model

### 6.1 Hybrid Cache Controller + Linear (#20457)

**方案**：Mamba state offload；`MambaPoolHost` page-first；`HybridCacheController`；修泄漏；CPU→req state 拷贝。Future：Mooncake/3FS V2、PP。

**状态**：roadmap ☑ · merged · submodule present：`hybrid_cache_controller.py` · `MambaPoolHost`。

### 6.2 Mooncake backend × hybrid (#21259)

**方案**：Mooncake L3 支持 Mamba + DSA 模型布局/传输。

**状态**：roadmap ☑ · merged · submodule mooncake hybrid 路径 present。

### 6.3 MLA + Mamba Hybrid KeLing (#22957)

**方案**：KeLing 类 MLA+Mamba 混合进 HiCache（PR 描述稀疏，仍 open）。

**状态**：roadmap ☐ · PR open。

### 6.4 3FS backend × hybrid (#23241)

**方案**：3FS L3 支持 DSA & Mamba（配置 file_path_prefix / file_size 等）。

**状态**：roadmap ☑ · merged。

### 6.5 Unified Hybrid Radix Cache (#20415)

**方案**：统一 RadixCache / Mamba / SWA 分叉副本 → 公共接口 + `UnifiedRadixCache` + TreeComponent，便于 HiCache/PD/新架构扩展。多 stage PR 推进。

**状态**：roadmap ☑ · 主路径在树：`unified_radix_cache.py` · `registry.py`。

### 6.6 DeepSeek V4 HiCache (#24691)

**方案**：借 UnifiedTree SWA HiCache +「Shadow Radix」机制（代码无 `ShadowRadix` 符号名）接 DSV4。

**状态**：roadmap ☑ · merged · 测试 `test_unified_radix_cache_kl_dsv4.py` 等。

### 6.7 MiMo-V2 HiCache (#27378)

**问题**：`head_dim != v_head_dim`，原 MHA host 池假设 K/V 同 item_size → 传输出错。

**方案**：`AsymmetricMHATokenToKVPoolHost`；K/V 独立 host buffer；限制已验证 layout 组合。

**状态**：roadmap ☑ · merged · `pool_host/mha.py::AsymmetricMHATokenToKVPoolHost`。

### 6.8 MLA host KV 跨 TP 去重 (#26691)

**问题**：MLA 各 TP rank GPU KV 相同，HiCache 却各自 D2H → host 浪费 (N-1)/N。

**方案**：仅 rank0 真 host 池；其它 `MLATokenToKVPoolHost(is_dummy=True)` 只记账；H2D 时 rank0 load + NCCL 广播（`load_stream`）。

**状态**：roadmap ☐ · PR open · submodule：**未**落地该 dedup（仅有 transfer `is_dummy` 等无关优化）。

**对 lake**：同机去重；跨机仍靠池，不靠 per-TP host 复制。

---

## 7. Q2 — PD-Decode × HiCache

### 7.1 Decode Side RadixTree

**方案**：Decode 启用 radix（`--disaggregation-decode-enable-radix_cache`），命中前缀可少接 P→D KV；与增量传输联动。

**状态**：roadmap ☑ · 非 hybrid 路径 present：`decode.py::_match_prefix_and_lock`。

### 7.2 Decode Side HybridRadixTree SWA/Mamba (#27770)

**问题**：SWA hybrid 在 decode radix 启动即拒；多轮仍要整段传 full+SWA。

**方案**：

- 复用 **full-attention** 前缀（cap 在 SWA 窗边界），只为新窗口分配/传输 SWA tail。  
- `cache_unfinished_req`：插入、去重、**full-device match** 重定点（因 SWA tombstone 时普通 match 为空）。  
- 要求 Unified tree；**拒** HiCache、Mamba/SSM、DSA、SWA-compress。

**状态**：roadmap ☐ · PR open · submodule：仍 `ValueError` 拒 hybrid+decode radix。

### 7.3 Storage Prefetch Interface（Decode）

**方案**：Decode 新请求入队即可查 storage 命中并启动 Storage→Host→Device prefetch（不必等 Prefill 回）。

**状态**：roadmap ☑ · `decode_hicache_mixin._start_hicache_prefetch` · `prefetch_from_storage` present。

### 7.4 L2 RadixTree 提命中率

**方案**：强化 host 层树节点命中（与 §4.1 同簇，Q2 表述偏「命中率」）。

**状态**：roadmap ☐ · host_value / host_hit_length 机制已有；独立「L2 树」产品项 open。

### 7.5 Storage group 语义

**问题**：一页内多 key（K/V、按 rank 切分等）需**统一可见与驱逐**。

**方案**：Mooncake Group Semantics（[#1887](https://github.com/kvcache-ai/Mooncake/issues/1887)、[#2127](https://github.com/kvcache-ai/Mooncake/issues/2127)）；SGLang `enable_group_semantics` + `_make_group_id`。

**状态**：roadmap ☐ · submodule **present**（mooncake_store + tests）；跨后端完备性仍跟 Mooncake。

**对 lake**：池内「逻辑共置 / 组驱逐」可对照。

### 7.6 异构 TP × PD × HiCache

**问题**：P/D TP 不等时布局/传输与 HiCache restore 叠在一起难做对。

**方案**：roadmap 无独立 PR；现有 **hetero-TP staging**（`staging_handler.py`、NIXL prep）与 HiCache 路径未统一；HiSparse+hetero 有限制。

**状态**：roadmap ☐ · partial。

---

## 8. Q2 — PD Disaggregation（传输）

### 8.1 Incremental KV Transfer

**方案**：Decode 查本地/存储前缀长度 L → 协议带 `decode_prefix_len` → Prefill `start_send_idx=L` 只发残差。全命中时 KV indices 可空，仍传 aux/state。

**状态**：roadmap ☑ · present：`TransferInfo.decode_prefix_len` · `prefill.py::finalize_bootstrap`。

**对 lake**：残差传输契约可直接借鉴；命中权威改池视图。

### 8.2 PD Host TransferMode (#21591)

**方案**：Prefill 将 KV **直传 Decode DRAM**（绕 GPU 中转），利好长上下文 Decode batch；落地 HiSparse + NIXL DRAM mem kinds。

**状态**：roadmap ☑ · merged · `admit_request_direct` present。

### 8.3 Large decode batches

**方案**：Decode 用 host 上 L2/L3 已命中前缀 + Prefill 增量，再一并 load 回 GPU 做后续步。

**状态**：roadmap ☐ · `HiCacheRestoreResult` / `_process_hicache_local_restores` / `DecodePrefixMatch` **present**；与 §4.1/§7.4 一起标产品化未完。

---

## 9. Q2 — Agent / Rollout KVCache Management

### 9.1 Agent-Aware KV Phase 1 (#24656)

**方案**：OpenAI 兼容请求可选 `agent_hints`（workflow_id、step、tool、TTL、reuse_hint…）→ 调度器轻量 DAG → 树节点标注 → 可选 `agent_aware` 驱逐。不做跨进程协调/生产调参。

**状态**：roadmap ☐ · RFC open · submodule **absent**。

### 9.2 Programmatic KV Cache hints (#27574)

**方案**：见 [§2.2](#22-kv-orchestrator--prefetchdemotepin)；与 #24656 互补——前者偏 API 元数据进树，后者偏 Router soft hint + L3 retention。

**状态**：roadmap ☐ · RFC open · submodule **absent**。

---

## 10. Q2 — HiCache × 其它特性

### 10.1 PP × HiCache (#22607)

**问题**：每 PP rank 独立树；L3 **异步 prefetch** + wall-clock LRU → host 树发散 → `host_hit_length`/shape 崩溃。L1/L2 同步路径相对安全，L3 放大分叉。

**方案**：三通信域——

1. prefetch 线程 gloo all_reduce MIN（storage_hit）  
2. 另一 gloo 组 MIN（completed_tokens）——**两线程不能共用同一 communicator**  
3. Scheduler：TP/CP 内 reduce + **PP0→下游 `_pp_sync` 定向广播**（terminate/drain）

WIP 多项 PR（L3 fix #27010 等仍 open）。

**状态**：roadmap ☐ · issue open · submodule：有 `_pp_sync` / `check_hicache_events`，**完整事件编号方案未闭环**。

**对 lake**：多副本位置视图必须单写者或强同步；印证「弱一致多树」成本。

### 10.2 MTP × HiCache (#21125 · #30393)

**问题**：draft/target 共享 `req_to_token` 但 KV 池分离；target 从 L2/L3 load_back 后 draft 未更新 → 接受率崩。

**方案**：

- #21125：draft KV 进 L2/L3 backing（`set_draft_kv_pool` / `maybe_register_hicache_draft`）。  
- #30393：sidecar——`DRAFT`、`DRAFT_INDEXER`（DSA）、`DRAFT_SWA` 与主池一并传输；修 replay 后 accept rate。

**状态**：roadmap ☐（组合仍脆）· 两 PR 已合/在推进 · submodule partial（Mooncake v2 draft；部分后端禁 draft L3）。

### 10.3 EP & DP × HiCache

**方案**：roadmap 已勾；使 Expert/Data parallel 与 HiCache 共存。

**状态**：roadmap ☑ · 工程上仍见组合 bug（见 pain-points）；无单一设计专文。

### 10.4 CP × HiCache (#20460)

**方案**：Context Parallel 与 HiCache 同步（#22607 依赖的 cp sync）。

**状态**：roadmap ☑ · merged（PR body 短）。

### 10.5 CP Layer-Wise Split × HiCache (#29421)

**方案**：DSA MLA 下按层拆分 KV 所有权（`LayerSplitDSATokenToKVPool`），owner broadcast；与 HiCache/PD prefill-CP 协同。

**状态**：roadmap ☑ · `enable_dsa_cache_layer_split` present。

---

## 11. 代码索引（子项 → 符号）

| 子项 | 文件:符号 |
|------|-----------|
| Session（legacy） | `session_radix_cache.py::SessionRadixCacheMixin` |
| Incremental PD | `TransferInfo.decode_prefix_len` · `prefill.py::finalize_bootstrap` |
| Decode prefetch | `decode_hicache_mixin.py::_start_hicache_prefetch` |
| Large batch restore | `decode_hicache_mixin.py::HiCacheRestoreResult` |
| Host 直传 | `hisparse_coordinator.py::admit_request_direct` |
| Unified 树 | `unified_radix_cache.py::UnifiedRadixCache` |
| Hybrid 控制 | `hybrid_cache_controller.py::HybridCacheController` |
| Asym MHA | `pool_host/mha.py::AsymmetricMHATokenToKVPoolHost` |
| Mooncake group | `mooncake_store.py::_make_group_id` |
| PP sync | `hiradix_cache.py::_pp_sync` · `unified_radix_cache.py::_pp_sync` |
| Draft HiCache | `cache_controller.py::set_draft_kv_pool` · `maybe_register_hicache_draft` |
| CP layer-split | `dsa_cache_layer_split.py::LayerSplitDSATokenToKVPool` |
| Canary | `kv_canary/api.py` |
| Hybrid 拒 decode radix | `kv_cache_builder.py`（SWA/SSM ValueError） |

---

## Z. 对 lake 总对照

| SGLang #21846 簇 | lake | 态度 |
|------------------|------|------|
| 增量 PD / Host 直传 / Decode prefetch | Transfer Bus + 残差拉 | **借鉴** |
| buffer_only / 放大 L3 | 池统一 L0–L3 | **同向，lake 更彻底** |
| Session / agent hint | 前缀亲和 + ref 冻结；gateway 意图 | **借鉴原则，不照搬 soft API** |
| Sequence Split / MLA TP dedup | 池分片与同机去重 | **借鉴问题，换池实现** |
| TreeCore Rust | kv-pool/controlplane Rust/Go | **同向不同位** |
| PP×L3 多树同步 | 单写者位置视图 | **不照搬多树+all_reduce** |
| HiSparse / MTP sidecar | Could | 后期对照 |
| Prefill-as-a-Service | 三模式已覆盖分离 | 不必等 |

**分叉不变**：SGLang = 引擎私有 L1/L2 + 外挂 L3 + soft hint；lake = 池权威 + 无状态 worker + D-direct。

---

*二次调研覆盖 issue 正文全部勾选项；无独立 PR 的条目已标明「实质载体 / 仅叙事 / absent」。上游状态以 GitHub 为准，请定期重核。*
