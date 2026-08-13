# CLAUDE.md

本文件给 Claude Code（及任何接手的开发者/AI）提供 lake 仓库的工作指引。
先读 [`docs/00-plan.md`](docs/00-plan.md) 了解全貌与当前阶段，再读本文件了解**怎么做**。

## 这是什么仓库

lake 是一个**探索性**仓库，目标是设计并验证一套**彻底的存算分离**大模型推理系统。
所有有状态物（权重、KV cache、调度队列）从算力路径剥离，算力节点可随时销毁/拉起。

**当前阶段**：P4（KV Pool 原型）基本收敛，P5（存算分离验证）正在推进；P6/P7 为后续弹性调度与性能校准。
仓库现在是**文档 + 三语言原型并行**：Rust 存储层、Go 请求控制面、Python 计算层骨架已就位；`src/` 仅保留早期单进程兼容原型。

## 文档结构（务必遵守分类）

```
docs/
├── 00-plan.md          # 路线图主线（阶段、任务、状态），改阶段进度只动这里
├── features/           # P0：做什么（特性 / SLO / 非功能）
├── architecture/       # P1：怎么搭
└── research/           # 相关工作
```

- 特性 → `features/`，架构 → `architecture/`，文献 → `research/`。不要把新文档堆在 `docs/` 根。
- 文档间用**相对路径**链接（如 `[../features/features.md]`），跨目录引用要带对路径。
- 改动设计后，检查所有相关文档的内部链接是否仍有效（grep 旧路径）。

## 已确立的设计原则（改动前必须遵守，如要推翻需与用户确认）

1. **混合执行模式**：请求不固定走 P→D。Router 按 `f(请求, 集群状态) → (模式, 节点)` 在三种模式间逐请求选路：
   - **PD 分离** / **混部**（同节点完成）/ **D-direct**（前缀 KV 已被存储池放置在执行节点 HBM，零/极小传输直跳）。
   - **关键区分**：Pool 命中（前缀 KV 在分布式池，省重算但需传输）≠ 本地命中（前缀 KV 已被存储池放置在某执行节点 HBM，可 D-direct，零/极小传输）。
   - 混部与 D-direct **不违背存算分离**——KV 仍归存储池权威、写回 Pool；分离的是"存储与计算"，不是"P 与 D 必须分机"。
   - 详见 [`docs/features/features.md`](docs/features/features.md) "执行模式"节。

2. **失败处理不设降级链**：执行失败（故障/超时）→ 触发 F4 故障恢复重新路由 → Router 依最新状态重选模选点。
   不写 mode-to-mode 的预设 fallback 阶梯。模式选择是纯函数，失败即重跑该函数。

3. **过载控制归 gateway，推理系统只管执行**：
   - 限并发、拒请求、按优先级丢弃、准入控制 → gateway / 外部控制面。
   - Worker **不得**为保 SLO 自行降 batch size 或丢请求。
   - 推理系统的过载职责是**上报信号**（队列长度、in-flight、剩余容量），供 gateway 决策。
   - 可用性 SLO 仅针对"已准入、非过载"请求；过载拒绝不计推理系统失败率。
   - 详见 [`docs/features/slo.md`](docs/features/slo.md) "过载控制"节、[`docs/features/nonfunctional.md`](docs/features/nonfunctional.md)。

