"""仿真参数。每项的来源/假设标签与 `kvcm/sim/params.md` 一一对应，改这里要同步改那里。"""

from __future__ import annotations

from dataclasses import dataclass

GB = 1e9


@dataclass(frozen=True)
class Params:
    # ---- 模型：DeepSeek V4.1（假设与 V4-Pro 同层结构）
    # tools/cmx-sim/sim.js MODELS.v4pro：nC4=30, nC128=31；BF16 logical entry 1024 B main / 256 B index
    # growing bytes/token = 30*(1024+256)/4 + 31*1024/128
    kv_bytes_per_token: float = 30 * (1024 + 256) / 4 + 31 * 1024 / 128   # 9,848 B（BF16）
    kv_bytes_per_token_fp8: float = 30 * (584 + 132) / 4 + 31 * 584 / 128  # 5,511 B（vLLM FP8 payload，敏感性）
    weights_bytes: float = 1.6e12        # 假设：1.6T 参数 × W8A8 1 B/参数

    # ---- 硬件：昇腾 A3，14 节点 × 16 NPU = 224（用户给定）
    n_nodes: int = 14
    npu_per_node: int = 16
    hbm_per_npu: float = 64 * GB         # 假设：910C 每 die 64 GB（每芯片 128 GB，2 die 计 2 NPU）
    act_reserve_per_npu: float = 4 * GB  # 假设：激活/workspace/通信 buffer 预留
    dram_kv_per_node: float = 1000 * GB  # 假设：每节点可给 KV 池的 DRAM
    nodes_per_instance: int = 2          # 假设：32 NPU 一个实例（权重 1.6 TB 需 >1 节点），共 7 实例

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
        per_npu = self.hbm_per_npu - self.weights_bytes / self.npu_per_inst - self.act_reserve_per_npu
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
        ttft = self.fit_c + (1.0 - np.asarray(hit_frac)) * (self.fit_a * nk + self.fit_b * nk * nk)
        return ttft * (self.fit_cards / self.fit_gbs) * self.a3_per_950

    # A3 上同等每卡并发下的 TTFT：拟合 TTFT × 950→A3 换算 + 未掩盖的 KV 加载时间
    def ttft(self, n_tokens, hit_frac, load_bytes_local=0.0, load_bytes_remote=0.0, remote_bw=None):
        import numpy as np
        nk = np.asarray(n_tokens, dtype=float) / 1024.0
        base = self.fit_c + (1.0 - np.asarray(hit_frac)) * (self.fit_a * nk + self.fit_b * nk * nk)
        bw_r = remote_bw or self.bw_remote
        return base * self.a3_per_950 + np.asarray(load_bytes_local) / self.bw_local_h2d \
            + np.asarray(load_bytes_remote) / bw_r


DEFAULT = Params()
