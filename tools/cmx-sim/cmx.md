# NVIDIA CMX（Inference Context Memory Storage）

只根据 NVIDIA 公开材料拆 CMX 在 Vera Rubin 上是什么、补哪一层、数据怎么走。数字能钉的钉，钉不住的标成定性口径。不对照其它推理系统或存储方案。

可调公式与图在同目录仿真页：[`index.html`](index.html)（PD 分离 Prefill 跑满）、[`capacity.html`](capacity.html)（1M 会话在 G1/G2/CMX 驻留）。

## CMX 在补哪一层

NVIDIA 的判断：agentic / 百万 token 上下文把 KV 变成一种既要快、又要大、又不必按企业数据做耐久的东西。

- G1 HBM 太小太贵。
- G4 共享文件/对象是为不可重建数据准备的（副本、scrub、元数据），拿来扛热 KV 会拉高每 token 功耗、让 GPU 空转。
- G3 本地 SSD 能缓冲，但绑在单节点上，换 GPU、跨 agent 复用还得再搬一次。

CMX 的产品名在材料里也叫 Inference Context Memory Storage（ICMS）。它插的是 **G3.5**：以太网闪存、POD 级共享、专为 ephemeral KV。

公开定性口径是「每个 GPU POD 数 PB 共享容量」，用来在 HBM/DRAM 挤出之后仍保留可复用历史，并频繁 prestaging 回主机/GPU，避免 decode stall。

时间点：2026 H2 经制造/存储伙伴出货。对比基线写的是 traditional storage（通用 x86 企业存储栈），不是某一家推理引擎的实测。

## 在 Rubin POD 里的位置

Vera Rubin POD 是五类机柜拼起来的：

| 机柜 | 公开要点 |
|------|----------|
| NVL72 | 72 Rubin GPU + 36 Vera CPU，NVLink 6 |
| Groq 3 LPX | 推理加速柜 |
| Vera CPU | CPU 柜 |
| BlueField-4 STX | **CMX 住在这里** |
| Spectrum-6 SPX | 以太网交换 |

公开数字：约 40 机柜、1152 Rubin GPU。计算柜经 Spectrum-X 访问 STX，不是 NVL72 机柜里的本地盘。

STX 机柜：BlueField-4 = Vera CPU + ConnectX-9 SuperNIC，scale-out 走 Spectrum-X。计算节点上的 BlueField-4 在另一篇 Rubin 芯片文里写成 64 核 Grace + CX-9、最高 800 Gb/s——和 STX「Vera + CX-9」的表述并存。分析时把两者当成**同一代 DPU 家族的两个落点**（计算节点发起端 / 存储柜终止端），不要合成一颗芯片的一张规格表。

| 层 | Rubin 上的介质 | 这套 1M 负载下干什么 |
|----|----------------|----------------------|
| G1 | 288 GB HBM4 / GPU，22 TB/s | 正在算的 KV；Prefill attend、Decode 逐步扫描都在这里 |
| G2 | NVL72 柜内 54 TB LPDDR5X | 预取缓冲、HBM 溢出 |
| G3 | 节点本地 SSD | 单机暖 KV；别的 GPU 看不见 |
| G3.5 CMX | STX 以太网闪存 | POD 内共享的 1M 会话 / 共享前缀 |
| G4 | 对象/文件 | 要留下的冷历史，不在 decode 热路径 |

NVL72 单柜公开数字：约 20.7 TB HBM + 54 TB LPDDR5X；NVFP4 inference 50 PFLOPS/GPU。

## 数据路径

1. 计算柜 BlueField-4 做发起端：NVMe-oF / NVMe KV，线速 CRC 和加密，KV 不进主机 CPU 拷贝链。
2. STX 上的 BlueField-4 终止协议、读写闪存。
3. Spectrum-X 提供可预期时延的 RDMA。
4. NIXL 的职责是在计算开始前把命中块 prestaging 到 G2/G1。没有这条预取，CMX 就退化成稍快一点的 G4。