4. **存储池是长期存续、模型无关的独立基础设施，并统一管理全部分层**（F11 + F3）：
   - 同一池同时承载多个 `(model_id, revision)` 的 KV/权重，可对接任意模型；模型上下线与池生命周期解耦。
   - 池不解释张量布局，按**不透明字节块**存取 → 接入新模型只需注册 `model_id` 命名空间。
   - **统一编址 L0–L3**（层 = 介质，不是位置）：GPU HBM(L0) → DRAM 池化(L1) → NVMe 池化(L2) → 对象存储(L3) 全部由存储池统一管理放置/驱逐/副本/冷热/生命周期。计算节点不拥有任何内存——HBM/DRAM/NVMe 是池的物理载体，不是 worker 私有状态。因此不存在计算层私有的易失缓存（如 APC），所有 KV 位置均为存储池权威元数据。DRAM/NVMe 各是一层、一个池，block 放本机还是远端 KV Node 由池放置决定（同 L0 放哪个节点 HBM），不按位置拆层。L2(NVMe) = F4 恢复点，L3 = SSOT。
   - **冷热与生命周期**：L0/L1 做缓存副本、L2/L3 间按移动、L3 永久权威；冷热按"引用数>0 冻结 + 热度分(LFU-Aging) + 前缀亲和"判定；迁移主动为主（按热度 promotion/demotion + L0 预放置）+ 被动兜底（读 miss 回填/写满驱逐）；迁移/GC/碎片整理共享后台带宽池（<10%）。
   - **放置与 batch 职责边界（方案 Z）**：存储池按热度主动预放置 KV 到 HBM 并发布位置视图；调度器读视图组 batch（本地命中优先→D-direct，缺失补拉），不反向指挥放置。单向耦合。
   - 具备：按模型配额（软/硬 + 借用）、池容量扩缩（一致性哈希最小迁移）、GC（冷块/孤儿块/级联删/旧 revision）、碎片整理（逻辑共置 + 物理压实，后台节流可暂停）。
   - 触硬配额 → 池返回**写入背压信号**上传，请求级 shedding 仍归 gateway。
   - 详见 [`docs/architecture/kv-cache-pool.md`](docs/architecture/kv-cache-pool.md)。

5. **技术选型（已定，P2 落地）**：

   | 层 | 语言 |
   |----|------|
   | 存储层（KV Pool / Weight Cache / Tiered Store） | Rust |
   | 存储控制面（位置视图权威 / radix / 配额·GC / etcd checkpoint） | Rust |
   | 请求控制面（Router，含集群级调度，无独立 Scheduler） | Go |
   | 计算层（Prefill / Decode / Draft） | Python + Triton |
   | 元数据 | etcd · SSOT 用 S3/MinIO · 跨语言 RPC 用 gRPC+Protobuf（大块 KV 走 RDMA 旁路） |

   目录划分见 [`docs/00-plan.md`](docs/00-plan.md) P2 节。`src/` 是早期单进程原型，**不要**在三语言子项目就位后继续往 `src/` 加功能。

## 3rdparty 参考源码（submodule）

`3rdparty/` 以 git submodule 引入项目源码,作为设计与实现的直接参考:

| 路径 | 来源 | 主要参考点 |
|------|------|-----------|
| `3rdparty/sglang` | sgl-project/sglang | **HiCache**:L1/L2/L3 分层、HiRadixTree(节点记 KV 位置)、prefetch/write-back 策略、page-first 布局、计算-传输重叠;**计算层**:Worker→ModelRunner、spec decode(DSPARK 仅此有 / DFLASH / MTP / EAGLE,drafter 共置串行、`PoolName.DRAFT`)→ 见 [`docs/research/sglang/model-runner.md`](docs/research/sglang/model-runner.md) |
| `3rdparty/mooncake` | kvcache-ai/Mooncake | **transfer-engine**(RDMA 零拷贝)→ Transfer Bus;**mooncake-store** → KV Pool(L3) |
| `3rdparty/lmcache` | LMCache/LMCache | 跨请求/跨实例 KV 复用、多存储后端、`rust/` 工程模式 |
| `3rdparty/vllm` | vllm-project/vllm | **计算层**:PagedAttention、worker/`GPUModelRunner`、`KVConnectorBase_V1` 接口(存算分离接入点)、spec decode |
| `3rdparty/transformers` | huggingface/transformers | **模型定义参考**:Qwen3/PyTorch `nn.Module` 结构、HF config 字段、decoder/model/causal-lm 分层；只作模型骨架与配置对照,不作服务端执行参考 |
| `3rdparty/dynamo` | ai-dynamo/dynamo | **编排层/控制面**:KV-aware router、KVBM(GPU→CPU→SSD→远端 三层 offload)逻辑/物理/引擎三层、Rust 编排、多后端通信(etcd/nats/tcp/zmq) |
| `3rdparty/tilert` | tile-ai/TileRT | **超低延迟 decode**(tile runtime,核闭源) + **vLLM PD 插件**(`TileRTConnector`/`pd_vllm`,NIXL/Mooncake)→ 见 [`docs/research/tilert/`](docs/research/tilert/) |
| `3rdparty/memcache` | Ascend/memcache | **昇腾分布式 KVCache 对象池**(MetaService/LocalService、HBM/DRAM/SSD、MemFabric OneCopy)→ 见 [`docs/research/memcache/`](docs/research/memcache/) |
| `3rdparty/ucm` | ModelEngine-Group/unified-cache-management | **统一缓存框架**(可插拔 KVStore、vLLM connector、稀疏插件、PD-via-pool)→ 见 [`docs/research/ucm/`](docs/research/ucm/) |

