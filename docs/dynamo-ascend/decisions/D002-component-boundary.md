# D002: 组件边界——Dynamo 全组件的适配动作划分

- 日期:2026-09-17
- 状态:已定(组件清单随上游演进需定期核对)

## 背景

分布式推理系统里三个名字都带"调度"的组件职责完全不同,混为一谈会导致适配范围失控(讨论中实际发生过):

| 组件 | 层级 | 职责 | 昇腾侧现状 |
|------|------|------|-----------|
| **Router** | 集群级控制面 | 收外部请求,按全局 KV 命中/负载/拓扑决定发给哪个实例 | vLLM/SGLang 没有此组件;Dynamo Router 提供 |
| **Scheduler** | 单实例控制面 | 实例内请求队列状态机、continuous batching、抢占 | vllm-ascend / SGLang NPU 内部已成熟 |
| **BlockAllocator** | 单实例内存底层 | KV Block 分配/回收、PagedAttention 页表 | 同上,引擎内部已成熟 |

## 决策一:不动引擎内部

适配红线:**不魔改 vllm-ascend / SGLang 内部的 Scheduler 与 BlockAllocator**。

- 跨实例 KV 移动由控制面协调器决策"何时搬、从哪搬到哪",但**不直接操作**目标实例的 BlockAllocator——通过引擎暴露的 connector / kv_transfer 接口或 RPC 触发实例内部分配与接收。
- 集群级路由是新增组件(Router),不下沉进引擎。

## 决策二:Dynamo 全组件的适配动作

Dynamo 不止 Router/Worker 两块。按上游 main 的组件清单(依据 [`../../research/dynamo/overview.md`](../../research/dynamo/overview.md) 核实),逐个定动作:

| 组件 | 干什么 | 动作 | 说明 |
|------|--------|------|------|
| **Frontend** | OpenAI 兼容 HTTP 入口,预处理(tokenize)/后处理 | **复用** | 纯 CPU 组件,只需 tokenizer 与模型配置,不碰权重,与硬件无关 |
| **Router**(KV-aware) | 按 KV 命中 + 负载选 worker,编排 PD 交接 | **复用** | Rust 实现,与硬件无关;"是否缺昇腾拓扑感知"待查,见 D001 修正 #2 |
| **DistributedRuntime** | 服务发现、端点注册、请求传输、生命周期 | **复用** | Rust 地基(`lib/runtime`),与硬件无关 |
| **基础设施**(etcd / NATS / ZMQ / TCP) | 发现面 / 事件面 / 请求面的通信栈 | **复用** | 三平面各自可插拔,均与硬件无关 |
| **Planner** | 双环自动扩缩(慢环预测定下限 + 快环救突发) | **复用逻辑,适配数据** | 扩缩逻辑不动;但快环依赖引擎每次前向发的 FPM 指标,vllm-ascend 侧要接上指标发布;容量估计需要 NPU 的 profiling 数据 |
| **Profiler / AIConfigurator** | 给 Planner 供容量数据(最优 TP 度、性能插值) | **适配数据** | 工具复用,需要 NPU 上的实测/估计数据 |
| **Worker 胶水层** | 把引擎包成 Dynamo worker(注册、发 KV 事件、暴露 RPC 端点) | **适配** | `dynamo.vllm` / `dynamo.sglang` 启动器要对接 vllm-ascend / SGLang NPU;环境变量、torch_npu 初始化、镜像、K8s 资源名等差异清单见 D001 |
| **NIXL** | 传输库,同一 API 搬 HBM/DRAM/SSD/远端,屏蔽互联差异 | **替换后端** | API 不动;昇腾传输首选 NIXL + Mooncake TE 后端(TE 已有昇腾实现),待实测,见 [../data-plane-options.md](../data-plane-options.md) |
| **KVCR** | KV 二级存储 + router hint 驱动 P2P(KVBM 的继任者) | **复用策略,替换后端** | 存储后端位接 Mooncake Store 或 memcache(选型中,见上) |
| **KVBM** | 旧 KV 管理器(G1-G4 分层 offload) | **不用** | 已被上游 sunset(DEP #11673),代码还在 main 但不再演进 |
| **ModelExpress** | GPU 间流式传权重,加速冷启动 | **待定** | NPU 间权重传输路径未评估;前期可用共享存储 + 本地下载兜底 |
| **Grove / Dynamo Operator** | K8s 部署:拓扑感知 gang 调度、按 Planner 期望副本数调和 | **适配** | 资源名从 `nvidia.com/gpu` 改为昇腾 NPU 资源标识;拓扑调度要认昇腾 UB 域 |
| **容错**(优雅退出 / 请求迁移 / canary) | worker 挂掉时在途请求带已生成 token 换机续算等 | **复用** | 实现在请求处理流水线层,与硬件无关 |
| **mocker / AISimulate** | 模拟引擎,不起卡也能压测路由与扩缩 | **复用** | 对开发期价值大:NPU 资源紧张时控制面可先行验证 |

## 决策三:引擎侧两条路都保留

Dynamo 的后端是可插拔的(vLLM / SGLang / TRT-LLM),昇腾侧对应两条现成的引擎适配路径:

| 引擎 | 昇腾适配在哪 | 现状 |
|------|-------------|------|
| **vllm-ascend** | 独立仓([vllm-project/vllm-ascend](https://github.com/vllm-project/vllm-ascend)),vLLM 的 platform 插件 | Mooncake 已官方集成:TE 做 PD 分离传输、Store 做分布式 KV 池——**数据面路径最短,建议先走这条** |
| **SGLang** | NPU 支持在 SGLang 主干(`python/sglang/srt/hardware_backend/npu/`,另有 `pyproject_npu.toml`、`docker/npu.Dockerfile`) | HiCache 分层缓存 + Mooncake L3 后端是现成组合;作为第二后端保留 |

## 后果

- 好处:复用面远大于适配面——真正要动的是 Worker 胶水层、NIXL 后端、KVCR 存储后端、K8s 部署层四处;引擎可跟随 vllm-ascend / SGLang 上游升级,调度语义出问题时归属清晰。
- 代价:跨实例 KV 搬运的触发点受限于引擎 connector 接口的能力;若接口不足,优先向引擎上游提接口,而不是在 fork 里挖洞。
- 后续:Planner 的 FPM 指标对接、NPU profiling 数据产出,是"复用逻辑"之外必须自己做的两件数据面以外的事,容易漏,列在这里防丢。
