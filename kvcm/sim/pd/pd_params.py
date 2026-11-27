"""PD 转换仿真的专用参数。模型/硬件/prefill 代价沿用 `kvcm_sim.params.DEFAULT`，
这里只放 PD 实验新增的量；每项的来源/假设标签见 `kvcm/sim/params.md`「PD 转换」节。"""

from __future__ import annotations

from dataclasses import dataclass, replace

from kvcm_sim.params import DEFAULT as BASE, GB


@dataclass(frozen=True)
class PDParams:
    # ---- 用户给定口径（2026-11-25）
    kv_bytes_per_token: float = BASE.kv_bytes_per_token      # BF16 3,200 B
    hbm_kv_per_node: float = 296 * GB    # gpu_memory_utilization 0.85 → 18.5 GB/die × 16
    host_weight_copy: float = 510 * GB   # sleep/wake 路径的 host 权重副本，从 DRAM 池扣
    n_nodes: int = BASE.n_nodes
    npu_per_node: int = BASE.npu_per_node

    # ---- decode（假设：A3 上无 V4.1-Flash 实测）
    t_step: float = 0.025                # D 节点满 KV 时一步 decode 耗时（roofline 估算 + 固定开销）
    tpot_slo: float = 0.050              # docs/features/slo.md ITL P50 < 50 ms（draft）
    kv_fill: float = 0.90                # D 节点 HBM KV 最高填充率
    rho_p: float = 0.80                  # P 节点 prefill 利用率上限（给排队留余量）

    # ---- 切换（假设）
    t_weights_sleep: float = 30.0        # sleep/wake + host H2D 回填 + 唤醒，单次（敏感性 10/120/600 s）
    t_p_drain: float = 2.0               # P 节点排空在途 prefill（≈ TTFT P95 量级）
    bw_flush_per_npu: float = 54 * GB    # FabricMem D2rH（D8a §9.1）：未写满页刷入池
    bw_pool_read_per_npu: float = BASE.bw_remote   # 78 GB/s：接收节点从池重读
    bw_d2d_per_npu: float = BASE.bw_roce           # 无池时点对点迁移（RoCE 23.8 GB/s）
    page_tokens: int = 512               # 写穿粒度：写满一页才入池

    # ---- 流体模型
    window: float = 300.0                # 5 分钟窗口
    overload_tol: float = 0.01           # goodput：过载窗口占比 ≤ 1%

    @property
    def kv_cap_node(self) -> float:
        return self.hbm_kv_per_node * self.kv_fill

    @property
    def f_mixed(self) -> float:
        """混部节点 prefill 时间占比上限：既要排队余量，又要 decode TPOT = t_step/(1-f) ≤ SLO。"""
        return min(self.rho_p, 1.0 - self.t_step / self.tpot_slo)

    def with_(self, **kw) -> "PDParams":
        return replace(self, **kw)


DEFAULT = PDParams()
