# kvcm 闭环条目存档

来源：[backlog.md](backlog.md) 的已闭环条目——条目闭环即移入本文存档，backlog 原位置留编号占位（编号不复用）。正式结论以各条目指向的正式文档（D 系文档 / `kv-prefetch.md` / README）为准；本文是过程记录存档，不再更新。

## 一、设计决策

**D1. Router 集成与请求调度** — ✅ 已闭环

最终形态：router 决策请求发哪个实例 → precopy 预热该实例 → 并决定哪些 KV 释放。工作文档：[D1-router-scheduling.md](D1-router-scheduling.md)（事实基线 F1-F15 已核实，含复制幂等性与 lease 语义；六问全闭环，「待定问题」已转「设计决策」）。

六个问题（全部闭环，结论写在 D1 对应小节）：

1. **选路输入**——✅（D1 §1）
   - 选路逻辑分两层：
     - 索引：订阅事件流，维护「block hash → 实例」的表
     - Router：自己算请求的 hash 链去查索引（RFC #1403 `/query` 支持 token_ids 或 hash key 两种入参，不需要原始 token 文本）
   - 索引的数据来源两个：引擎原生事件 + kvcm 补发（预复制放置）；kvcm 负责补齐引擎看不到的部分
   - Router 不访问池 key——池里的位置信息是控制面的事
   - 索引做成什么进程形态（Router 内置 / 独立进程 / 内置但对外暴露查询——全量索引只有一份；辅助预测树单独决策）与事件开关、传输接线归 D4
2. **可见性**（索引怎么知道 KV 放在哪、删了没）——✅（D1 §2）。拆两问：
   - **放置** = 方案 a：控制面补发 `BlockStored` 事件
     - 为什么只能补发：池事件不带机器位置、复制路径本身不发布任何事件（任何版本都如此，F14）
     - 补发时机：READY 核验通过后，即「全 rank 都备好、可以路由」——master 的单 key 粒度给不了这个判断
   - **删除** = 升级 mooncake、开 master publisher
     - 首选：索引侧直接订阅 publisher，解析 PoolKey 得出 block hash（Conductor `ParseVllmObjectKey` 现成；Dynamo `shared_cache.rs` 自己维护 hash↔key 映射，思路相同，F14）
     - 备选：kvcm 订阅后翻译成引擎格式重发
     - 前提：0.3.11 不含 publisher，须升级（E8 已核）
   - 明确不做：轮询比对（只为删除信号不值得）、索引条目 TTL（与放置无关的独立兜底，不需要）、Router 直读 master（排除）
   - 方案 a 的复用要点：事件字段全是 keys.py 的中间产物，发布直接 import 上游 `ZmqEventPublisher`；事件身份 = 端点（每个目标实例一个补发端点，「端点 → 实例」注册归 D4/E13；索引做成独立进程时走 RFC #1403 `/register` 注册）
3. **「哪个实例拥有前缀」怎么定义**——✅（D1 §3）
   - 实例 X 拥有前缀 P：P 的连续满块链的 KV 在 X 本机——HBM（gpu 事件）和本机池 seg（cpu 事件 / kvcm 补发）都算
   - 命中长度 = 连续满块数 × block_size
   - HBM 与 DRAM 的差异留到 §5 打分处理
   - rank 维度不用管：事件只在全 rank 齐备时才发，Router 不需要 per-rank 索引
4. **时序**——✅（D1 §4）
   - 请求不等 READY：照常走 F3 兜底链（副本在池里就远端读），复制在后台追
   - 复制的三个时机并存：
     - 顺手补：请求路过时顺带触发
     - 提前备：前端 agent/coder 钩子在请求发出前先把信号给控制面（最终形态，归 D4）
     - 直接用现成的：Router 前缀亲和本来就倾向把请求发到已有所需 KV 的实例
   - intent 以（前缀 × 目标实例）为幂等键去重
   - 边界：kvcm 不决策选路；Router 不直接下复制命令，用 intent 表达意图（Router → kvcm 下行）；事件上行（kvcm → 索引）
5. **选路目标函数**——✅（D1 §5）
   - 归 Router 内部实现，kvcm 不参与决策（§1-§3 之后 Router 的输入已齐）
   - 候选已列：分级兜底 / 打分制（Dynamo 的分层成本可借来当初值）/ 传输成本两种算法
   - 权重待真机校准（P7）
