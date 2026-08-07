# 成本模型 v1 — KV 传输 vs 计算（P7.2）

三模式逐请求选路（[`../features/features.md`](../features/features.md)「执行模式」）的量化输入：
**传 KV 多久 vs 重算 prefill 多久**，据此给决策树阈值。模型代码 [`../../bench/cost_model.py`](../../bench/cost_model.py)，本文数值由 `python3 bench/cost_model.py` 生成。

> 形态对齐 SGLang HiCache prefetch 预算（base + per_token 线性；`hiradix_cache.py::prefetch_from_storage` 的 `timeout = min(max, base + per_ki_token·n/1024)`，见 [`../research/sglang/hicache.md`](../research/sglang/hicache.md)）。关键差异：SGLang 的公式是**单条路径的超时预算**；本模型是**三条路径（PD 分离/混部/D-direct）之间的分界**——reference 无对应物，结构自拟。

## 模型

```
T_transfer(H) = base_t + H · kv_bytes_per_token / BW      # 传 H token 的 KV
T_prefill(N)  = base_p + N · t_c                          # 重算 N token
分界 H*       = (base_t − base_p) / (t_c − b/BW)          # t_c ≤ b/BW 时不存在(重算恒胜)
```

命中按 block 粒度向下取整（128 token/块 = 复用最小单位），实际生效命中 = `⌊H/128⌋·128`。

## 参数

| 参数 | 值 | 来源 |
|------|----|------|
| `kv_bytes_per_token` | 114,688 B（28 层 × 2(K,V) × 8 kv_heads × 128 head_dim × fp16） | Qwen3-0.6B HF config（[`../research/transformers/overview.md`](../research/transformers/overview.md)） |
| `t_c`（prefill/token） | 200 µs | SLO draft 锚定：prefill > 5000 tok/s/GPU（[`../features/slo.md`](../features/slo.md)） |
| decode/step | 500 µs | SLO draft 锚定：decode > 2000 tok/s/GPU |
| `base_t` / `base_p` | 2 ms / 1 ms | **待真机占位 TODO-P7-hw**（形态借 SGLang prefetch base） |
| `BW` | 扫描 0.1–100 GB/s | 覆盖 loopback TCP → 单 NIC RDMA → 多 NIC 聚合（Mooncake transfer-engine 实测法，[`../research/mooncake/transfer-engine.md`](../research/mooncake/transfer-engine.md)） |

## 结论

### 1. 分界带宽 ≈ 0.57 GB/s：数据中心网络下「传 KV」几乎恒胜

| 有效带宽 | 每 token 传输 | H*（传划算的最小命中） |
|---------|--------------|----------------------|
| 0.1 GB/s | 1147 µs | N/A（重算恒胜） |
| 0.5 GB/s | 229 µs | N/A（重算恒胜） |
| 1 GB/s | 115 µs | 11.7 token |
| 5 GB/s | 23 µs | 5.6 token |
| 10–100 GB/s | 1–12 µs | ≈ 5 token |

`t_c = 200µs/token` 对 `b/BW`：BW > 0.57 GB/s 时传输斜率更平，H* 迅速收敛到 ≈5 token（被 base 差主导）。
**block=128 量化后 H* 远小于一个块** → 实践规则简化为：**≥ 1 个块命中且 BW ≥ 1 GB/s → 传 KV（PD 分离）；< 1 块 → 重算（混部）**。只有带宽掉到 sub-GB/s（拥塞/跨 AZ/对象存储直连）时重算才恒胜。

### 2. 模式矩阵（@10 GB/s, block=128）

| prompt | 池命中 25% | 池命中 50%+ | 本地命中 ≥1 块 | 无命中 |
|--------|-----------|------------|---------------|--------|
| 512 | PD 分离（传 3.5ms ≪ 算 27ms） | PD 分离 | **D-direct** | 混部 |
| 2048 | PD 分离（7.9 vs 103ms） | PD 分离 | D-direct | 混部 |
| 8192 | PD 分离（25 vs 411ms） | PD 分离 | D-direct | 混部 |

本地命中（前缀 KV 已在执行节点 HBM）零传输，任何 ≥1 块的本地命中都直跳 D-direct；
D-direct 的残差 prefill 与 P 侧同算力成本，省掉整段传输——**D-direct 的约束是节点负载而非传输成本**（利用率维度归 Router 负载信号，不在本模型）。

### 3. block 粒度（64/128/256）

量化损失三者同量级（300 token 命中各损 44）；最小传输单元时延 2.7/3.5/4.9ms（@10GB/s）。
**128 维持默认**：64 不改善量化损失（管理开销翻倍），256 抬高最小命中门槛且单块传输逼近 5ms 的 D-direct 选择开销预算。待 P7.3 用真 workload 碎片率复核。

### 4. 对 SLO 预算的回填

- TTFT（PD 分离）中 KV 传输占比：2048 token 全命中传 7.9ms，相对 P50<400ms 预算 ≈2%——**传输不是 TTFT 瓶颈，prefill 计算才是**（103ms）。
- D-direct 模式选择开销 <5ms 预算与传输节省（≥3.5ms/块）同量级——**选路必须 µs 级**（P6.3 已落地：纯内存读镜像），否则吃掉首块收益。

### 5. 分层加载 vs 重算（决策 A：L3 截断阈值）

命中下层后「加载进 L0 还是放弃直接重算」按**层介质成本**逐段判定（前缀链式，只能截短复用前缀、尾部重算，不能跳块）：

