# kvcm 代码架构（五层）

- 定位：kvcm 服务化（E10 重构 / E11 进程化 / E15 并行限速）的模块分层设计，E10 开工依据
- 本文**不新增机制结论**——操作语义、完成信号、幂等性、能力事实全部引自 [D1-router-scheduling.md](D1-router-scheduling.md) / [D2-kv-operations.md](D2-kv-operations.md)；本文只把它们落成代码结构
- 语言：Python（继承现有 `demo/precopy/` 1577 行与 mooncake store_py 绑定、keys.py 与引擎同口径；换语言属独立决策，不在本文）

## 分层总览

```text
L1 接口层     IntentReceiver / EventPublisher / InstanceWatcher
              进程对外通道；不含任何存储概念
L2 编排层     复合流程 = 原子操作的编排
              只调 L3 接口；代码里不出现 seg / lease / client_id / PoolKey / rank
L3 原子操作层 copy / move / keepalive / pin / demote / promote / retain / remove
              抽象接口（语义规格 = D2 §1）+ capability 声明
L4 适配层     两个正交维度，各一实现：
              StoreBackend（存储）MooncakeBackend = StoreClient + SegTopology
              EngineAdapter（引擎）VLLMAdapter = KeyComputer + RankTopology + 事件格式
L5 状态层     Ledger（sqlite，四类状态记录）
```

- 依赖方向只允许向下：L1 → L2 → L3 ← L4；L5 被 L2/L4 持有
- 核心思想：**原子操作屏蔽后端与引擎语义**——L2 编排对任何存储后端、任何推理引擎成立；换后端换 StoreBackend，换引擎换 EngineAdapter

## L1 接口层（进程对外通道）

| 模块 | 职责 | 出处 |
|------|------|------|
| IntentReceiver | 收 Router 预热 intent / 会话结束线索（E12；先占位，协议字段随 D4） | D1 §4 职责边界 |
| EventPublisher | READY 后按目标实例端点补发 `BlockStored`；端点生命周期（每实例一端点、注册给索引） | D1 §2 方案 a 落地、E14 |
| InstanceWatcher | 实例上线/销毁接入 → 触发预热 / seg 副本处理；「端点→实例」「seg→实例」注册的权威来源（E13；先占位，事件源随 D4） | D1 §3、E13 |

- 三条通道全部单向：intent 下行、事件上行、实例事件接入——与 D1 §4「两条单向通道」一致

## L2 编排层（复合流程）

复合流程由原子操作编排而成（D2 §1「复合流程表」即规格），一个流程 = 一段只调 L3 的编排代码：

| 流程 | 编排 | 出处 |
|------|------|------|
| PrefetchFlow | copy×N + 核验 + keepalive 注册 + READY 后触发补发 | D2 §1、D1 §4 |
| SessionEndFlow | 停 keepalive + demote | D1 §6 |
| DrainFlow | copy/move（或 seg 级批量 CreateDrainJob）+ 宽限期卸载 | D2 §4 计划下线四步 |
| RetainFlow | move 到活 seg（副本脱离将死引擎） | D2 §4 |

支撑组件（同进程内的常驻循环，E11 进程化的主体）：

- **TaskEngine**：任务状态机（排队 / 进行 / READY / 失败）+ 并发上限 + **限速器**——E11 的核心、E15 的落点（限速输入 = 网卡/RDMA 计数器采样的链路利用率，D1 §5）
- **KeepaliveLoop**：保活注册表驱动的周期续租（D3；机制 D2 F9，策略未定）
- **ReleaseLoop**：软释放 / demote / 主动 Remove 三级（D1 §6）
- **RetainLoop**：retain 与计划下线编排（D2 §4）

## L3 原子操作层（核心抽象）

- 接口规格直接抄 D2 §1：八项操作的语义、完成信号、READY 有无、幂等性都已定型——本层把它们落成 Python 抽象基类
- 八项分三类（D2 §1）：**原子动作**（copy / move / keepalive / demote / promote / remove）、**元数据标记**（pin）、**目的型**（retain——由 demote / move / pin 组合实现，对外仍呈现单步语义）
- 完成信号：任务类（copy/move）查任务状态 + 核验副本 → READY；READY 有三个（copy / move / promote），其余五个无（D2 §1 完成信号节）
- 幂等：控制面以（前缀 × 目标实例）为幂等键，在途不重复发、完成不重复发（D1 §4）；后端幂等兜底（D1 F11）

