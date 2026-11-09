"""生成 D8a 的预取 EV 曲线与保留 TTL 曲线。

口径与 D8a §8.1 一致：GLM-5.2，93 KB/token，跨机 ROCE 23.8 GB/s。

运行：
    python3 kvcm/design/figures/plot_ev_ttl.py

输出 algo-prefetch-ev.svg 与 algo-retention-ttl.svg（覆盖）。
"""

from __future__ import annotations

import math
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

KB = 1024
MB = KB * 1024
GB = 1_000_000_000  # 十进制 GB（HIXL 带宽口径）

BYTES_PER_TOKEN = 93 * KB
BW_WAN = 23.8 * GB  # 950 ROCE 实测
V_PREFILL = 5900

# 以 128K 上下文为例（长上下文的代表值）
CONTEXT = 128_000
S_BYTES = CONTEXT * BYTES_PER_TOKEN
S_SAVED_MS = 0.1 * CONTEXT / V_PREFILL * 1000  # 命中率 +10% 省下的 prefill
C_COPY_MS = S_BYTES / BW_WAN * 1000  # 一次跨机搬运的耗时
C_OCCUPANCY_MS_PER_S = 2.0  # 每 GB·s 的占用成本，折算到下列用法


def plot_ev() -> None:
    lam = 1 / 300  # 平均 300s 一次复用
    ts = [t for t in range(1, 1801, 10)]
    ev = []
    for t in ts:
        p_use = 1 - math.exp(-lam * t)
        occupancy = (S_BYTES / GB) * t * 0.01  # 简化：每 GB·s 0.01ms
        ev.append(p_use * S_SAVED_MS - C_COPY_MS - occupancy)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(ts, ev)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xlabel("prefetch lead time (s)")
    ax.set_ylabel("EV (ms)")
    ax.set_title(
        f"Prefetch EV vs lead time (128K ctx, 93KB/tok, copy={C_COPY_MS:.0f}ms (ROCE 23.8GB/s))"
    )
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out = pathlib.Path(__file__).with_name("algo-prefetch-ev.svg")
    fig.savefig(out, format="svg")
    print(f"wrote {out}")


def plot_ttl() -> None:
    p0, mu, c_h_ms_per_gb_s = 1.0, 1 / 600, 0.5
    ts = [t for t in range(1, 3601, 10)]
    net = []
    for t in ts:
        resume = p0 * math.exp(-mu * t) * S_SAVED_MS
        cost = c_h_ms_per_gb_s * (S_BYTES / GB) * t
        net.append(resume - cost)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(ts, net)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xlabel("retention TTL (s)")
    ax.set_ylabel("Net (ms)")
    ax.set_title(f"Retention net benefit vs TTL (128K ctx, {S_BYTES / GB:.1f}GB)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out = pathlib.Path(__file__).with_name("algo-retention-ttl.svg")
    fig.savefig(out, format="svg")
    print(f"wrote {out}")


if __name__ == "__main__":
    plot_ev()
    plot_ttl()
