"""生成 D8a 的延迟量级图（KV 路径成本 vs 上下文长度）。

口径与 D8a §8.1 一致：GLM-5.2，93 KB/token，79 层。

运行：
    python3 kvcm/design/figures/plot_latency_stack.py

输出 algo-latency-stack.svg（覆盖）。数值改动时同步改 D8a §8.1 的表格。
"""

from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

KB = 1024
MB = KB * 1024
GB = MB * 1024

BYTES_PER_TOKEN = 93 * KB  # GLM-5.2
BW_WAN = 25 * GB  # 跨机，GB/s
BW_LOCAL = 100 * GB  # 本机 DRAM→HBM，GB/s
V_PREFILL = 5900  # token/s，整实例

CONTEXTS = [32_000, 64_000, 128_000, 256_000, 512_000, 1_000_000]
LABELS = ["32K", "64K", "128K", "256K", "512K", "1M"]


def main() -> None:
    sizes = [c * BYTES_PER_TOKEN for c in CONTEXTS]
    transfer = [s / BW_WAN * 1000 for s in sizes]  # ms
    local = [s / BW_LOCAL * 1000 for s in sizes]  # ms
    prefill = [c / V_PREFILL * 1000 for c in CONTEXTS]  # ms

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(LABELS, prefill, marker="o", label="T_prefill (recompute)")
    ax.plot(LABELS, transfer, marker="s", label="T_transfer (cross-host)")
    ax.plot(LABELS, local, marker="^", label="T_local (DRAM->HBM)")
    ax.set_yscale("log")
    ax.set_xlabel("context length")
    ax.set_ylabel("ms (log)")
    ax.set_title("KV path cost by context length (GLM-5.2, 93KB/token)")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()
    fig.tight_layout()

    out = pathlib.Path(__file__).with_name("algo-latency-stack.svg")
    fig.savefig(out, format="svg")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
