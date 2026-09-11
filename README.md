# Lake — 彻底的存算分离推理系统

> 一个探索性仓库，目标是设计并验证一套**彻底**的存算分离（storage-compute disaggregated）大模型推理系统。

## 为什么是"彻底"

传统推理引擎把显存、KV cache、权重、调度器耦合在一个进程（甚至一张卡）里。这带来了：

- **扩缩容慢**：算力节点有状态，无法像无状态服务一样水平伸缩。
- **KV cache 利用率低**：cache 固化在产生它的节点上，前缀共享、跨请求复用、跨节点迁移都很难。
- **Prefill / Decode 互相干扰**：长 prefill 阻塞 decode，吞吐与延迟难以同时优化。
- **存储与算力绑定**：权重加载、checkpoint 恢复都依赖本地磁盘，冷启动代价高。

"彻底"意味着把**所有有状态的东西都从算力路径上剥离出去**：权重放到对象存储 + 内存池，KV cache 作为独立的分布式资源池，Prefill 与 Decode 拆成两个可独立伸缩的算力池，调度器只做无状态路由。算力节点理论上可以随时销毁、随时拉起。

更进一步，本系统**以 KV 为中心**设计，连 GPU HBM / 主机内存 / 本地 NVMe 都不归计算节点私有，而是存储池统一管理的物理载体（L0–L3，层=介质非位置）。**agent 多轮对话**是首要驱动场景：其"长共享前缀 + 逐步增长"结构使 KV 池化、前缀复用与反向回传增强的价值最大化（见 [`docs/architecture/execution-modes.md`](docs/architecture/execution-modes.md)）。

## 整体架构

```
                             ┌──────────────────────────┐
                             │   Bifrost/Router(Gateway)│
                             │     路由 + 模式选择      │
                             └────────────┴─────────────┘
                                          │ gRPC
              ┌───────────────────────────┴────────────────────────────┐
              ▼                                                        ▼
 ┌─────────────────────────┐                              ┌─────────────────────────┐
 │       计算节点 A        │                              │       计算节点 B        │
 │                         │                              │                         │
 │ 计算引擎 Python+Triton  │                              │ 计算引擎 Python+Triton  │
 │   (零分层, 只 replay)   │                              │   (零分层, 只 replay)   │
 │                         │                              │                         │
 │ in-process agent (Rust) │<────────────CNIC────────────>│ in-process agent (Rust) │
 │   本地视图镜像/组装表   │         L0->L0 RDMA          │   本地视图镜像/组装表   │
 └────────────┬────────────┘                              └────────────┬────────────┘
              │                                                        │
              │                     SNIC (南北向)                      │
 ┌────────────┬────────────────────────────────────────────────────────┬────────────┐
 │                      存储池 (统一管理 L0-L3, 层=介质非位置)                      │
 │             放置 / 冷热 / 生命周期 / radix 前缀索引 / 位置视图 / GC              │
 │                                                                                  │
 │           易失缓存（丢了回填）                     持久权威（恢复点 / SSOT）     │
 │┌────────────────┐  ┌────────────────┐      ┌────────────────┐  ┌────────────────┐│
 ││  L0  GPU HBM   │  │ L1  DRAM 池化  │      │ L2  NVMe 池化  │  │  L3  对象存储  ││
 ││  池放置/易失   │  │ 缓存副本/易失  │      │ F4 恢复点/持久 │  │   SSOT/永久    ││
 │└────────────────┘  └────────────────┘      └────────────────┘  └────────────────┘│
 │                                                                                  │
 │                        热 <── promotion / demotion ──> 冷                        │
 │                                                                                  │
 │  风险窗口: NPU/进程级故障 -> 从 L2 续推(丢少量 token); 整机级故障 -> 退 L3 SSOT  │
 │                                                                                  │
 └────────────────────────────────────────┬─────────────────────────────────────────┘
                                          │ 位置视图 / radix / ref
                     ┌────────────────────┬────────────────────┐
                     │    存储控制面 (Rust 进程内存, 强一致)    │
                     │ 位置元数据 / radix / 引用 / 拓扑 / 配额 │
                     │       etcd: 降频 checkpoint + lease     │
                     └─────────────────────────────────────────┘
```