逐层对应、借鉴点与**关键差异**(我们更彻底:L1/L2 也归存储池而非实例私有)见 [`docs/research/3rdparty-reference.md`](docs/research/3rdparty-reference.md)。各项目的深度分析见分目录:`docs/research/{sglang,lmcache,mooncake,vllm,dynamo,tilert,memcache,ucm}/`；Transformers 仅作为模型定义源码参考。

约定:
- `3rdparty/` **只读**,不修改 submodule 内代码。要改造先 fork 换 URL。
- submodule 自带 `.claude/` 规则——改它们自身代码的约束,与本项目无关,**忽略**。
- clone 本仓库需 `git submodule update --init --recursive`。体积较大时可用浅克隆:`git clone --recurse-submodules --depth 1 --shallow-submodules <repo>`(浅克隆后无法在 submodule 内随意 `checkout` 切 ref)。Ascend MemCache 的传输底座 `memfabric_hybrid` 为**嵌套** submodule——文档推荐的 recursive clone/update **会**一并拉下;仅非 recursive 初始化时才不拉。审计 OneCopy 时需确认该路径已 init。TileRT 公开树相对较小。
- 设计/实现遇到分层、传输、复用、放置等问题,先查对应 submodule 源码再动手。

## reference 强制查阅规则（硬性，每次都做）

**每一次**技术讨论（设计决策、机制选型、数据结构/接口设计）或代码修改，**必须**先查阅 `docs/research/` 下的实现参考，并在回复中**显式**告诉用户：reference 里有什么值得参考的地方、参考了什么、按哪段代码回溯。这是硬约束，不是建议。

### 怎么查

