# 02 — 总体架构

lake 以 **KV 为中心**做彻底存算分离:所有有状态物(权重、KV cache、调度队列)从算力路径剥离,算力节点可随时销毁/拉起。请求短暂、节点可换,**KV 是贯穿多请求、多节点、多轮次的唯一长存物**。

> 设计动机的展开(虚拟内存 → PagedAttention → 存算分离)与业界对照,见 [`kv-virtual-memory.md`](kv-virtual-memory.md)。

## 分层视图

```mermaid
flowchart TB
    client["Client"] --> gateway["Bifrost Gateway\n鉴权 / 限流 / 准入"]
    gateway --> router["Go Router\nview mirror + 模式选择"]

    subgraph computePool["计算节点池"]
        worker["Python Worker\nscheduler + ModelRunner"]
        agent["Rust storage-agent\nFFI + transfer endpoint"]
        localOverlay["agent local overlay\nL0/L1/L2 state + local ref"]
        l0["L0 HBM"]
        l1Local["L1 DRAM\n本机载体"]
        l2Local["L2 NVMe\n本机载体"]
        worker <--> agent
        agent <--> localOverlay
        localOverlay --- l0
        localOverlay --- l1Local
        localOverlay --- l2Local
    end

    subgraph storagePool["存储池"]
        storageCp["Rust Storage Control Plane\nAuthority global view"]
        kvAgent["KV Node agent\nlocal tier serve"]
        kvOverlay["agent local overlay\nL1 MR + L2 NVMe"]
        l1Remote["L1 DRAM\n池化载体"]
        l2Remote["L2 NVMe\n池化载体"]
        objectStore[("L3 Object Store\nSSOT")]
        storageCp -->|"放置 / GC / defrag / shard"| kvAgent
        kvAgent <--> kvOverlay
        kvOverlay --- l1Remote
        kvOverlay --- l2Remote
        storageCp --> objectStore
    end

    subgraph metadata["低频元数据"]
        etcd[("etcd\nleader lease + checkpoint")]
    end

    router -->|"Dispatch"| worker
    router <-->|"view stream / snapshot"| storageCp
    agent -->|"async events / batched commit"| storageCp
    agent <-->|"Locate confirm / barrier"| storageCp
    agent <-->|"TransferService\nRDMA / TCP fallback"| kvAgent
    storageCp <-->|"checkpoint / lease"| etcd
```

计算节点不拥有内存——HBM/RAM/NVMe 是存储池的物理载体,不是 worker 私有状态。热路径上由本机 agent 写 `local overlay`（slot/free-list/local ref/L1-L2 本地状态）,再把可全局可见的事件批量提交给存储控制面；`Authority` 维护的是全局提交态视图,不是每 token/step 的同步热路径。

## 架构图索引

- **总体架构**：本页上图，说明 Gateway / Router / 计算节点 / 存储控制面 / KV Node / L0–L3 的边界。
- **KV Pool 模块图**：见 [`kv-cache-pool.md`](kv-cache-pool.md) "KV Pool 模块图"，说明 block 身份、radix、位置视图、agent 与分层介质。
- **控制面权威图**：见 [`control-plane.md`](control-plane.md) "控制面模块图"，说明 Authority、checkpoint、view mirror 与 Router/agent 的关系。
- **请求生命周期图**：见 [`data-flow.md`](data-flow.md) "请求生命周期总图"，说明准入、选路、执行、写回与失败恢复。
- **存储层职责切分图**：见 [`storage-layer.md`](storage-layer.md) "分层缓存"，说明为何采用纵切职责而不是按层横切 owner。

## 关键设计决策

### 1. 算力节点无状态
节点不持久化任何推理状态。权重从存储池加载,KV 从存储池读写,调度状态在控制面(etcd)。节点可随时杀死,崩溃/缩容不丢可恢复进度。

### 2. P/D 物理隔离(可选,非强制)
借鉴 DistServe / Splitwise:Prefill(计算密集)与 Decode(访存密集)可分池,各自按负载伸缩。但**请求不固定走 P→D**——见决策 3。

### 3. 三种执行模式,逐请求选路
Router 按 `f(请求, 集群状态) → (模式, 节点)` 逐请求选路,三模式同集群并存:

- **PD 分离**:P、D 分机,KV 跨节点传输。
- **混部**:P+D 同节点完成,无跨节点传输。
- **D-direct**:前缀 KV 已被存储池放置在执行节点 HBM,零/极小传输直跳。

执行模式与 KV 流转时序的权威描述见 [`execution-modes.md`](execution-modes.md)——以存储池视角归为两条时序(本地完成 / 跨节点传输),P/D 只是调度层角色。

### 4. 分层缓存归存储池统一管理
```
GPU HBM(L0) ─ DRAM(L1,池化) ─ NVMe(L2,池化) ─ 对象存储(L3, SSOT)
```
**层 = 介质,不是位置。** 四层全部由存储池统一管理(放置/驱逐/副本/冷热/生命周期)。L0/L1 做缓存副本(易失),L2/L3 间按移动,L3 永久权威;L2(NVMe)= F4 恢复点(NVMe 持久 + NPU 故障不烧 NVMe,恢复能力与位置无关),L3 = SSOT(抗整机级/池级失败)。DRAM/NVMe 各是一层、一个池,block 放本机还是远端是池的放置决策(同 L0 放哪个节点 HBM),不按位置拆成两层。冷热按引用数+热度分(LFU-Aging)+前缀亲和判定;迁移主动为主+被动兜底。详见 [`storage-layer.md`](storage-layer.md)。

### 5. 控制面 / 数据面分离
- **控制面**:KV 位置元数据、节点拓扑、负载视图、扩缩容决策。强一致(etcd)。
- **数据面**:KV 传输、权重加载、模型前向。最终一致,容忍延迟。

## 请求生命周期

```
Request → Bifrost(Gateway)
  → Router 读本地命中视图镜像(radix + 位置视图):前缀复用?是否本地命中?
  → 按命中与负载选模式 + 节点:
      ├─ 本地命中        → D-direct:该节点残差 prefill + decode
      ├─ 单节点划算      → 混部:该节点完整/增量 prefill + decode
      └─ 跨节点划算      → PD 分离:P prefill → KV 放置到 D → D decode
  → decode 增量 KV 异步写回存储池(容错 + 前缀生长,见 execution-modes.md)
  → 多轮 agent:延伸 KV 亦可 D→P 直接喂回 prefill 节点服务下一轮(见 data-flow.md §3.4)
  → 完成 → 本机 HBM 放置归还存储池(L1+ 副本按冷热保留)
```

失败 → F4 重新路由,Router 重跑选路(纯函数),不设降级阶梯。

## 开放问题

- KV 传输带宽与 prefill/decode 计算时间的比值,在什么范围内 P/D 物理分离才划算?
- KV Pool 分片粒度(per-layer / per-block / per-sequence)如何影响传输与复用?(已定 per-block,粒度 128 token,见 [`kv-cache-pool.md`](kv-cache-pool.md))
- 投机解码 draft model 放哪一层?→ 已定:默认与 decode(target)共置(sglang 式),独立 Draft 池可选,见 [`compute-layer.md`](compute-layer.md) "投机解码"节。
- **请求在途迁移(换 DP/换节点)归存算分离的开放性**:未来特性;KV(target + drafter)归池权威使迁移代价低,抢占重算式下框架无需特别预留(见 [`scheduling.md`](scheduling.md) "DP 间在途再均衡")。控制态交接、seed 重建、抖动控制等细节待特性立项。