- **存储池四层**（层 = 介质，不是位置）：HBM → DRAM → NVMe → 对象存储，逐 tier 池化、热冷 promotion/demotion。DRAM/NVMe 各是一层、一个池，block 放本机还是远端 KV Node 由池放置决定（同 L0 放哪个节点 HBM），不按位置拆层。L2（NVMe）= F4 恢复点（NVMe 持久 + NPU 故障不烧 NVMe，恢复能力与位置无关），L3 = SSOT；物理载体分布不同，但元数据全归存储池统一管理，计算节点不拥有任何一层。
- **in-process agent**：存储池在每个计算节点的本地端点（Rust `.so`，in-process）——组装 block table、发起/接收跨实例传输、注册本地内存、持本地视图镜像（零 RPC 决策）。全局权威（位置视图 / radix / ref 汇总）在 Rust 存储控制面进程内存（etcd 只存降频 checkpoint + lease），本地镜像是其推送副本。
- **KV 流转三方向**（以 KV 为中心，存储池不区分 P/D）：
  - **P→D** 服务本次（PD 分离正向：产出 → 消费）
  - **D→池** 服务未来（decode 延伸前缀反向回传，radix 生长）
  - **D→P** 服务下一轮（agent 多轮：decode 侧 KV 喂回 prefill，DualPath 原生支持）
  - **drafter KV 同款进池**：draft 模型自身 KV 与 target KV 同款管理——进存储池、跨请求前缀复用、随 target 迁移（不 L0-only）；seed hidden / 窗口状态按重算式暂不入 radix（见 [`docs/architecture/compute-layer.md`](docs/architecture/compute-layer.md) "投机解码"）。

> 详图与组件边界见 [`docs/architecture/overview.md`](docs/architecture/overview.md)；KV 流转时序见 [`docs/architecture/execution-modes.md`](docs/architecture/execution-modes.md)；双网络与 RDMA 退化见 [`docs/architecture/topology.md`](docs/architecture/topology.md)。

### 关键模块

| 模块 | 语言 | 职责 |
|------|------|------|
| **Gateway** | 外部(Bifrost) | 鉴权 / 限流 / 入口准入 / 过载 shedding（进/不进；去哪归 Router）；不自研，见 [`docs/architecture/control-plane.md`](docs/architecture/control-plane.md)「Gateway 对接约定」 |
| **计算层**（Prefill/Decode/Draft） | Python+Triton | 前向计算（graph replay），引擎零分层逻辑，只消费 ready→算→发 done |
| **in-process agent** | Rust | 存储池在计算节点的本地端点：组装 block table / 发起传输 / 注册本地内存 / 持本地视图镜像（零 RPC） |
| **存储池** | Rust | 统一管理 L0–L3：放置 / 冷热 / 生命周期 / radix 前缀索引 / 配额 / GC / 碎片整理 |
| **存储控制面** | Rust + etcd | 位置视图权威（进程内存强一致）/ radix / 引用汇总 / 节点拓扑 / 负载视图；etcd 只存降频 checkpoint + lease |
| **传输引擎** | Rust | 跨实例 KV 传输（RDMA 零拷贝），池 agent 发起；多 NIC 聚合 + TCP 退化 |
| **请求控制面** | Go | Router 无状态路由 + 模式选择（含集群级调度，无独立 Scheduler 进程），零 RPC 读本地命中视图镜像 |

### 关键特性

- **彻底存算分离**：L0–L3 全归存储池统一管理（层=介质非位置），计算节点不拥有任何内存，可随时销毁/拉起。
- **混合执行模式**：请求不固定走 P→D，Router 逐请求在 **PD 分离 / 混部 / D-direct** 三模式间选路。
- **前缀复用 + 本地命中**：radix 前缀索引 + 位置视图；本地命中（前缀 KV 已在某执行节点 HBM）→ D-direct 零/极小传输直跳。
- **反向回传 + D→P**：decode 延伸 KV 回传池生长 radix（服务未来）；agent 多轮里 D→P 直接喂回 prefill（服务下一轮），不绕一跳存储。
- **双网络隔离**：CNIC（计算）/ SNIC（存储）物理分离，两类带宽是池的资源；RDMA 三级退化在传输引擎内吸收、上层接口不变。
- **两级 ref + 持久语义分层**：本地引用计数（池 agent）+ 全局汇总（控制面）；L2(NVMe) = F4 恢复点（NVMe 持久 + NPU 故障不烧 NVMe，恢复能力与位置无关）/ L3 = SSOT（抗整机级/池级失败）。
- **失败不设降级链**：执行失败 → F4 重路由，Router 重跑选路纯函数，无 mode-to-mode fallback 阶梯。
- **过载归 gateway（Bifrost）**：限并发 / 拒请求 / 按优先级丢弃归外部控制面（Bifrost），推理系统只管执行 + 上报信号。