1. **先定主题**：本次讨论涉及哪个机制（分层缓存 / KV 传输 / 前缀复用 / 放置调度 / 后端抽象 / HA / GC…）。
2. **按主题定位文档**：
   - 分层 + radix 节点记位置 + prefetch/write-back 策略 + 内存布局 + 计算-传输重叠 → `docs/research/sglang/{overview,hicache,storage-backends}.md`
   - **计算层(SGLang)**:Worker→ModelRunner + drafter 适配 + 与 vLLM 对照(V2/dummy、DP/TP/PP 控制面) → `docs/research/sglang/model-runner.md`
   - block 生命周期(何时释放/降层/彻底放弃,现状 vs 未来) → `docs/research/sglang/block-lifecycle.md`
   - 上游 issue/roadmap 痛点与 lake 对照 → `docs/research/sglang/pain-points.md`
   - **Agentic 分布式 KV 总设计**(SGLang #21846：增量 PD、Host 直传、UnifiedRadix、agent hint) → `docs/research/sglang/agentic-kv-roadmap.md`
   - **Guided / structured decoding**(xgrammar 库边界、overlap/async 同步、spec+grammar) → `docs/research/guided-decoding.md`
   - **Sampling 参数对照**(SGLang × vLLM;`n`≠beam;spec 兼容;penalty 空泡;状态归属与前缀共享) → `docs/research/sampling-params.md`
   - **Scheduler→Worker 字段**(vLLM `SchedulerOutput` × SGLang `ScheduleBatch`/`ForwardBatch`、架构根因) → `docs/research/scheduler-worker-interface.md`
   - 跨实例复用 + 多存储后端 + 内容寻址 + 控制器元数据 + Rust 裸设备 I/O → `docs/research/lmcache/{overview,sharing-and-backends}.md`
   - RDMA 零拷贝传输 + 多 NIC 聚合 + 对象级 KV store + 分配策略 + HA → `docs/research/mooncake/{overview,transfer-engine,kv-store}.md`
   - **昇腾 KV 对象池**(MemCache):Meta/Local、HBM/DRAM/SSD、MemFabric OneCopy → `docs/research/memcache/{overview,architecture,pain-points}.md`（与 Mooncake store 同层对照；非 radix 控制面）
   - **统一缓存框架(UCM)**:可插拔 KVStore、vLLM connector、PD-via-pool、稀疏插件 → `docs/research/ucm/{overview,architecture,pain-points}.md`（引擎插件层；与 LMCache 同层对照）
   - **计算层(vLLM)**:PagedAttention/worker/model runner + KV connector 接口(worker↔存储池接入点) + spec decode + 权重加载 → `docs/research/vllm/{overview,compute}.md`
 - **模型定义(Transformers)**:Qwen3 `nn.Module`/HF config/decoder/model/causal-lm 分层 → `docs/research/transformers/overview.md`
   - **vLLM Q3 KV/Session 调度**(#48168 agent prefix · #48501 `session_id`/`continuation_id` · retention) → `docs/research/vllm/kv-session-roadmap.md`
   - **编排层/控制面**:KV-aware router(overlap 量化) + KVBM logical/physical/engine 三层 offload + Placement/StorageTier(介质非位置) + 链式 block 哈希 + 多后端通信(etcd/nats/tcp/zmq) → `docs/research/dynamo/overview.md`
   - **NVIDIA CMX**(整栈：GPU + Dynamo Router/KVBM + NIXL + DOCA Memos 双端 KV 设备 + STX 盘框；NVMe-KV 128-bit key；VAST 做 DPU 上的存储软件/DASE，不是新盘；软件切口与 DPU 侧算法见同文) → `docs/research/nvidia-cmx.md`（公开材料+GTC S81773/S82255+Dynamo/NIXL 源码；仿真 `tools/cmx-sim/`）
   - **超低延迟 decode / vLLM PD 插件**(TileRT):connector claim、MTP-aware 传 KV、NIXL/Mooncake → `docs/research/tilert/{overview,pd-vllm,pain-points}.md`（核闭源,不作存储面参考）
   - 跨项目逐层对应与借鉴顺序 → `docs/research/3rdparty-reference.md`
3. **沿代码回溯**：每个参考文档末尾都有「代码索引」节，把概念/机制映射到 `文件:符号`。符号名是稳定锚点（行号会漂移，找不到时 `grep -n "符号名" 3rdparty/<repo>/<文件路径>`）。需要确认实现细节时，直接读对应符号的源码。

### 必须在回复里给出的内容

- **参考了哪些实现**：点名项目 + 具体 `文件:符号`（至少一条，能定位到代码）。
- **值得参考的点**：从这些实现里能直接借鉴什么（API 形态、数据结构、算法、布局、策略、失败处理…），并说明**为什么**对当前讨论有用。
- **关键差异**：reference 这么做，我们要不要照搬？哪里更彻底 / 哪里要改造 / 哪里是它们的局限我们需另设计（如 HiCache L1/L2 实例私有 vs 我们归存储池；Mooncake store 无内容寻址 vs 我们 radix；LMCache 无全局强一致元数据 vs 我们 etcd 强一致）。

### 示例措辞

> 参考实现：SGLang HiCache `hiradix_cache.py::prefetch_from_storage` + `can_terminate_prefetch`。
> 值得参考：timeout 预算公式 `base + per_ki_token * num_tokens/1024` 可直接用于我们 prefetch 预算模型；三策略（best_effort/wait_complete/timeout）对应我们"被动兜底读 miss 回填"的终止语义。
> 关键差异：SGLang 实时查后端 `batch_exists`（弱一致），我们由 Rust 存储控制面进程内存维护强一致位置视图（etcd 只存降频 checkpoint），一跳命中，省掉每次访问的 RPC。

不涉及任何参考实现时（纯本项目内部讨论、无对应参考）也要**显式说明**「本项无直接参考实现」，而不是省略——省略会被当成漏查。

## SLO 是架构硬约束

SLO 数值是 draft（待 P7 校准），但**约束关系是硬的**：TTFT/ITL/冷启动等目标倒逼架构取舍（如 D-direct 模式选择开销 < 5ms，否则吃掉本地命中省传输的收益）。
改架构设计时，对照 [`docs/features/slo.md`](docs/features/slo.md) 检查是否仍满足 SLO 预算。

## 工作约定

- **职责边界优先**：遇到"某能力归谁"的问题，先判断是否越界（推理系统只管执行 vs. gateway 管过载；F4 管故障恢复 vs. Router 管重决策）。宁可少做，不要越界。
- **文档先行**：新增能力先写进 `features/` 或 `architecture/`，再考虑代码。代码服务于验证文档里的假设。
- **保持文档一致**：改了一处设计，同步更新所有引用它的文档（features ↔ architecture ↔ plan），并在 `00-plan.md` 勾选状态。
- **链接要活**：移动文档用 `git mv` 保留历史；移动后 grep 修复所有内部链接。
- **不自行决定技术选型**：语言/框架/存储选型由用户定。新增依赖或换语言前问用户。
- **职责外的事不擅自做**：过载 shedding、鉴权计费等外部控制面职责，不在推理系统内实现。

## Git 约定

- **主仓在 GitLab**:`https://gitlab.com/BeeBreeze/lake`(remote `gitlab`,2026-08 自 GitHub→GitCode→GitLab 迁入)。`origin`(GitHub)/`gitcode` 两个 remote 已停用,勿推送。
- **禁止直接推送到 `main`**。所有改动走 MR:在新分支上提交,推送后开 MR,经用户确认后再合并。
- **动手前先问用户是否建新分支**:开始实质改动前,主动询问用户是否要建新分支(若当前已在 `main`,默认应建)。不要自行直接在 `main` 上提交。
- 分支命名:用 `docs/...` / `feat/...` / `fix/...` 等前缀 + 简短描述(如 `docs/vllm-kv-roadmap-update`)。
- 提交信息中文,开头用 `docs(P0):` / `feat:` / `fix:` 等前缀,结尾附 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- 推送:`git push gitlab <branch>`(HTTPS,凭据由 git credential 管理)。
- 用户尚未配置全局 git 身份,仓库本地配置为 `witcher` / `witcher@users.noreply.github.com`,提交时用 `git -c commit.gpgsign=false commit`。
- MR/issue/流水线操作用 `glab` CLI(已认证 gitlab.com):`glab mr create` / `glab issue list` / `glab ci list`;合并由用户决定,不自行合并。
- CI:GitLab CI(`.gitlab-ci.yml`),main push 与 MR 触发;`.github/workflows/` 与 `.gitcode/workflows/` 为历史遗留配置,已停用勿维护。

## 原型运行

早期单进程原型（验证前缀复用逻辑，非生产）：

```bash
python3 -m src
# 期望输出：两个请求共享前缀，第二个命中 KV Pool（reused=3, prefill=1）
```

三语言子项目就位后此原型将被取代，勿在此基础上扩展。

## 三语言构建（P2 空壳已就位）

```bash
# Rust 存储层(workspace,tonic-build 在线生成 stub)
cd rust && cargo build
# Go 请求控制面(pb.go 入仓)
cd go && go build ./...
# Python 计算层(stub 入仓,worker 包可 import)
python3 -c "import lake; from lake.pb import lake_pb2; import lake.prefill, lake.runtime"
```

改 `proto/` 后重生成 Go/Python stub(入仓):`bash scripts/gen_stubs.sh`(工具链版本见脚本头注释)。CI(`.github/workflows/build.yml`)跑三语言 build + stub-drift 检查。