| 层 | 剖面（base + per-token） | T*（加载划算的最小命中段） | 实践规则 |
|----|--------------------------|----------------------------|----------|
| L1（池化 DRAM，RDMA） | 0.5ms + 2.3µs/tok | <0（恒加载） | **任意命中即加载** |
| L2（池化 NVMe） | 2ms + 23µs/tok | 5.6 tok ≪ 1 块 | **任意命中即加载** |
| L3（对象存储） | 20ms + 115µs/tok | **222.7 tok → ≥2 块（256 token）** | 命中段 <2 块 → 截断重算；≥2 块 → 批量异步预取 |

层剖面为待真机占位（TODO-P7-hw），但结论形态稳健：L1/L2 per-token 成本较 prefill（200µs）低 1–2 个数量级，固定开销也小，**加载恒胜**；只有 L3（RTT 主导）存在真实分界。

**与 SGLang 互证**：派生阈值 ≈256 token 恰与 SGLang 硬编码 `prefetch_threshold=256`（`hiradix_cache.py::prefetch_from_storage`，L3 命中段短于阈值则放弃预取直接重算，见 [`../research/sglang/hicache.md`](../research/sglang/hicache.md)）同值——reference 的拍脑袋魔数被本模型从介质成本推出，互为印证。关键差异：SGLang 阈值全局硬编码；我们按层派生、随真机剖面校准。

**与决策 B 的正交性**（churn 抑制，见 [`kv-cache-pool.md`](kv-cache-pool.md)「P7.3 校准结论」）：本节回答「本次用不用」（成本判定）；「用完留不留」（promote 后待遇）由 hit_count 准入判定。L3 命中段 ≥2 块才加载，加载后是否给热块待遇仍看 hit_count。

### 6. 带宽输入的三层结构（sub-GB/s 细分，issue #68 条目 4 定稿 2026-08-05）

分界带宽是连续函数（§1，交叉点 ≈0.57 GB/s），文档阈值用 1 GB/s 静态保守值。若部署环境掉到 sub-GB/s（跨 AZ/拥塞），静态阈值会持续指挥错误的「传」决定（每请求 ~2x）。定稿三层结构（参考 Mooncake transfer-engine 账本位置 + DualPath 静态/动态分工，见 [`../research/mooncake/transfer-engine.md`](../research/mooncake/transfer-engine.md)、[`../research/dualpath.md`](../research/dualpath.md)；关键差异：DualPath 信号是闲/忙利用率，本决策是成本比较需连续带宽，升级为 EWMA；Mooncake 测量实例本地不进成本模型，此处提为池全局视图喂模式选择）：

- **Layer 0｜静态能力表（部署时，兜底）**：按路径类分档（同机/同 AZ/跨 AZ/L3 直连），真机部署用 Mooncake 基准测量法（多 NIC 聚合实测）校准一次。一切动态机制失效时的回退。
- **Layer 1｜数据面被动测量（P5 真字节路径，信号源）**：池传输引擎对每次传输记 `(bytes, duration, 路径类)`，产出 per-路径类有效带宽 **EWMA**——样本从真实传输免费来，**不做主动探测**（探测耗所测资源，且空闲路径恰在最需要信号时无信号）。多 NIC 聚合/重试在数据面内部吸收，控制面看到的是已被数据面优化过的数。
- **Layer 2｜控制面带约束消费（Router 模式选择）**：池发布带宽视图（搭 `ReportLoad` ack 现有反向通道）；Router 读视图替代写死的 1 GB/s，三条约束：
  1. **滞回带**：视图值在阈值附近 ±band 内维持原决策——H* 的定义决定此处传/算等代价，翻转纯属抖动；
  2. **分钟级慢更新 + clamp**（SGLang 预算公式 min/max clamp 同款思想）；
  3. **用途限定抓离谱错误**：视图值与静态表偏离 >2x 才翻转默认决策（0.3 GB/s 当成 10 GB/s 的每请求 2x 错误才是目标）。

**失效语义**：视图缺失/过期/冷启动无样本 → 回退 Layer 0，任何时刻都有答案。**一个视图两个消费者**：§5 的 L3 加载阈值（`TIER_LOAD_PROFILES`）将来也读同一视图（L3 直连同为 sub-GB/s 高发路径）。**宏观兜底**：持续低带宽的终极响应是扩容（聚合带宽随节点数增长）+ 部署拓扑修正，运行时自适应只管瞬态与配置离谱。

**当前落地**：原型 mock 传输无真样本，Layer 1/2 归 P5；模式选择的带宽输入已改为 per-路径类配置字段（`go/router/modeselect.go`，PR #69，静态表先喂，P5 换数据源）。

## 阈值回填（features.md 执行模式节，v1）

| 条件 | 阈值（v1，mock 环境） |
|------|----------------------|
| 本地命中 ≥ 1 block（128 token） | D-direct |
| 池命中 ≥ 1 block 且路径有效带宽 ≥ 1 GB/s | PD 分离 |
| 命中 < 1 block 或带宽 < 1 GB/s | 混部（重算） |
| L1/L2 命中段 | 任意长度即加载（加载恒胜） |
| L3 命中段 < 2 block（256 token） | 截断重算（不预取） |
| L3 命中段 ≥ 2 block | 批量异步预取（不进同步 promote） |

带宽 < 1 GB/s 的细分（H* 随带宽漂移）留真机校准；v1 用 1 GB/s 作保守分界（H*≈12 ≪ 128；真实分界连续——BW > ~0.6 GB/s 时 1 块命中即可传，见 `cost_model.py::mode_for` 口径注释）。

## 局限（defer）

- `base_t`/`base_p` 为占位值；H* 对 base 差敏感（真机校准后可能漂 ±1 个块量级，不改变「≥1 块即传」的实践规则）。
- 无 GPU 计算实测：`t_c` 锚定 SLO draft 而非真 kernel；batch 效应（prefill 吞吐随 batch 变化）未建模。
- 传输按单流有效带宽；多 NIC 聚合、collective 突发窗避让（D→P 路径）待真机拓扑。