## 仓库定位

这是**探索与研究**仓库，不是生产系统。当前阶段以设计文档 + 原型代码为主，验证以下假设：

1. KV cache 池化后，前缀复用带来的收益能否覆盖跨节点传输成本？
2. Prefill/Decode 物理隔离后，P50/P99 延迟与吞吐的帕累托前沿是否真的外推？
3. 算力节点做到无状态后，秒级扩缩容是否可行，冷启动代价如何压缩？
4. 以对象存储为单一事实来源（single source of truth）时，权重/KV 的分层缓存策略如何设计？

## 路线图

先看 [`docs/00-plan.md`](docs/00-plan.md) —— 依次要做的事情与技术选型（存储+存储控制面 Rust / 请求控制面 Go / 计算 Python+Triton）。

**当前阶段**：P0 特性设计 ✅ → P1 架构设计 ✅ → P2 模块划分与技术选型（进行中）。

## 目录结构

```
lake/
├── README.md
├── docs/
│   ├── 00-plan.md              # 路线图与执行计划（主线入口）
│   ├── features/               # P0 特性设计 — 做什么
│   │   ├── goals.md            #   目标与非目标
│   │   ├── features.md         #   特性清单（Must/Should/Could）
│   │   ├── slo.md              #   SLO 与衡量指标
│   │   └── nonfunctional.md    #   非功能需求
│   ├── architecture/           # P1 架构设计 — 怎么搭
│   │   ├── overview.md         #   总体架构
│   │   ├── storage-layer.md    #   存储层（L0–L3 分层 / 冷热 / 池放置·调度读视图）
│   │   ├── compute-layer.md    #   计算层（HBM 池化入图 / KV 管理 / 投机解码）
│   │   ├── kv-cache-pool.md    #   KV cache 池（Block 寻址 / 跨实例传输 / ref / 双网络）
│   │   ├── scheduling.md       #   路由与调度（命中感知 / 在途再均衡）
│   │   ├── execution-modes.md  #   执行模式与 KV 流转时序（以 KV 为中心）
│   │   ├── data-flow.md        #   请求生命周期（决策树 / 三模式执行段 / F4 / D→P）
│   │   ├── consistency.md      #   一致性与故障模型（持久语义 / ref 两级 / 风险窗口）
│   │   └── topology.md         #   部署拓扑（双网络 / RDMA 退化 / 故障域）
│   └── research/               # 相关工作
│       ├── references.md            #   文献与参考总清单
│       ├── 3rdparty-reference.md    #   3rdparty 源码与本设计的逐层对应(汇总)
│       ├── hbm-tier-and-offload.md  #   HBM 归属与 KV 卸载路径对照
│       ├── distributed-models.md    #   各项目分布式模型对比
│       ├── serving-stack-comparison.md # Dynamo/FlexKV/llm-d/AIBrix 四栈对比
│       ├── dualpath.md              #   DualPath 双路径 KV 加载分析
│       ├── nvidia-cmx.md            #   NVIDIA CMX 目标栈与边界
│       ├── agentic-cache-workload.md #  Agentic cache 负载与仿真输入
│       ├── vllm_vs_sglang/          #   vLLM × SGLang 对比(PD 分离控制/guided decoding/采样参数/Scheduler→Worker 字段)
│       ├── sglang/                  #   SGLang HiCache 深度分析(+ 上游痛点)
│       ├── lmcache/                 #   LMCache 深度分析
│       ├── mooncake/                #   Mooncake 深度分析
│       ├── vllm/                    #   vLLM 深度分析（计算层参考）
│       ├── transformers/            #   Transformers（模型定义参考）
│       ├── dynamo/                  #   Dynamo 深度分析（编排层/控制面参考）
│       ├── kvcr/                    #   KVCR（KVBM 继任者）
│       ├── tilert/                  #   TileRT（超低延迟 decode + PD 插件）
│       ├── memcache/                #   Ascend MemCache（昇腾 KV 对象池）
│       ├── ucm/                     #   UCM（统一缓存框架）
│       ├── tensorcast/              #   TensorCast（张量状态基础设施）
│       ├── flexkv/                  #   FlexKV（引擎旁 CPU/SSD/远端卸载）
│       ├── kvcached/                #   kvcached（GPU 虚拟内存弹性 KV）
│       ├── aibrix/                  #   AIBrix（K8s 推理基础设施:网关路由/自动扩缩/KV 事件同步）
│       └── llm-d/                   #   llm-d Router（EPP 精确缓存感知 + PD sidecar）
├── 3rdparty/                   # 参考源码（git submodule，只读）
│   ├── sglang/                 #   SGLang（HiCache 分层 KV + spec decode 计算层）
│   ├── lmcache/                #   LMCache（跨实例 KV 复用）
│   ├── mooncake/               #   Mooncake（KVCache-centric 分离架构）
│   ├── vllm/                   #   vLLM（计算层：PagedAttention/KV connector）
│   ├── transformers/           #   Transformers（模型定义参考）
│   ├── dynamo/                 #   Dynamo（编排层：KV-aware router + KVBM 三层 offload）
│   ├── kvcr/                   #   KVCR（KVBM 继任者：引擎内二级存储 + P2P）
│   ├── tilert/                 #   TileRT（超低延迟 decode + vLLM PD 插件）
│   ├── memcache/               #   Ascend MemCache（昇腾分布式 KVCache 对象池）
│   ├── ucm/                    #   UCM（统一缓存框架：可插拔 KVStore）
│   ├── tensorcast/             #   TensorCast（张量状态基础设施层）
│   ├── flexkv/                 #   FlexKV（引擎旁 CPU/SSD/远端 KV 卸载）
│   ├── kvcached/               #   kvcached（GPU 虚拟内存弹性 KV：VA/物理页解耦 + 跨进程超卖）
│   ├── production-stack/       #   vLLM production-stack（实例级路由器参考，分析见 model-routing.md §5）
│   ├── aibrix/                 #   AIBrix（K8s 推理基础设施：网关路由策略集 + KV 事件同步）
│   └── llm-d-router/           #   llm-d Router（EPP：KV 事件→全局块索引 + 推测索引）
└── src/                        # 早期单进程 Python 原型（验证假设用，将被 rust/go/python 子项目取代）
```

