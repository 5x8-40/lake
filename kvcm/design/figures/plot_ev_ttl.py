"""生成 D8a 的预取 EV 曲线与保留 TTL 曲线。

口径：DeepSeek V3.2，128K，90%→99% 的拟合 TTFT 差值作为 S_saved；
跨机 ROCE 23.8GB/s。

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

GB = 1_000_000_000
CONTEXT = 128_000
BYTES_PER_TOKEN = 85_888  # DeepSeek V3.2 BF16
BW_WAN = 23.8 * GB
V32_C, V32_A, V32_B = 0.0106, 0.1929, 0.0016629


def ttft_ms(nk: int, hit: float) -> float:
    return (
        V32_C + (1 - hit) * (V32_A * nk + V32_B * nk * nk)
    ) * 1000


S_BYTES = CONTEXT * BYTES_PER_TOKEN
S_SAVED_MS = ttft_ms(CONTEXT / 1000, 0.90) - ttft_ms(CONTEXT / 1000, 0.99)
C_COPY_MS = S_BYTES / BW_WAN * 1000
C_OCCUPANCY_MS_PER_S = 2.0


def plot_ev() -> None:
    lam = 1 / 300
    ts = [t for t in range(1, 1801, 10)]
    ev = []
    for t in ts:
        p_use = 1 - math.exp(-lam * t)
        occupancy = (S_BYTES / GB) * t * 0.01
        ev.append(p_use * S_SAVED_MS - C_COPY_MS - occupancy)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(ts, ev)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xlabel("prefetch lead time (s)")
    ax.set_ylabel("EV (ms)")
    ax.set_title(
        f"Prefetch EV (V3.2, 128K, 90->99%, copy={C_COPY_MS:.0f}ms)"
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
    ax.set_title(f"Retention net benefit (V3.2, 128K, {S_BYTES / GB:.1f}GB)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out = pathlib.Path(__file__).with_name("algo-retention-ttl.svg")
    fig.savefig(out, format="svg")
    print(f"wrote {out}")


if __name__ == "__main__":
    plot_ev()
    plot_ttl()
