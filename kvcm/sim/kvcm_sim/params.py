"""仿真参数。每项的来源/假设标签与 `kvcm/sim/params.md` 一一对应，改这里要同步改那里。"""

from __future__ import annotations

from dataclasses import dataclass

GB = 1e9


@dataclass(frozen=True)
class Params:
    # ---- 模型：DeepSeek-V4.1-Flash（HF deepseek-ai/DeepSeek-V4.1-Flash config.json + 技术报告）
    # CSA2 只有 kv_source_layers=[2,8,14,20] 存全局 KV，compress_ratios 依次为 2,2,2,1；
    # 每个 entry = main KV 512 维 + indexer K 128 维；主口径按 BF16 存（用户决定）：
    # (1024 + 256) B，3×1280/2 + 1280 = 3,200 B
    kv_bytes_per_token: float = 3 * (1024 + 256) / 2 + (1024 + 256)       # 3,200 B
    # 官方 FP4 + E4M3 scale 存储：288 + 68 = 356 B/entry → 890 B（技术报告数字，仅作对照）
    kv_bytes_per_token_fp4: float = 3 * (288 + 68) / 2 + (288 + 68)        # 890 B
    weights_bytes: float = 510.3e9       # HF safetensors 48 个分片合计（FP8 + FP4 专家 + Engram 表）

    # ---- 硬件：昇腾 A3，14 节点 × 16 NPU = 224（用户给定）
    n_nodes: int = 14
    npu_per_node: int = 16
    hbm_per_npu: float = 64 * GB         # 假设：910C 每 die 64 GB（每芯片 128 GB，2 die 计 2 NPU）
    act_reserve_per_npu: float = 4 * GB  # 假设：激活/workspace/通信 buffer 预留
    # vLLM gpu_memory_utilization：引擎只用每 die HBM 的 85%，权重与激活都从这 85% 里出
    gpu_mem_util: float = 0.85
    act_in_budget: bool = True           # 敏感性：False = 激活不从预算里扣（360 GB/实例）
    dram_kv_per_node: float = 1000 * GB  # 假设：每节点可给 KV 池的 DRAM
    nodes_per_instance: int = 1          # 假设：16 NPU 一个实例（权重 510 GB 单节点放得下），共 14 实例

    # ---- 带宽（kvcm/design/D8a-scheduling-algorithms.md §9.1 A3 910C 参考）
    bw_local_h2d: float = 86 * GB        # FabricMem 机内 rH2D（AICPU 展开）下限
    bw_remote: float = 78 * GB           # FabricMem 跨机 rH2D（AICPU 展开）下限
    bw_roce: float = 23.8 * GB           # ROCE rH2D（敏感性）

    # ---- prefill：D8a §8.1 设备 A（950）V4-Pro 拟合，TTFT = c + (1-hit)(aN + bN²)，N 为 K-token
    fit_c: float = 0.0259
    fit_a: float = 0.1856
    fit_b: float = 0.0003338
    fit_cards: int = 64                  # 拟合测点拓扑：64 卡 · gbs=64（tools/cmx-sim/measured.js）
    fit_gbs: int = 64
    a3_per_950: float = 2.0              # 假设：1 张 950 ≈ 2 个 A3 NPU（die）的 prefill 吞吐
    # 假设：拟合是 V4-Pro（prefill 激活 49B）；V4.1-Flash prefill 只激活 8B（技术报告 CED），
    # 与 token 数相关的项按激活参数比缩放，固定开销 c 不缩
    model_prefill_scale: float = 8.0 / 49.0

    # ---- 负载
    prefill_util_target: float = 0.5     # 假设：基线下 prefill 占集群 NPU 时间 50%（混部，其余给 decode）

    @property
    def n_npu(self) -> int:
        return self.n_nodes * self.npu_per_node

    @property
    def n_inst(self) -> int:
        return self.n_nodes // self.nodes_per_instance

    @property
    def npu_per_inst(self) -> int:
        return self.npu_per_node * self.nodes_per_instance

    @property
    def hbm_kv_per_inst(self) -> float:
        per_npu = (self.hbm_per_npu * self.gpu_mem_util - self.weights_bytes / self.npu_per_inst
                   - (self.act_reserve_per_npu if self.act_in_budget else 0.0))
        return max(0.0, per_npu) * self.npu_per_inst

    @property
    def dram_kv_total(self) -> float:
        return self.dram_kv_per_node * self.n_nodes

    def pages(self, nbytes: float, page_tokens: int) -> int:
        return int(nbytes / (page_tokens * self.kv_bytes_per_token))

    # NPU-seconds：拟合点 64 卡同时跑 64 个请求，单请求平均占 64/64 = 1 张 950 卡·TTFT
    def npu_seconds(self, n_tokens, hit_frac):
        import numpy as np
        nk = np.asarray(n_tokens, dtype=float) / 1024.0
        ttft = self.fit_c + (1.0 - np.asarray(hit_frac)) * self.model_prefill_scale * (self.fit_a * nk + self.fit_b * nk * nk)
        return ttft * (self.fit_cards / self.fit_gbs) * self.a3_per_950

    # A3 上同等每卡并发下的 TTFT：拟合 TTFT × 950→A3 换算 + 未掩盖的 KV 加载时间
    def ttft(self, n_tokens, hit_frac, load_bytes_local=0.0, load_bytes_remote=0.0, remote_bw=None):
        import numpy as np
        nk = np.asarray(n_tokens, dtype=float) / 1024.0
        base = self.fit_c + (1.0 - np.asarray(hit_frac)) * self.model_prefill_scale * (self.fit_a * nk + self.fit_b * nk * nk)
        bw_r = remote_bw or self.bw_remote
        return base * self.a3_per_950 + np.asarray(load_bytes_local) / self.bw_local_h2d \
            + np.asarray(load_bytes_remote) / bw_r


DEFAULT = Params()