> 首次 clone 后执行 `git submodule update --init --recursive` 拉取 `3rdparty/`。参考项目与本系统设计的逐层对应见 [`docs/research/3rdparty-reference.md`](docs/research/3rdparty-reference.md)。
>
> submodule 体积较大（SGLang/Mooncake/vLLM 各数百 MB）。磁盘紧张或想快速浏览时用浅克隆：
> ```bash
> git clone --recurse-submodules --depth 1 --shallow-submodules <repo>
> ```
> （浅克隆后无法在 submodule 内切换 ref，升级 submodule 需先 `git submodule deinit -f <path>` 再重新深克隆 init。）

## Python 开发环境（uv）

`uv` 仅用于开发期 Python 环境管理；Rust / Go 仍分别使用 `cargo` / `go`。从仓库根目录执行：

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

验证 Python 计算层：

```bash
.venv/bin/python -m pytest python/lake/engine/tests python/lake/runtime/tests
.venv/bin/python -c "import lake; from lake.pb import lake_pb2; import lake.engine, lake.runtime, lake.kernels"
```

需要 Torch / Triton 时再安装 CUDA extra：

```bash
uv pip install -e ".[dev,cuda]"
```

## 设计文档速览

- **路线图**：见 [`docs/00-plan.md`](docs/00-plan.md)
- **特性 / SLO / 非功能**：见 [`docs/features/`](docs/features/)
- **总体架构**：见 [`docs/architecture/overview.md`](docs/architecture/overview.md)
- **执行模式与 KV 流转**：见 [`docs/architecture/execution-modes.md`](docs/architecture/execution-modes.md)
- **存储层**：见 [`docs/architecture/storage-layer.md`](docs/architecture/storage-layer.md)
- **计算层**：见 [`docs/architecture/compute-layer.md`](docs/architecture/compute-layer.md)
- **KV cache 池**：见 [`docs/architecture/kv-cache-pool.md`](docs/architecture/kv-cache-pool.md)
- **调度**：见 [`docs/architecture/scheduling.md`](docs/architecture/scheduling.md)
- **数据流与请求生命周期**：见 [`docs/architecture/data-flow.md`](docs/architecture/data-flow.md)
- **一致性与故障模型**：见 [`docs/architecture/consistency.md`](docs/architecture/consistency.md)
- **部署拓扑**：见 [`docs/architecture/topology.md`](docs/architecture/topology.md)
- **相关工作**：见 [`docs/research/references.md`](docs/research/references.md)

## 状态

🚧 早期设计阶段。

## License

MIT