KV 在 CMX 上按可重建、短寿命处理：不做企业存储那套多副本和后台 scrub。块丢了就从 token 重算。热集靠预取留在 G1/G2。这是「5× 能效」叙事的前提，不是闪存介质本身 magically 省电。

## 技术点

### 1. KV 是单独数据类

相对权重和业务对象，KV 可重算、按会话生长、访问是多对多持续打满。CMX 的存储栈按这个假设裁掉耐久服务。公开博客原话：critical for performance yet inherently ephemeral。

### 2. G3.5 补的是共享，不只是容量

G1/G2/G3 随 GPU 线性涨，但 G3 绑节点。1M 会话要在多轮、多 agent、换 GPU 之后复用，必须有一层 POD 内大家都能 RDMA 到的闪存。插 G3.5 的原因是这个，不是「本地盘不够再买一块」。

### 3. 预取，不是同步 miss

1M 会话每轮预取是数 GB 到几十 GB。同步 miss 会让 GPU 空转。官方 5× TPS 建立在「预取成功、GPU 持续有活干」上。工作负载、命中率、ISL/OSL 都没公开。

### 4. DOCA 是 KV 的 I/O 面

Rubin 芯片文：DOCA 提供 KV communication and storage interfaces。闪存被呈现成 POD 级 key-value，不解释张量。完整性、加密、协议终止在 DPU。NVIDIA 写明对存储伙伴和更广编排开放。

### 5. Dynamo / NIXL 是编排，不是闪存前提

公开路径：Dynamo 管 KV 块在层次间的放置，NIXL 做传输。这是 NVIDIA 画出的控制面怎么用 CMX，不是 STX 机柜加电的物理前提。CMX 机柜 + DOCA 是存储面；没有 Dynamo 也可以有别的编排去调 prestaging，只是公开演示不是那样画的。

### 6. 官方 5× 在说什么

原文相对 traditional storage：通用 x86 存储栈、为不可重建数据准备的副本/元数据。STX 新闻稿另外还有「相对传统 CPU 存储架构 4× 能效、2× 摄入」——那是 STX 存储参考架构的另一条声明，不要和 CMX 的 5× TPS 混成一个数。都没有公开模型与负载。

## 公开数字 vs 没公布的

| 有 | 没有（不要当成数据表） |
|----|------------------------|
| NVL72：72 GPU，288 GB HBM4 @ 22 TB/s/GPU，柜 20.7 TB HBM + 54 TB LPDDR5X，NVFP4 inf 50 PFLOPS/GPU；POD ≈ 1152 GPU；CX-9 端点 1.6 Tb/s/GPU（部分表）/ BF4 800 Gb/s | CMX 每柜闪存 TB、每 GPU 保证带宽、prestaging SLA、副本因子、故障后重算预算 |
| 「PB / GPU POD」、以太网闪存、H2 2026 伙伴出货 | 5× 的对照负载（模型、ctx、命中、batch） |

出柜去 CMX 的带宽，仿真默认按 ConnectX-9 1.6 Tb/s 单向 ≈ **200 GB/s/GPU**，可在仿真页改。若采用「0.4 TB/s/GPU」那种机柜汇总表，按双向或不同口径理解，不要和 200 GB/s 混用。

## 1M 会话下，CMX 实际会被打到哪一面

负载：每人一条 ~1M token 会话，前缀命中 90/95/99%，PD 分离。两件不同的事：

- **Prefill 跑满**（主仿真）：算力打满时 unique tok/s 由 GEMM + indexer 决定。高命中时新 KV 很小，但 Prefill GPU 仍要把命中前缀从 CMX 拉进来才能 attend → **读主导**。99% @ 1M 时读/写 ≈ 99。
- **容量驻留**（第二仿真）：活着的 1M 会话数一旦超过 G1（再超过 G2），多出来的只能在 CMX。Agent 在工具上等待的会话不占算力，但占容量。这和「PB/POD」口径对得上。
- Decode 若整段 KV 已在 HBM，CMX 不参与那一步。用 `22 TB/s ÷ KV` 得到的 tok/s 是 G1 扫描上限，不是 CMX 带宽模型。