**capability 声明（显式，不藏在接口后面假装统一）**

- 版本口径：**目标基线 = mooncake main**——E10 落地按 main 填充能力、按 main 语义实现；0.3.11 列仅为当前部署（demo 测试床 wheel）约束记录，部署升级后该列即废弃
- 初版清单（每项都能指回 D2 事实条目）：

| 能力 | mooncake 0.3.11 | mooncake main | 出处 |
|------|----------------|---------------|------|
| has_lease（读续租） | ✓ | ✓ | D2 F9 |
| can_pin_after_write（事后加 pin） | ✗ | ✗ | D1 F5（pin 挂写路径） |
| can_copy_with_source（指定源复制） | ✗（move 才能指定源） | ✗ | D2 F7 |
| has_offload / has_promotion（池通道降层/提回） | ✓ | ✓ | D2 §3 |
| has_per_key_demote（确定性立即降层） | ✗ | ✗ | D2 §3 缺口 |
| has_task_cancel（copy/move 任务取消） | ✗ | ✗ | D2 §1 审计 P9 |
| has_retained_client（保留身份注册） | ✗ | ✓ | D2 F13② |
| has_dfs_replica（共享 3FS 副本） | ✗ | ✓ | D2 F13② |
| has_admin_query（零副作用核验） | ✗（`GetReplicaList` 顺带续租） | ✓ | D2 F11b |

- L2 按能力选路径：D2 §3 的写穿/写回分支、§4 的 retained client/DFS 前提，本质就是能力分支——本层把它落成显式声明
- 同一后端不同版本能力不同（0.3.11 vs main 已在表里）——capability 在适配器初始化时按版本填充，不写死在类上

## L4 适配层（两个正交维度）

适配分两个正交维度：**存储后端**与**推理引擎**——两者各自独立可换，各建一个适配器。L3 原子操作的实现同时持有两者（引擎侧算 key 与 rank 拓扑，存储侧执行 RPC）。

### StoreBackend（存储后端适配）

**MooncakeBackend**（首个实现）：

| 子件 | 职责 | 现有代码来源 |
|------|------|-------------|
| StoreClient | master RPC 封装（copy/move/exist/GetReplicaList/Remove/upsert…）+ client UUID 持久化（sqlite，D2 §2 方案一） | `demo/precopy/common.py` + `demo/precopy/precopy.py` 的 RPC 调用段 |
| SegTopology | seg→client/实例映射；seg、client_id、lease 语义 | `demo/precopy/resolve.py` 的 seg 侧（生产权威源 = L1 InstanceWatcher 注册，D1 §3/E13） |
| capability 填充 | 按后端版本填充能力声明（目标基线 = **mooncake main**，0.3.11 仅为当前部署约束记录） | 新增，L3 表驱动 |

- **seg 概念与 lease/client_id 语义归本层**：全是 mooncake 特有——留在通用层则抽象漏
- **第二后端候选已登记在案**：昇腾 MemCache（`3rdparty/memcache`；kv-prefetch.md 待定项「生产默认走 MemCache，还是留 Mooncake」）——L3 抽象现在建正是时候；但只建一层接口，第二后端真接入时再修接口，不为假想后端过度设计

### EngineAdapter（引擎适配）

**VLLMAdapter**（首个实现）：

| 子件 | 职责 | 现有代码来源 |
|------|------|-------------|
| KeyComputer | 前缀 → hash 链 → PoolKey（与引擎同口径） | `demo/precopy/keys.py`（lazy-import vllm-ascend `KeyMetadata`/`PoolKey` 保证同口径） |
| RankTopology | rank↔KV shard 形状（TP/PP/DP 切分；异构 TP 的 `decode_tp_size`/`prefill_tp_size` 语义）；rank↔seg 对号 | `demo/precopy/resolve.py` 的 rank 侧降为对号实现 |
| 事件格式 | 引擎 KV 事件（`BlockStored` 等）schema 的生成/解析 | EventPublisher 补发时按本格式构造 |
| engine config | block_size / num_kv_heads / head_size / 层数读取（KV 大小估算，E14 容量规划输入） | 新增 |

