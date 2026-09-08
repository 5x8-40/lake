# 模型级路由(Model Router)调研

调研对象:OpenAI / Anthropic / Databricks 等厂商的模型路由产品,以及开源实现与论文。
目的:给 Dynamo / lake 的实例级 Router 找可借鉴的机制。结论在最后一节。

## 先分清两类路由

| | 模型级路由(model routing) | 实例级路由(instance routing) |
|---|---|---|
| **决策** | 这个请求发给哪个模型、哪家 API、哪种 agent harness | 确定模型后,发给哪个 worker 进程 |
| **目标** | 质量与成本的权衡(便宜模型能答就不调贵的) | 缓存命中与负载均衡(谁存着前缀 KV、谁空闲) |
| **典型位置** | gateway / 产品层 | 推理系统内部 |
| **代表** | OpenAI GPT-5 router、Databricks Smart Routing、RouteLLM | Dynamo Router、lake Router |

两层独立存在,可以串联:gateway 的模型路由先选模型,推理系统的实例路由再选 worker。

```mermaid
flowchart LR
    C[客户端] --> G["模型级路由(gateway 层)<br/>选模型 / harness / 档位"]
    G --> R["实例级路由(推理系统内)<br/>选 worker:Dynamo Router / lake Router"]
    R --> W1[worker 0]
    R --> W2[worker 1]
```

## 厂商现状

### OpenAI:GPT-5 的 real-time router