6. **release 归谁**——✅（D1 §6）
   - 分工：Router 提供会话生命周期线索，控制面决策执行，池驱逐兜底
   - 三个手段：
     - 主路径：停保活（软释放——有读就自动续租，动不了正在用的数据）
     - 中间级：demote 降层（DRAM→SSD，不删，归 D2）
     - 例外：主动 `Remove`（key 是全局的，别的实例上的原始副本会一起删）——仅限「确认没人用」

已知现状：

- 引擎事件通道上看不到池副本删除（F10）→ 索引里池副本条目只增不减，条目过时只损性能（F3）
- GPU 侧有 Removed 事件（F8）
- 池原生 publisher 有 removed 事件，但 0.3.11 不含、且载荷只说 key 怎么了、不说在哪台机器（F14，E8 已核）——升级后首选索引侧直接订阅解析（D1 §2 方案 b），kvcm 翻译重发为备选
- 选路方进程形态与 D4 耦合；E11-E14 依赖设计定型，E10/E11 骨架可先行

**D5. 故障衔接** — ✅ 已闭环

- 事实：copy 期间源实例掉线即失败（master 把任务派给源属主，D1 F4）。
- 事实：源属主宕机后，在途任务标记由 master 的定期清理线程（reaper）清掉（`master_service.cpp:10654` 附近）；清理完成前重发同一 copy 会收到 `OBJECT_HAS_REPLICATION_TASK` 报错（幂等语义见 D1 F11-F12）。
- 半成品副本的清理：✅ 已核实（两版本同行为）
  - 半成品副本 = copy/move 已把数据写到目标、但还没标完成的副本
  - master 的定期清理线程（reaper）发现任务超时（TTL = `put_start_release_timeout_sec_`）后，按任务记录的 `replica_ids` 精确丢弃这些半成品：
    - key 还有其它完成态副本（copy 场景的源副本）→ 保留 key，只丢半成品
    - 半成品是唯一副本 → 整个 key 删掉
  - 证据：`master_service.cpp:10653-10700` @main（清理动作写操作日志，主备切换后可恢复）；`:3259-3309` @0.3.11（注释原话「Discard expired CopyStart/MoveStart operations」）
  - 目标 seg 上占的物理内存：副本从 master 元数据丢弃后，由引擎自己的内存管理回收
- 故障重路由衔接：✅ 已定（既有原则的直接推论）
  - 请求侧不等：按 D1 §4 的兜底链走——副本还在池里就远端读，kvcm 出故障不放大成请求失败
  - KV 侧动作：kvcm 按 intent 对新目标实例重新预热（同一 intent 重发不会重复搬，P2 幂等键）
  - 没有预设的降级步骤表：路由由 Router 按最新集群状态重新选

## 二、工程项

**E6. 0.3.11 API 子集核对**——✅ 已闭环（结论写在 D2 F13）

- 做法：三个版本的 RPC 注册表 diff（v0.3.11 与 v0.3.11.post1 完全相同）
- 0.3.11 已有：lease / offload / promotion 心跳取任务、Admin 查询三件套、`EvictDiskReplica`、`BatchExistKey` 续租（D1 F15）、soft pin（D2 F9）
- main 才有：`ProbeKey`/`BatchProbeKey`、零副作用 Admin 查询（`GetReplicaListForAdmin`）、租户配额五件套、KV 事件、容量快照、`UnmountLocalDiskSegment`
- `default_kv_lease_ttl` 测试床实测 11s（D2 F9）
- 缺口清单 = D2 F13⑥（per-key 降层 RPC、copy/move 取消、key→属主查询）——这些就是要改 mooncake 池侧的工作量

**E8. mooncake kv_event schema 核对**——✅ 已闭环（结论写在 D1 F14）

publisher 含 stored/removed/cleared，**驱逐发 removed**；载荷 = key+medium+tenant+group，**无 seg/机器位置**；ZMQ 传输；CMake 默认 OFF、官方 wheel 构建 ON。**0.3.11.post1 不含此机制**（publisher 代码在 v0.3.11 之后进仓，tag 上无 `kv_event/` 目录、无构建开关）——D1 路径 b 要用须升级 mooncake 版本。

**E9. vLLM 事件载荷核对**——✅ 已闭环（结论写在 D1 事实基线 F7-F10）

- `BlockStored` 含 `medium` 字段
- 两个发布源：引擎发的 GPU 块事件（含 Removed，上游既有）、连接器发的池 put 事件（无 Removed）
- 预复制不经过引擎，结构上就没有事件——只能靠控制面补发
