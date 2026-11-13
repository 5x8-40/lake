"""生成 D8a 的 TTFT 图：设备 A 拟合 + 本机 H2D / A3 ROCE。

口径：DeepSeek V3.2 / V4-Pro，单实例 950 实测拟合；
拟合式 TTFT = c + (1-hit)(a*N + b*N^2)，N 为 K-token。

运行：
    python3 kvcm/design/figures/plot_latency_stack.py

输出 algo-latency-stack.svg（覆盖）。
"""

from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

GB = 1_000_000_000

# cmx-sim measured.js buildFit('device-a', ...)
V32 = {"name": "V3.2", "c": 0.0106, "a": 0.1929, "b": 0.0016629, "bpt": 85_888}
V4 = {"name": "V4-Pro", "c": 0.0259, "a": 0.1856, "b": 0.0003338, "bpt": 9_848}
HIT = 0.99
BW_H2D = 47 * GB
BW_ROCE = 23.8 * GB

CONTEXTS_K = [32, 64, 128, 256, 512, 1024]
LABELS = ["32K", "64K", "128K", "256K", "512K", "1M"]


def ttft(model: dict, nk: int, hit: float = HIT) -> float:
    n = nk
    return model["c"] + (1 - hit) * (model["a"] * n + model["b"] * n * n)


def main() -> None:
    fig, ax = plt.subplots(figsize=(8, 5))
    for model, style in ((V32, "-"), (V4, "--")):
        fit = [ttft(model, n) * 1000 for n in CONTEXTS_K]
        local = [
            (ttft(model, n) + n * 1000 * model["bpt"] / BW_H2D) * 1000
            for n in CONTEXTS_K
        ]
        cross = [
            (ttft(model, n) + n * 1000 * model["bpt"] / BW_ROCE) * 1000
            for n in CONTEXTS_K
        ]
        name = model["name"]
        ax.plot(LABELS, fit, marker="o", linestyle=style, label=f"{name} fit")
        ax.plot(LABELS, local, marker="^", linestyle=style, label=f"{name} + H2D 47GB/s")
        ax.plot(LABELS, cross, marker="s", linestyle=style, label=f"{name} + ROCE 23.8GB/s")

    ax.set_yscale("log")
    ax.set_xlabel("context length")
    ax.set_ylabel("TTFT (ms, log)")
    ax.set_title("TTFT: fitted + local H2D / A3 ROCE (99% hit)")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=7)
    fig.tight_layout()

    out = pathlib.Path(__file__).with_name("algo-latency-stack.svg")
    fig.savefig(out, format="svg")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
