# KVCR(KV Cache Runner)— NVIDIA 新一代分布式 KV 缓存

> 源码:`3rdparty/kvcr/`(NVIDIA [ai-dynamo/kvcr](https://github.com/ai-dynamo/kvcr),Apache-2.0)。纯 Python,18 个文件约 8.7k 行。2026-08-24 在 [Dynamo DEP #11673](https://github.com/ai-dynamo/dynamo/issues/11673) 公布,官方自述"early open-source code drop,非生产就绪"。本文为核实后的分析(读 README + `docs/design_overview.md` + `PROVENANCE.md` + 源码)。
>
> **背景**:KVCR 是 KVBM 的继任者。KVBM v1 已被官方宣布 sunset,KVBM v2(kvbm-engine)合入 main 但始终没接生产绑定。变局全过程见 [`../dynamo/overview.md`](../dynamo/overview.md) "KVBM 变局"节。

## 一句话定位

KVCR 是**引擎进程内的 KV 缓存二级存储库 + 跨节点 P2P 数据面**:GPU 显存归引擎自己管,KVCR 只管 DRAM/SSD/对象存储和机器之间的搬运;全局"哪块 KV 在哪"归 router,KVCR 实例之间互不知情,靠 router 的请求级 hint 找到远端源。

## 出处(PROVENANCE)

据 `3rdparty/kvcr/PROVENANCE.md`([GitHub 链接](https://github.com/ai-dynamo/kvcr/blob/main/PROVENANCE.md);CI 不检出 submodule,文档一律不相对链接进 `3rdparty/`):

- 核心代码抽自 NVIDIA **私有 vLLM fork** 的 `vllm/v1/kv_offload/tiering/kvcc/`(6 个生产模块),2026-08-21 从内部仓 `NVIDIA-dev/kvcc` 初始化公开仓,全新历史。
- 即:它生来就是 **vLLM 原生 `kv_offload` 框架的一个 tiering 后端**,不是 Dynamo 的组件。Dynamo KV Router 只是它对接的 router 之一(还支持 sgl-router、llmd-router)。
- vLLM 侧接入 PR:[vllm#53624](https://github.com/vllm-project/vllm/pull/53624)(KVCR secondary-tier adapter,截至 2026-09-03 仍 open);quick-start 容器用该 PR 的 pin commit 构建。

## 放在哪一层

![KVCR 数据面架构](figures/kvcr-masthead.jpg)

(图源:`3rdparty/kvcr/docs/figures/kvcr-masthead.jpg`)

```
        Router(全局 BlockKey inventory + 请求级 hint)
          │  hint: block_hashes + 源节点 control endpoint + copy/move
          ▼
  Engine(vLLM/SGLang/TRT-LLM)
    · G1 HBM:引擎唯一所有者(分配/驱逐/调度)
    · 引擎自有 G2 offload:vLLM kv_offload 框架(primary/secondary tier)
          │  进程内 API:deposit / query / fetch / deliver / release + pin 回调
          ▼
  KVCR(引擎进程内 Python 库)
    · KVCR 自有 DRAM(G2)/ SSD(G3)/ 对象存储(G4)
    · NIXL agent:本地与跨机传输的统一抽象
          │  NIXL(RDMA/UCX/GDS…),字节不过 router
          ▼
  Peer KVCR(其它节点,destination 发起拉取)
```

- **接入点**:vLLM `vllm/v1/kv_offload/tiering/`(本仓 vllm submodule 该目录已有 `example/`、`fs/`、`obj/`、`p2p/` 后端,KVCR 经 #53624 新增 `tiering/kvcr/`);SGLang 侧走 HiCacheStorage 后端([sglang#32903](https://github.com/sgl-project/sglang/issues/32903))。
- 对照 KVBM:KVBM 是 Dynamo 的 Rust 组件,经自定义 connector(`DynamoConnector`)+ 自定义 CUDA kernel 接入;KVCR 是框架无关的 Python 库,经各引擎**原生** offload 接口接入。

## 与 KVBM 的区别 / 为什么重做

DEP #11673 点名现有方案(含 KVBM)的两种失败模式:

1. **紧耦合 GPU**:外部组件管 GPU 侧 KV block → 抢 kernel launch 带宽、要持续与引擎同步调度上下文;
2. **幼稚简单**:内存压力下被动驱逐,无准入过滤、无跨节点协调。

KVBM 两条都占了(v1 管 G1 布局/注册/搬运 + 自定义 reshape kernel;跨实例方案 #4887/#5243 自建 ZMQ registry hub,均未合并)。KVCR 是针对性的重做:

| 维度 | KVBM(v1) | KVCR |
|------|----------|------|
| 归属/语言 | Dynamo 子组件,Rust | 独立仓,框架无关,Python(依赖仅 msgspec/pyzmq/nixl) |
| GPU 显存 | 管 G1 layout 注册 + 自定义 kernel 做 reshape/搬运 | **完全不碰 GPU 管理**;引擎给指针,KVCR 用 NIXL 代做 DMA |
| 全局视图 | 曾自建 registry hub(未合并)/ NATS 事件流 | **不建**,复用 router 的最终一致 inventory;KVCR 只知本地 |
| 跨机共享 | v1 无(仅本机 G2/G3);v2 有 session 协议但没接生产 | **核心能力**:hint 驱动、destination 发起、NIXL P2P(远端 DRAM→本地 DRAM 或直灌 GPU) |
| 接入方式 | 自定义 connector + kernel | 引擎原生 kv_offload / HiCache 后端;框架负责 canonical 布局 |
| 弹性 | 引擎死则缓存失 | **KVCR-Guard** sidecar 持有内存池,引擎挂了缓存仍可被远端读,重启后 fenced handoff 热恢复 |
| 策略 | 固定 | 可插拔 policy(准入/驱逐/放置/恢复),默认 LRU |
| 状态 | **sunset**(官方 2026-07-16 宣布;2026-08-27 [dynamo#12993](https://github.com/ai-dynamo/dynamo/pull/12993) 把它从 offload 推荐后端撤下,改推 LMCache/FlexKV/HiCache) | 早期开发,MVP 目标 2026-09 月中 |

**为什么这么做**(官方理由,DEP #11673 讨论):

1. **GPU 管理必须归引擎**:引擎已有全部调度上下文(调度状态、活跃请求、decode 步数),任何外部实体都要被持续告知——协调必然上热路径。KVCR 的不变量是"GPU 内存所有权与调度归引擎",谁物理发起 DMA 不重要。
2. **框架原生 offload 已成熟**:vLLM kv_offload 输出 canonical 布局(连 DS4 SWA/CSA/HCA 都背靠背排好),SGLang 类似。KVBM 自定义 reshape kernel 的存在意义消失 → 关注点分离:框架做布局,KVCR 只搬数据。
3. **全局视图重复建设**:router 为路由本就追踪 KV 位置,再建一个 registry 是浪费 → router 持全局 inventory(尽量小,按 `BlockKey` 键),KVCR 实例无 peer 库存。"同节点与跨节点复用用同一 source 模型,共置只改变传输,不改变所有权与协调模型。"
4. **拉取 vs 重算的决策下放 worker**:router 只给 hint;是否去拉由目的地按自身 GPU/内存负载决定——"从中心点做这个决策很 tricky"。

## 架构

![KVCR 组件边界](figures/kv-architecture-detailed-light.svg)

(图源:`3rdparty/kvcr/docs/figures/kv-architecture-detailed-light.svg`,Figure 1)

### 组件角色

- **Framework/Engine**:GPU 内存唯一所有者;决定哪些 block 进出自有内存;经描述符 + 可选 pin 接口把自有 GPU/host 内存暴露给 KVCR(KVCR 注册进自己的 NIXL agent);向 router 上报自有内存。
- **KV Router**:维护所有 engine/KVCR 实例上报的最终一致全局 `BlockKey` inventory(带 tier 标签);按 overlap + 负载做缓存感知路由;向目的地 KVCR 发 hint(跨机检索给源节点 + peer control endpoint)。
- **KVCR**:引擎进程内库;管 KVCR 自有 DRAM/SSD 驻留 + 配置的对象存储;上报 KVCR 层 inventory;经 NIXL 执行数据移动;执行内建或用户策略;响应引擎的可用性查询;不阻塞引擎热路径。
- **KVCR-Guard(可选 sidecar)**:弹性开启时持有 KVCR 的 DRAM 分配;暴露一个 socket 端点供 active KVCR attach 并恢复全部已提交状态;attach 后经共享内存直访池,正常操作**无 IPC 往返**;宿主 backup KVCR(自带 NIXL agent),fencing 掉失效 owner 后可服务已提交的 KV;引擎重启后热启动并交还所有权。**不保 framework 自有内存**。

### 数据面:hint 驱动的 P2P

- **hint 协议**(JSON,`src/kvcr/hint_parser.py`):`source_control_endpoint` + `block_hashes`(u64 列表)+ `mode`(`copy` 保留源 / `move` 成功后源可驱逐)+ `no_retain`(建议目的地不留副本)。超时/取消保留源副本。
- KVCR 间移动**由目的地发起**;router 永不在数据路径上;peer 控制通道(ZMQ)只走连接元数据/ack/传输控制,字节直走 NIXL。
- `query` 返回 `HIT(tier)` / `FETCHING(tier)` / `FETCHABLE(source_tier)` / `MISS`——只读本地知识,**不查 router**;远端源信息只经 `submit_hint` 到达。`FETCHABLE`(peer DRAM) 的实际可读性要传输 + pin 成功才确认。

### 状态模型

中心结构 `block_index: BlockKey → BlockRecord`(`src/kvcr/core.py`)。`BlockRecord` 记录该块的全部已知位置(`fw_mem` / `local_dram` / `g3`)+ `in_flight_ops` + 访问统计;**只含本地状态,不随集群增长**(全局 inventory 在 router)。驻留状态与操作状态刻意分离;**claim** 挂在具体驻留上防驱逐(pin 保 framework 内存);就绪且无 claim 的驻留进 policy 打分的驱逐队列。

### 异步执行

event loop 拥有全部元数据变更、从不阻塞;独立 **progress 线程**拥有 NIXL agent,提交/轮询传输并把完成回投给 event loop。每个操作有 deadline;超时/取消立即上报并开始安全释放;framework pin 在其依赖工作完成后尽快释放;若 NIXL 仍可能访问描述符,物理释放等传输静止。

### API 表面(伪代码节选自 design_overview)

- Framework→KVCR:`deposit`(推入 KVCR 池,可 `no_evict`)/ `query` / `fetch`(驻留并 pin 在 KVCR DRAM)/ `deliver`(放到引擎给的目的地,源选择归 KVCR+router)/ `release` / `poll_completed` / `abort` / `close`。
- KVCR→Framework:`capacity_needed`(背压,最后手段)/ `request_pin` / `poll_pin_results` / `cancel_pin_request` / `release_pin`。
- Router↔KVCR:`InventoryEvent{keys, tier, removed}` 上报(只报受影响 key);`submit_hint` / `discard_hint` 下发。
- Policy:`decide_ingest`(准入)/ `eviction_score` + `decide_eviction` / `on_ingest` / `on_remove` / `decide_recovery`;`PlacementAction` = KEEP/DROP/COPY_TO/MOVE_TO;默认 LRU。`no_evict` claim 是硬约束,`no_retain` 是 advisory,冲突时 `no_evict` 赢。

## 时间线(怎么走到这一步)

| 时间 | 事件 |
|------|------|
| 2025-12 ~ 2026-01 | KVBM v1 上两次分布式尝试:[#4887](https://github.com/ai-dynamo/dynamo/pull/4887)(G4 对象存储 + registry hub)、[#5243](https://github.com/ai-dynamo/dynamo/pull/5243)(+23k 行分布式 KVBM)——**均未合并**,作者关闭 |
| 2026-04-08 | [#6773](https://github.com/ai-dynamo/dynamo/pull/6773) kvbm-engine(v2)合入 main:session 化 remote search/pull 协议——但**只合了库**,生产绑定仍 v1,仅 mocker 使用 |
| 2026-07-14 | [DEP #11673](https://github.com/ai-dynamo/dynamo/issues/11673)「KV Cache Controller」发布 |
| 2026-07-16 | 官方(harryskim):**"KVBM v1 is being sunset"**;新方向 = "引擎原生 offload + P2P 连接各节点 CPU 层",MVP ETA 9 月中 |
| 2026-08-21 / 08-24 | KVCR 仓初始化 / 在 DEP #11673 公开宣布 |
| 2026-08-27 | [dynamo#12993](https://github.com/ai-dynamo/dynamo/pull/12993) 文档把 KVBM 从 offload 推荐后端撤下 |
| 并行 | 生态对接:TRT-LLM [#18151](https://github.com/NVIDIA/TensorRT-LLM/issues/18151)(router hint P2P)、vLLM [#53421](https://github.com/vllm-project/vllm/issues/53421)(hint envelope)+ [#53624](https://github.com/vllm-project/vllm/pull/53624)(KVCR adapter)、SGLang [#36224](https://github.com/sgl-project/sglang/issues/36224)(hint)+ [#32903](https://github.com/sgl-project/sglang/issues/32903)(KVCR 作 HiCache 后端)、Dynamo router [#13134](https://github.com/ai-dynamo/dynamo/pull/13134)(typed KV hint contract) |

外部贡献者曾尝试给 KVBM 补 P2P([#7879](https://github.com/ai-dynamo/dynamo/pull/7879) "g2pb global peer offloading",draft),因官方转向 KVCR 而搁浅。

## 与 lake 对照

- **全局视图归属**:KVCR 复用 router(最终一致、最小 key 集、可水平扩展)vs lake 存储控制面进程内存权威(单写者线性一致、一跳命中、etcd 降频 checkpoint)。KVCR 证明"router 持全局视图 + hint"在工业界可行;但 lake 的存储池是长期存续基础设施、router 无状态,权威仍归池。
- **GPU 所有权方向相反**:KVCR 是"引擎拥有一切(含 HBM),KVCR 服务引擎";lake 是"池拥有一切(HBM 是 L0),引擎无状态可随时销毁"。KVCR 的 Guard 是在"引擎长期存活"假设上的容错补丁;lake 的假设本身就是引擎随时销毁。注意这不构成对 lake 的否定——KVCR 服务的是"引擎自有缓存"世界,lake 设计的是"存算分离"世界。
- **hint(拉、决策在 worker)vs 位置视图推送(放置归池、调度读视图)**:KVCR 的 `submit_hint` + `query`(HIT/FETCHABLE/MISS)+ worker 侧成本决策,对应 lake"池放置·调度读视图"的单向耦合——但 lake 的放置主动权在池(热度预放置),KVCR 的搬运主动权在目的地 worker(router 只 hint)。
- **分层**:KVCR 的 `CacheTier`(framework mem / KVCR DRAM / SSD / object / peer)也是介质分层,与 lake L0–L3 同构;但 KVCR 按"谁拥有"再细分(framework-owned vs KVCR-owned),lake 不分所有权(全归池)。
- **值得借鉴**:pin/claim 机制(传输中防驱逐 + 共享获取的取消安全)、操作 deadline 与"安全释放可超 deadline、调度不可"的语义、policy 可插拔接口形态(`decide_ingest`/`eviction_score`/`COPY_TO`/`MOVE_TO`)、`no_retain` advisory hint、Guard 的 fenced handoff + 共享内存直访(对应 lake F4 恢复与 L2 恢复点设计)。

## 代码索引

> 沿代码回溯用。符号名锚定,行号会漂移——找不到时 `grep -n "符号名" 3rdparty/kvcr/<文件路径>`。

| 机制 | 文件:符号 |
|------|-----------|
| 公开北向 API | `src/kvcr/api.py`::`KVCR` / `KVCRBindings`(pin 回调、inventory_sink、capacity_needed) |
| 核心状态(block 索引/驻留) | `src/kvcr/core.py`::`_KVCRCore` / `_BlockRecord`(`fw_mem`/`local_dram`/`g3`/`in_flight_ops`) |
| KVCR 自有 DRAM 层 | `src/kvcr/local_dram.py` |
| G3 磁盘层 | `src/kvcr/local_disk.py` |
| **P2P 远端 DRAM(跨机共享核心)** | `src/kvcr/remote_fw_dram.py`(全仓最大,1.6k 行;target: hint/query→fetch/deliver→start_write→write_done) |
| NIXL progress 线程 | `src/kvcr/progress.py`::`_KVCRProgress` / `_Op` |
| peer 控制通道(ZMQ) | `src/kvcr/control_channels.py` |
| router hint 协议解析 | `src/kvcr/hint_parser.py`::`ROUTER_HINT_KEY` / `_parse_kv_hint` |
| 策略接口与运行时 | `src/kvcr/policy.py`::`KVCachePolicy`;`src/kvcr/policy_runtime.py` |
| Guard(sidecar/接管/热恢复) | `src/kvcr/guard.py` / `guard_protocol.py` / `recovery_journal.py` / `memory.py`(共享内存池)/ `kvcr_service.py`(独立 daemon) |
| 设计文档 | `docs/design_overview.md`(Goals/Architecture/四个 API 面) |