Prefill 跑满公式：

```
unique = C × (1 − hit) + extra
FLOPs  = 2 × N_active + attn(C)     # V4 indexer 只打 C/4
tok/s  = (peak × util) / FLOPs     # 默认 Rubin 50 PFLOPS × 40% util
write  = tok/s × ΔB/token
read   = req/s × KV(hit × C)
```

## 四模型 KV 口径（仿真里 dtype 可改，不跟模型绑定）

| 模型 | 结构 | BF16 @ 1,048,576 | FP8 @ 1M（常见 serving） |
|------|------|------------------|--------------------------|
| V4-Pro | 30 c4a + 31 c128a，共享 KV | **9.62 GiB** | ~4.8 GiB（FP8+FP4 再小） |
| V4-Flash | 2 SWA + 21 c4a + 20 c128a | 6.72 GiB | ~3.4 GiB |
| GLM-5.2 | 79×MLA 576-d + 22 indexer | 94.4 GiB | 47.3 GiB（引擎 656 页则 53.5） |
| Kimi K3 | 24 MLA + 69 KDA 固定态 | 27.2 GiB | 13.7 GiB |

V4-Pro 的 9.62 GiB 钉在 vLLM 2026-04-24 附录：shared-KV 1024 B/entry，c4a indexer 256 B，SWA 128。

- c4a：`(128 + N/4)×1024 + (N/4)×256`
- c128a：`(128 + N/128)×1024`
- 论文 §4.2.1：Pro 61 层，先 2 层 HCA，再 CSA/HCA 交错 → **30 c4a + 31 c128a**

不要用「V3.2 的 10%」或平均 KB/token 去代替逐层公式。也不要用 SGLang `DeepSeekV4SingleKVPool.get_bytes_per_token` 的 584 B 未压缩 FP8 页布局去代替附录这条压缩公式。

GLM-5.2 钉在 HuggingFace `zai-org/GLM-5.2`：78 层 + 1 MTP；`kv_lora_rank=512` + rope 64 = 576；`index_topk_freq=4`，offset 3 → 21 full indexer + MTP = 22。IndexShare 不缩小 MLA 存储。

Kimi K3 钉在 HuggingFace `moonshotai/Kimi-K3` `text_config`：93 层 = 69 KDA + 24 Gated MLA。KDA 是每会话固定态；高命中预取必须带完整 KDA snapshot。

## 来源

- [NVIDIA：BlueField-4-powered CMX](https://developer.nvidia.com/blog/introducing-nvidia-bluefield-4-powered-inference-context-memory-storage-platform-for-the-next-frontier-of-ai/)（G1–G4、G3.5、5×、prestaging）
- [NVIDIA：Vera Rubin POD](https://developer.nvidia.com/blog/nvidia-vera-rubin-pod-seven-chips-five-rack-scale-systems-one-ai-supercomputer/)（五类机柜、STX 上的 CMX、1152 GPU）
- [NVIDIA：Inside Vera Rubin](https://developer.nvidia.com/blog/inside-the-nvidia-rubin-platform-six-new-chips-one-ai-supercomputer/)（BF4 800 Gb/s、ICMS PB/POD、Dynamo/NIXL/DOCA）
- [NVIDIA Newsroom：STX](https://nvidianews.nvidia.com/news/nvidia-launches-bluefield-4-stx-storage-architecture-with-broad-industry-adoption)（Vera CPU + CX-9；5× TPS vs traditional storage）
- [vLLM DeepSeek V4 附录](https://vllm-project.github.io/2026/04/24/deepseek-v4.html)（9.62 GiB）
- DeepSeek-V4 论文 §4.2.1（arXiv:2606.19348）