- **KeyComputer 归引擎侧，不归存储侧**——PoolKey 的权威定义在**引擎**（引擎写 KV 时就以此 key 落池，kvcm 只是按同口径找回）；mooncake 对 key 无格式要求（不透明字符串），后端不掺和 key 语义
- **引擎之间 key 计算不一样**：vLLM 是链式 block hash（`vllm/v1/core/kv_cache_utils.py::hash_block_tokens`，`prefix_caching_hash_algo` + `init_none_hash` 的 seed 折入、block_size、Mamba/MLA 模型类型均影响结果）；SGLang 是 `RadixKey` = token id 序列本身（`sglang/srt/mem_cache/radix_cache.py`）——连 key 形态都不同（hash 值 vs token 序列）。KeyComputer 放通用层或存储侧都是错位的
- **引擎强关联物全归本层**：key 计算、rank 拓扑、事件 schema、engine config——换引擎（如 SGLangAdapter）时 StoreBackend 一行不动

## L5 状态层

- **Ledger**（sqlite）：状态记录四类（D2 §1 定型）——任务状态机（可丢）/ 保活注册表（不可丢）/ 副本台账（半可丢，可从 master 重建）/ 会话绑定（不可丢）
- 部署形态：kvcm 单实例跟随 master（D2 §1）；上 HA 时再换 Redis/etcd

## 设计原则

1. **原子操作屏蔽后端与引擎语义**：L2 不出现 seg / lease / client_id / PoolKey / rank——这些词只允许出现在 L4
2. **能力显式，不假装统一**：后端差异走 capability 声明，L2 按能力分支；不在接口里塞进只有 mooncake 有的概念
3. **只建一层抽象**：L3 接口服务当前唯一后端（mooncake），第二后端（MemCache）接入时再修——不预判假想需求
4. **语义规格以 D1/D2 为准**：本文任何接口定义与 D1/D2 冲突时，改本文
5. **多实现方式：先落地一种，其余登记**：一个操作有多种实现方式时（D2 已有实例：retain = retained client / DFS 副本 / move 到活 seg / 自挂 SSD 兜底，§4；降层写穿 vs 写回分支，§3；copy 无指定源能力时以 move 替代，F7），**优先实现一种**，其余作为已知替代方式登记在 API 文档（能力前提、语义差异、补齐条件），后续按 capability 补齐——不并行铺开

## 与 E10 / E11 / E15 的对位

- **E10（重构）三步**
  1. 定义 L3 原子操作抽象基类 + capability 声明（把 D2 §1 八项落成接口）
  2. 现有 1577 行重组为 MooncakeBackend + VLLMAdapter（L4 两维度）+ Ledger（L5）——测试现成（`demo/tools/test_keys.py` / `demo/tools/test_resolve.py`）
  3. `precopy.py` 编排逻辑改写为 L2 PrefetchFlow（只调 L3）；L1/L2 其余模块出空骨架（类与接口先行）
- **E11（进程化）**：进程壳 + TaskEngine + Keepalive/Release/Retain 三个 Loop 常驻化；IntentReceiver / InstanceWatcher 占位接口随 E12/E13 填
- **E15（并行限速）**：TaskEngine 内的并发调度 + 限速器（链路利用率采样，D1 §5）——不独立做

## 参考实现

- **UCM 可插拔 KVStore**（[`../docs/research/ucm/architecture.md`](../../docs/research/ucm/architecture.md)）与 **LMCache 多存储后端**（[`../docs/research/lmcache/sharing-and-backends.md`](../../docs/research/lmcache/sharing-and-backends.md)）：后端抽象同构。关键差异：它们的抽象在**数据面**（connector get/put），本架构 L3 在**管理面**（copy/pin/lease/remove 控制操作）——管理语义无跨后端天然对应物，故 capability 必须显式
- **LMCache controller**（D2 §1 完备性审计同表）：控制面对外 = 命令集——L1 IntentReceiver 对应其命令入口
- **Dynamo standalone indexer**（`3rdparty/dynamo/lib/kv-router/src/services/indexer/`）：独立进程 + 契约接口的组件形态参照；本架构是 kvcm 单进程内部结构，组件拓扑已定（kvcm 独立控制面、索引归 Router 侧，D1 §4）