GPT-5 不是单个模型,而是一个系统:快速模型 `gpt-5-main` 答大多数问题,深度推理模型 `gpt-5-thinking` 处理难题,前面放一个**实时路由器**决定用哪个([GPT-5 System Card](https://openai.com/index/gpt-5-system-card/))。

- **路由依据**:对话类型、复杂度、工具需求、显式意图(用户写 "think hard about this" 就强制走推理模型)。
- **训练方式**:路由器持续用真实线上信号训练——用户手动切换模型的行为、回答偏好率、实测正确率。
- **兜底**:用量超限后由 mini 版接管剩余请求。
- 不对外暴露:API 里仍是显式指定模型,路由器只在 ChatGPT 产品内工作。

### Anthropic:无官方路由

Anthropic 没有模型路由产品。Claude Code 里是用户手动 `/model` 选择。生态位由社区项目 [claude-code-router](https://github.com/musistudio/claude-code-router)(约 3.6 万 star)占据:本地代理拦截 Claude Code 请求,按**场景规则**分流——`background`(后台任务→便宜模型)、`think`(规划模式→推理模型)、`longContext`(超阈值→长上下文模型)、`webSearch`、`default`。纯规则,无学习成分。

### Databricks:Smart Routing + Omnigent(任务级,选模型也选 harness)

2026 年发布,Beta 状态,文档见 [Smart Routing for coding agents](https://docs.databricks.com/aws/en/ai-gateway/smart-routing),设计细节见官方博客 [Smart Routing in Unity AI Gateway](https://www.databricks.com/blog/smart-routing-unity-ai-gateway-match-frontier-quality-30-lower-cost-task)。面向编程 agent,要点:

1. **任务级而非请求级**。任务开始时定一次模型和 harness,整个会话不再换。原因很直接:大规模下成本由 prompt cache 命中率主导,逐请求换模型会把命中率打没,省的钱不如丢的多。
2. **分类器要便宜**。用一个低延迟小模型读任务描述和元数据,打几个语义标签:改系统的哪部分、提示词带什么代码证据(片段/报错栈/无)、失败形态、改动是否局部、项目类型。由此得出任务族和语言族。
3. **默认中等,双向调整**。路由器默认选中档模型,按标签向上升档(需要前沿能力)或降档(任务简单)。一个策略覆盖整个模型谱系。
4. **模型和 harness 联合选择**。harness(决定每轮发多少上下文、何时调工具、何时压缩上下文)对成本的影响可以超过 2 倍,只换模型不换 harness 拿不到这部分。联合选择由 Omnigent(元 harness,编排多个编程会话)执行;子 agent 启动时独立再过一次路由——初始 prompt 往往欠定义,子任务边界更清晰,路由更准。
5. **效果**:内部 workload 省 35%,公开 benchmark 省 56%,质量追平单用 Opus 5。

博客还写了四个后续方向,都很实在:

- **先做任务边界免费的场景**:PR 评审、子 agent、批量迁移、定时任务——任务描述由机器生成、一开始就完整,不用猜。
- **晚几轮再路由**:首轮 prompt 是信息最差的决策点;让便宜模型先聊几轮澄清需求,任务成形后再路由。
- **会话做小**:一个会话一件事,主题变了就开会话,路由更准也更便宜。
- **换模型要在缓存失效点**:会话中途换模型意味着 cache miss,今天这太贵;压缩(compaction)事件天然在丢缓存,是换模型的低成本时机(提到 Cognition Devin Fusion 就这么做)。长期目标是路由层把 cache miss 显式计价。

### OpenSquilla:开源 agent 的轮次级路由(选模型只是 harness 的一环)

[TokenRhythm/opensquilla](https://github.com/TokenRhythm/opensquilla)(Apache 2.0,基元律动)是微内核架构的开源 AI agent,模型路由是它的省钱手段之一,实现为 **SquillaRouter**:本机运行的 LightGBM + ONNX 分类器,按长度、语言、代码片段、关键词加语义嵌入给**每一轮**打分,分派到 C0–C3 四档里能胜任的最便宜模型。分类在本机完成,提示词不出本机。技术报告《OpenSquilla: Token-Efficient Agent = Models + Routing Harness》([aiXiv 260822.000001](https://aixiv.science/abs/aixiv.260822.000001),[中文 ChinaXiv 202608.00176](https://chinaxiv.org/abs/202608.00176));另有一篇讲 harness 原生路由数据飞轮的 [arXiv 2607.11399](https://arxiv.org/abs/2607.11399)。

与 Databricks 对照,它的差异点:

1. **粒度更细:轮次级**。Databricks 是任务级(任务开始定一次,保缓存);OpenSquilla 每一轮都重新选模型。轮次级能省更多(报告数据:保留固定旗舰模型 99.96% 的任务质量,成本降 88.9%;PinchBench 25 任务上与 OpenClaw+Opus 4.7 同分 0.925,成本 $0.688 vs $6.233),代价是频繁换模型会丢 prompt cache——它的解法是 **prompt 缓存隔离**(按档位隔离缓存命名空间)加自适应提示词(简单轮次连系统提示都换轻量版,缓存代价同步缩小)。两种粒度谁更优,取决于 provider 侧缓存价格与命中形态,没有通用答案。
2. **路由之外还有集成**。难题不只路由给一个模型,而是**分发给多个候选模型再聚合作答**(mixture-of-agents 思路),报告声称在深度研究任务上以 Fable 5 的 31% 成本拿到更高分;带成本感知回退——单模型够用时自动跳过集成。
3. **思维深度分级**:简单轮次直接关闭推理(reasoning)输出,不为"你好"付推理 token 的钱。

### OpenRouter:auto → auto-beta(市场信号)

API 聚合商,`openrouter/auto` 自动选模型。2026 年 8 月换掉原 NotDiamond 引擎,新机制自称 "wisdom of the market"([公告](https://openrouter.ai/blog/announcements/introducing-the-new-auto-router/)):把 prompt 分到约 30 类任务,按**全平台最近 7 天开发者真实消费份额**(周 55T+ token)给该类任务选模型——开发者集体在用脚投票,路由器跟着迁移。用户用 `cost_tier`(low/medium/high/xhigh/max 五档)控制价格带;多轮对话传 `session_id` 保持模型粘连。不收路由费,按选中模型原价计费。

反例:[Martian](https://www.linkedin.com/pulse/martian-vs-openrouter-optimization-trap-vidhi-vashishth-ney8c) 是最早做模型路由的创业公司,已从路由器转型。教训值得记:模型选择是开发者最在乎、也最难验证对错的决策(你永远看不到"另一个模型会怎么答"),纯黑盒自动路由难以建立信任;OpenRouter 先把接入、计费、failover 做好,自动路由只作为可选项。

### vLLM Semantic Router(开源)

[vllm-project/semantic-router](https://github.com/vllm-project/semantic-router):Envoy 的外挂处理器(ext_proc),Rust(Candle/ONNX)跑分类器、Go 对接 Envoy。内置一组 ModernBERT 分类器作为**信号**(意图/领域、PII、越狱、幻觉、反馈),信号经布尔规则组合成路由决策,落到配置的模型池;论文见 [When to Reason(arXiv 2510.08731)](https://arxiv.org/html/2510.08731v1)(按"需不需要推理"分流,MMLU-Pro 上延迟与 token 消耗减半且精度不降)与信号驱动框架论文。另有工作把分类链路本身优化了 98 倍([arXiv 2603.12646](https://arxiv.org/html/2603.12646v1))——佐证"路由器的开销必须远小于省下的钱"。

### LiteLLM / Portkey 等 AI 网关

路由策略是负载均衡与容错型(least-busy、最低延迟、成本上限、顺序 fallback),不做"这个请求哪个模型答得好"的质量预测。与本文主题的模型级路由是近邻但不同类。

## 学术工作

### 成本-质量路由主线

| 工作 | 年份/出处 | 机制 | 备注 |
|------|----------|------|------|
| FrugalGPT([arXiv 2305.05176](https://arxiv.org/abs/2305.05176)) | 2023,Stanford | **级联**:先调便宜模型,DistilBERT 给回答打分,不够再升级更贵的 | 最高省 98% 成本;级联与路由的区别:级联是串行试错,路由是一次决策 |
| HybridLLM([ICLR 2024](https://arxiv.org/abs/2404.14618)) | 2024 | BERT 预测查询难度,小/大模型二选一 | 难度预测路线的代表 |
| RouteLLM([arXiv 2406.18665](https://arxiv.org/abs/2406.18665),[lm-sys/RouteLLM](https://github.com/lm-sys/RouteLLM)) | 2024,LMSYS | 用 Chatbot Arena 偏好数据训练四种路由器(矩阵分解/加权 Elo/BERT/causal LLM);**阈值标定**控制强模型调用比例 | MT-Bench 上省 85% 成本、保持 95% GPT-4 质量;开源、模型数据在 HuggingFace |
| GraphRouter([ICLR 2025](https://arxiv.org/abs/2410.03834)) | 2025 | 任务-查询-模型建异构图,路由=边预测 | 利用任务间结构信息 |
| Avengers([arXiv 2408.12683](https://arxiv.org/abs/2408.12683)) | 2024 | 查询聚类,每类选最优小模型 | 不训练神经网络也有竞争力 |
| 综述([arXiv 2603.04445](https://arxiv.org/html/2603.04445v2)) | 2026 | 路由/级联统一分类 | 入门地图 |

### 评测:方法多,有效的少

- [RouterBench](https://arxiv.org/abs/2403.12031)(2024):11 个模型 × 7 个任务,40 万条预计算输出,路由策略离线评测的事实标准。
- [LLMRouterBench](https://aclanthology.org/2026.findings-acl.1881.pdf)(ACL 2026 Findings):40 万实例、21 个数据集、33 个模型,统一重测 10 个代表性路由方法。结论值得全文引用:**多数方法在统一评测下拉不开差距;若干商业路由器跑不过"永远选最好单模型"这个基线;与理论上限(Oracle)的差距主要来自"该升档的没升"**。Embedding 模型的选择影响有限,模型池越大收益越递减。

### 旁支:预测输出长度用于调度(与实例级路由直接相关)

实例级路由的负载项需要知道"这个请求会占多久",而 decode 长度事先未知。这一支工作专门解决这个问题:

| 工作 | 机制 | 效果 |
|------|------|------|
| SSJF([arXiv 2404.08509](https://arxiv.org/abs/2404.08509),LMSYS) | BERT-base 代理模型预测输出长度,投机式最短作业优先 | 平均完成时间降 30-40%,吞吐 2.2-3.6× |
| ELIS([arXiv 2505.09142](https://arxiv.org/abs/2505.09142)) | BGE 编码器预测长度 + 最短剩余时间优先 | 平均完成时间降 19.6% |
| PARS([arXiv 2510.03243](https://arxiv.org/abs/2510.03243)) | 成对排序(learning-to-rank)预测相对长度,vLLM 实现 | 优于 FCFS 与既有 SJF 变体 |
| TIE([arXiv 2604.00499](https://arxiv.org/pdf/2604.00499)) | 预测长度**分布**而非点估计,按尾部风险惩罚长请求 | 每 token 延迟再降 2.9×(对 SSJF) |

## APC 命中率:harness 纪律与实例级亲和调度

模型级路由之外,还有一条围绕 **APC(automatic prompt caching,前缀缓存)命中率**的线索,横跨 harness 设计和实例级调度两层。这条线决定了路由策略的约束条件。

### harness 侧:Anthropic 的 Claude Code 经验

[Lessons from building Claude Code: Prompt caching is everything](https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything)(2026-04,Anthropic 官方)。核心事实:prompt 缓存是**前缀匹配**——前缀里任何一处变了,其后全部失效。Claude Code 的整个 harness 围绕这条约束设计:

- **提示词排布:静态在前,动态在后**。系统提示与工具定义(全局共享)→ CLAUDE.md(项目级)→ 会话上下文(会话级)→ 对话消息。他们踩过的坑:在静态系统提示里放了精确时间戳、工具定义顺序不稳定、改了工具参数,都会打破缓存。
- **更新走消息,不改提示词**。时间、文件变更这类易变信息,塞进下一轮 user 消息或工具结果里,而不是改系统提示。
- **会话中途不换模型**。缓存按模型隔离:对话进行到 100k token 时,哪怕问题很简单,换便宜模型也比留在贵模型上更贵——要重建整个前缀缓存。要换模型就用子 agent,让主 agent 写交接消息。
- **会话中途不增删工具**。工具定义在缓存前缀里。Plan Mode 的实现方式是把 `EnterPlanMode`/`ExitPlanMode` 本身做成工具,工具集恒定;大量 MCP 工具用 `defer_loading` 发轻量占位(名字+标记),模型需要时再加载完整 schema,占位恒定所以前缀稳定。
- **压缩(compaction)用缓存安全的 fork**:压缩请求复用父会话完全相同的系统提示、上下文和工具定义,把压缩指令作为新的 user 消息追加在末尾——对 API 来说这几乎就是父会话的上一次请求,前缀缓存全部命中。
- **把缓存命中率当可用性指标监控**:命中率掉太多就当事故(SEV)处理。几个百分点的命中率波动对成本和延迟影响巨大。

这篇博客从 provider 侧解释了为什么 Databricks 选任务级路由:换模型的真实成本是缓存重建,不是 token 单价差。

### 调度侧:vLLM production-stack 的缓存亲和路由

vLLM 项目下除引擎外还有 [vllm-project/production-stack](https://github.com/vllm-project/production-stack)(K8s 上的分布式部署栈),其请求路由器(`vllm_router`)专为 APC 命中率设计,`routing_logic.py` 里实现了多种策略:

| 策略 | 机制 | 适用 |
|------|------|------|
| `roundrobin` | 轮询 | 负载均匀,无缓存亲和 |
| `session` | 按请求头里的 session id 粘连;无 session id 时选 QPS 最低者 | 多轮对话 |
| `prefixaware` | 路由器本地维护前缀树,同前缀永远发同一实例——**即使缓存已被驱逐** | 前缀形态稳定 |
| `kvaware` | 经 **LMCache controller** 集中查询各实例实际持有的前缀,路由到最长命中的实例;低于 `--kv-aware-threshold`(默认 2000 token)或不命中则回退 QPS/轮询 | 缓存状态实时可见 |
| `disaggregated-prefill` 系列 | PD 分离部署的 prefill/decode 分流 | PD 分离 |

`kvaware` 与 `prefixaware` 的差别值得注意:前者查的是**实例真实持有的缓存**(LMCache controller 集中记账),后者查的是**路由器自己的历史记录**(发过的就以为还在)。驱逐之后,后者会把请求发到已经没有数据的实例上。

### 调度侧:SGLang 的会话粘连与缓存感知策略

SGLang 的路由组件(`sgl-model-gateway`,Rust)的策略列表在 `src/policies/` 下,与 APC 相关的两个:

- **`cache_aware`**(`cache_aware.rs` + `tree.rs`):路由器为每个 worker 维护一棵**近似前缀树**(存原始文本而非 token id,省 tokenize 开销),按请求历史推断各 worker 的缓存内容,不查引擎真实状态。匹配率超阈值就发最匹配的 worker,否则发树最小(缓存容量最空)的;后台 LRU 驱逐叶子防内存膨胀。**负载不均衡时**(max-min 超绝对阈值且 max/min 超相对阈值)自动切到最短队列优先,均衡时切回缓存感知——缓存亲和与负载均衡按系统状态二选一,不是加权求和。
- **`consistent_hashing`**(`consistent_hashing.rs`):会话粘连。优先级:显式 `X-SMG-Routing-Key` 请求头 > 隐式稳定头(`authorization` / `x-forwarded-for` / `cookie`)> 匿名请求随机。一致性哈希环保证 worker 上下线时只有少量会话换节点。

### 三种实例级缓存亲和方案对照

| | 缓存状态来源 | 会话粘连 | 负载处理 |
|---|---|---|---|
| Dynamo Router | worker 主动发 KV 事件,router 建全局索引(权威在 worker) | 无专门机制(靠前缀命中自然实现) | 与命中项加权和进同一 cost 函数 |
| vLLM production-stack `kvaware` | LMCache controller 集中记账 | `session` 策略可叠加 | 不命中回退 QPS/轮询 |
| SGLang `cache_aware` | router 本地近似树,按历史推测(无权威) | `consistent_hashing` 策略 | 失衡时整体切换策略 |

lake 的对照:缓存状态由存储池权威维护(强于三家的"推测/记账"),会话亲和靠前缀命中自然获得;可借鉴的是 SGLang 的**失衡切换**(负载极不均时缓存亲和让位)和 production-stack 的**命中阈值**(短请求不做亲和查询,直接负载均衡)。



## 跨调研反复出现的四个结论

1. **路由粒度受缓存约束**。逐请求换模型/换实例都会破坏缓存命中:Databricks 因此选任务级,OpenRouter 提供 `session_id` 粘连,Anthropic 从 provider 侧给出原因(换模型=重建整个前缀缓存),SGLang/production-stack 用一致性哈希和 session 策略做粘连。"换档要在缓存失效点做"是共同的纪律。OpenSquilla 的轮次级路由是反例,但它用缓存隔离+自适应提示词把换档代价本身改小了——粒度之争的实质是缓存代价之争。
2. **判断必须便宜**。没有任何一家拿前沿模型当路由器:Databricks 用小模型打标签,vLLM-SR 用 ModernBERT,RouteLLM 用矩阵分解/BERT,OpenSquilla 用本机 LightGBM。路由器成本必须远小于它省下的钱。
3. **评测比方法难**。benchmark 任务太规整,真实会话首轮 prompt 欠定义(Databricks 原话);LLMRouterBench 显示大量发表方法无效。任何路由策略上线前都要用真实 trace 回放评测。
4. **缓存命中率是一等运维指标**。Anthropic 把命中率下跌当事故(SEV)处理;harness 的提示词排布、工具集恒定、压缩 fork 都是围绕命中率的设计纪律。推理系统侧同理:命中率应进 SLO 与告警,而不只是性能计数器。

## 与 Dynamo Router 对照

Dynamo Router 是实例级路由([分析见 dynamo/overview.md](dynamo/overview.md) "Router" 节):cost = prefill 负载 × 调整后 prefill 块数 + 预计 decode 块数 + 权重 × 在途请求数;缓存信号来自 worker KV 事件,负载信号来自本地记账,权重手工设定。

从模型级路由借鉴,可做的方向:

1. **decode 长度预测进 cost 函数**。cost 里的 `potential_decode_blocks` 目前是粗估;SSJF/ELIS/PARS 证明轻量预测器(BERT 级)可行且收益明确。预测输出长度还能辅助执行模式选择:预计 decode 很短的请求倾向混部,长的倾向 PD 分离。**lake 可做**:Router 挂一个可选预测器,先用历史请求离线 replay 验证,不进关键路径。
2. **代价权重在线学习**。RouteLLM 证明路由器可以从反馈数据训练;Dynamo 有 FPM 指标回路(每次前向的结构化指标),可用观测到的 TTFT/ITL 对代价权重做闭环调整。用 bandit 级别的方法就够,不需要 RL;先在仿真里跑(Dynamo 侧对应物是 AISimulate/DynoSim)。
3. **难度信号跨层传递**。模型级路由按 lake 的职责划分归 gateway,不在推理系统内实现;但 gateway 判出的难度/任务类型可以作为请求元数据传下来,推理系统用它做调度分级和预放置决策。这与 KVCR hint 协议同构:hint 传 KV 位置,这类元数据传请求属性,都是"上层知道得多、下层执行"的单向传递。
4. **换档代价显性计价**。Databricks 的核心工程结论——路由决策要把 cache miss 计入成本;Anthropic 从 provider 侧给出量化直觉(100k token 会话换便宜模型反而更贵)。lake 的执行模式选择函数里 D-direct / PD 分离的传输与重算代价已是显式项,这条已对齐;后续若做"会话中途换档"(如长会话压缩后重选模式),同样要在失效点做并计价。
5. **评测先行**。RouterBench 式离线回放 + 仿真,优于直接上线调参。lake 已有 agentic workload 的 trace 分析([agentic-cache-workload.md](agentic-cache-workload.md)),可作为路由策略的 replay 输入。
6. **亲和策略的工程细节**(来自 production-stack 与 SGLang):短请求设命中阈值,不做亲和查询直接负载均衡;负载严重失衡时缓存亲和整体让位(SGLang 的双阈值切换);会话粘连用一致性哈希,worker 上下线只迁移少量会话。lake 的亲和信息比三家都强(存储池权威视图,非推测),这些阈值与切换逻辑可以直接移植。

不照搬的:

- **级联逐档升级**(FrugalGPT 式):质量层机制,职责在 gateway;与 lake "故障不设降级链"不冲突(那是故障处理),但也不在推理系统内做。
- **语义相似度选模型**(embedding 路由):实例级路由要的是精确的块命中,语义相近不等于 KV 可复用;语义缓存在提示词层的复用是另一个课题,不在 Router 内。

## 参考链接

- OpenAI:[GPT-5 System Card](https://openai.com/index/gpt-5-system-card/)、[Introducing GPT-5](https://openai.com/index/introducing-gpt-5/)
- Databricks:[Smart Routing 博客](https://www.databricks.com/blog/smart-routing-unity-ai-gateway-match-frontier-quality-30-lower-cost-task)、[产品文档](https://docs.databricks.com/aws/en/ai-gateway/smart-routing)
- OpenRouter:[Auto Router 公告](https://openrouter.ai/blog/announcements/introducing-the-new-auto-router/)、[文档](https://openrouter.ai/docs/guides/routing/routers/auto-router)
- OpenSquilla:[GitHub](https://github.com/TokenRhythm/opensquilla)、[技术报告](https://aixiv.science/abs/aixiv.260822.000001)([中文](https://chinaxiv.org/abs/202608.00176))、[数据飞轮论文 arXiv 2607.11399](https://arxiv.org/abs/2607.11399)、[官网](https://opensquilla.ai/zh/)
- Anthropic:[Prompt caching is everything](https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything)
- 开源:[lm-sys/RouteLLM](https://github.com/lm-sys/RouteLLM)、[vllm-project/semantic-router](https://github.com/vllm-project/semantic-router)、[vllm-project/production-stack](https://github.com/vllm-project/production-stack)([KV-aware routing 文档](https://docs.vllm.ai/projects/production-stack/en/vllm-stack-0.1.11/use_cases/kv-cache-aware-routing.html))、[musistudio/claude-code-router](https://github.com/musistudio/claude-code-router)、[LiteLLM](https://github.com/BerriAI/litellm)
- SGLang 调度源码(本地 `3rdparty/sglang/sgl-model-gateway/src/policies/`,[GitHub](https://github.com/sgl-project/sglang/tree/main/sgl-model-gateway/src/policies)):`cache_aware.rs`(近似前缀树+失衡切换)、`consistent_hashing.rs`(`X-SMG-Routing-Key` 会话粘连)、`tree.rs`
- 论文:FrugalGPT [2305.05176](https://arxiv.org/abs/2305.05176) · HybridLLM [2404.14618](https://arxiv.org/abs/2404.14618) · RouteLLM [2406.18665](https://arxiv.org/abs/2406.18665) · GraphRouter [2410.03834](https://arxiv.org/abs/2410.03834) · RouterBench [2403.12031](https://arxiv.org/abs/2403.12031) · LLMRouterBench [ACL 2026](https://aclanthology.org/2026.findings-acl.1881.pdf) · 路由综述 [2603.04445](https://arxiv.org/html/2603.04445v2) · When to Reason [2510.08731](https://arxiv.org/abs/2510.08731) · SSJF [2404.08509](https://arxiv.org/abs/2404.08509) · ELIS [2505.09142](https://arxiv.org/abs/2505.09142) · PARS [2510.03243](https://arxiv.org/abs/2510.03243) · TIE [2604.00499](https://arxiv.org/abs/2604.00499)
