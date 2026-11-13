"""生成 D11 对比图：定位象限 / 能力矩阵 / 两种控制流。

口径：kvcm@4d9dd33（2026-11-13）与各上游 HEAD（快照见 D11 头部）。
坐标与打分为**判断**，依据逐条写进 D11 §2–§4 的事实表。

运行：
    python3 kvcm/design/figures/plot_vs_oss.py

输出 fig-vs-oss-positioning.svg / fig-vs-oss-matrix.svg / fig-vs-oss-flow.svg。
图内文字用英文：仓库无 CJK 字体，中文会渲染成方块。
"""

from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

HERE = pathlib.Path(__file__).parent


def positioning() -> None:
    """x = who decides placement; y = what it can do to KV bytes."""
    # (label, x, y, kind)  kind: engine / hint / central / store
    pts = [
        ("vLLM / SGLang\nenvelope only", 1.0, 1.1, "hint"),
        ("UCM", 1.4, 2.5, "engine"),
        ("FlexKV", 1.8, 3.0, "engine"),
        ("KVCR", 2.2, 3.7, "hint"),
        ("MemCache", 2.1, 2.9, "store"),
        ("AIBrix", 3.2, 2.2, "central"),
        ("Dynamo router\n+ KVCR", 3.4, 2.5, "central"),
        ("llm-d EPP\n+ P2P", 3.7, 2.8, "central"),
        ("LMCache\ncoordinator", 4.1, 3.2, "central"),
        ("kvcm / KV Fabric", 4.6, 4.6, "ours"),
    ]
    color = {
        "engine": "#8fb3d9",
        "hint": "#e0a35c",
        "central": "#9dc08b",
        "store": "#b8b8b8",
        "ours": "#c0504d",
    }
    fig, ax = plt.subplots(figsize=(8.2, 6.2))
    for label, x, y, kind in pts:
        ax.scatter(
            x,
            y,
            s=340 if kind == "ours" else 210,
            c=color[kind],
            edgecolors="black" if kind == "ours" else "none",
            linewidths=1.6,
            zorder=3,
        )
        ax.annotate(
            label,
            (x, y),
            textcoords="offset points",
            xytext=(0, -22),
            ha="center",
            va="top",
            fontsize=8,
        )
    ax.set_xlim(0.4, 5.3)
    ax.set_ylim(0.4, 5.3)
    ax.set_xlabel("who decides where KV lives  (engine-local  ->  central control plane)")
    ax.set_ylabel("what it can do to KV  (route only -> move / pin / retain)")
    ax.set_title("KV control: engine-local plugins vs central client", fontsize=11)
    ax.grid(True, alpha=0.25, linestyle="--")
    handles = [
        plt.Line2D([], [], marker="o", linestyle="", markersize=9, color=color[k],
                   label=n)
        for k, n in (
            ("engine", "engine-side plugin, no hint"),
            ("hint", "hint envelope consumed in engine"),
            ("central", "central view, routing or commands"),
            ("store", "object pool (no KV semantics)"),
            ("ours", "kvcm (pool-external client)"),
        )
    ]
    ax.legend(handles=handles, loc="lower right", fontsize=7.5, framealpha=0.95)
    fig.tight_layout()
    fig.savefig(HERE / "fig-vs-oss-positioning.svg", format="svg")
    plt.close(fig)


CAPS = [
    "global prefix->placement view",
    "authoritative replica record",
    "pick target instance",
    "prefetch to target",
    "keepalive / renew lease",
    "pin (no evict)",
    "demote / offload",
    "move (source deleted)",
    "cross-engine survival (retain)",
    "sees engine HBM / APC",
]
COLS = ["UCM", "FlexKV", "KVCR\n(+envelope)", "LMCache\ncoord", "llm-d\nEPP", "Dynamo\nrouter", "AIBrix", "kvcm"]
# 2 = own implementation, 1 = partial / indirect, 0 = absent
GRID = np.array([
    [0, 1, 1, 2, 2, 2, 2, 2],  # global view
    [0, 0, 0, 0, 0, 0, 0, 2],  # authoritative record
    [0, 0, 2, 2, 1, 1, 1, 2],  # pick target
    [1, 1, 2, 2, 0, 0, 1, 2],  # prefetch to target
    [0, 0, 0, 0, 0, 0, 0, 2],  # keepalive (lease renewal)
    [1, 0, 1, 2, 0, 0, 1, 2],  # pin
    [1, 1, 0, 2, 1, 1, 1, 2],  # demote
    [0, 1, 2, 0, 1, 1, 0, 2],  # move
    [0, 0, 0, 0, 0, 0, 0, 0],  # retain (designed, not wired)
    [1, 1, 1, 0, 1, 1, 1, 0],  # sees HBM
])
TXT = {
    (0, 0): "evt", (0, 1): "radix", (0, 2): "hint", (0, 3): "dir", (0, 4): "idx",
    (0, 5): "idx", (0, 6): "idx", (0, 7): "master",
    (1, 7): "auth",
    (2, 0): "-", (2, 1): "-", (2, 2): "y", (2, 3): "y", (2, 4): "req", (2, 5): "req",
    (2, 6): "req", (2, 7): "y",
    (3, 0): "store", (3, 1): "self", (3, 2): "y", (3, 3): "y", (3, 6): "L2", (3, 7): "y",
    (4, 7): "loop", (5, 0): "GC", (5, 2): "env", (5, 3): "y", (5, 6): "L2", (5, 7): "y",
    (6, 0): "store", (6, 1): "self", (6, 3): "y", (6, 4): "fs", (6, 5): "kvcr",
    (6, 6): "L2", (6, 7): "flow",
    (7, 1): "self", (7, 2): "y", (7, 4): "p2p", (7, 5): "p2p", (7, 7): "y",
    (8, 3): "pin", (8, 7): "todo",
    (9, 0): "y", (9, 1): "y", (9, 2): "y", (9, 4): "idx", (9, 5): "idx", (9, 6): "L1",
    (9, 7): "no",
}


def matrix() -> None:
    fig, ax = plt.subplots(figsize=(8.6, 6.4))
    cmap = matplotlib.colors.ListedColormap(["#f2f2f2", "#f6d6a8", "#7fae7a"])
    ax.imshow(GRID, cmap=cmap, vmin=0, vmax=2, aspect="auto")
    ax.set_xticks(range(len(COLS)))
    ax.set_xticklabels(COLS, fontsize=8)
    ax.set_yticks(range(len(CAPS)))
    ax.set_yticklabels(CAPS, fontsize=8)
    for i in range(GRID.shape[0]):
        for j in range(GRID.shape[1]):
            ax.text(
                j, i, TXT.get((i, j), ""), ha="center", va="center",
                fontsize=7, color="#222222",
            )
    ax.set_xticks(np.arange(-0.5, len(COLS), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(CAPS), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", length=0)
    ax.set_title(
        "KV capability coverage  (green = own implementation, orange = partial / indirect)",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(HERE / "fig-vs-oss-matrix.svg", format="svg")
    plt.close(fig)


def _box(ax, x, y, w, h, text, fc="#eef2f7"):
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.02",
            fc=fc, ec="#555555", linewidth=1.0,
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=7.5)


def _arrow(ax, xy1, xy2, style="->", ls="-"):
    ax.add_patch(
        FancyArrowPatch(xy1, xy2, arrowstyle=style, mutation_scale=10,
                        linewidth=1.0, linestyle=ls, color="#333333")
    )


def flow() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.4))
    for ax in axes:
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

    a = axes[0]
    a.set_title("hint push: router advises, engine decides", fontsize=9.5)
    _box(a, 0.05, 0.76, 0.36, 0.14, "router / EPP\n(global index)")
    _box(a, 0.55, 0.76, 0.40, 0.14, "engine + KVCR library")
    _box(a, 0.55, 0.44, 0.40, 0.18, "policy: decide_ingest\nreject / accept")
    _box(a, 0.55, 0.10, 0.40, 0.18, "local DRAM / SSD\n+ NIXL peer")
    _arrow(a, (0.41, 0.83), (0.55, 0.83))
    a.text(0.475, 0.87, "kv.fetch\nkv.prefetch\nkv.move", fontsize=6.5, ha="center")
    _arrow(a, (0.75, 0.76), (0.75, 0.62))
    _arrow(a, (0.75, 0.44), (0.75, 0.28))
    a.text(0.05, 0.36,
           "engine owns HBM state,\nbatch and queue depth\n"
           "-> keeps the cost decision\n-> control plane cannot\n   force a move",
           fontsize=7, va="top")
    a.text(0.55, 0.02,
           "vLLM / SGLang ship the envelope only:\nno action handler on main",
           fontsize=6.5, style="italic")

    b = axes[1]
    b.set_title("central command: control plane acts on the pool", fontsize=9.5)
    _box(b, 0.03, 0.72, 0.38, 0.16, "kvcm\n(pool client, own UUID)", fc="#f7e7e6")
    _box(b, 0.55, 0.82, 0.40, 0.12, "Mooncake master\n(replica authority)")
    _box(b, 0.55, 0.52, 0.40, 0.20, "seg A (engine)\nseg B (engine)\nkvcm seg (opt.)")
    _box(b, 0.28, 0.10, 0.42, 0.16, "sqlite ledger\n(keepalive / tasks)")
    _arrow(b, (0.41, 0.80), (0.55, 0.87))
    b.text(0.47, 0.93, "create_copy_task /\nmove / pin", fontsize=6.5, ha="center")
    _arrow(b, (0.55, 0.74), (0.41, 0.78))
    b.text(0.47, 0.66, "GetReplicaList\n-> READY", fontsize=6.5, ha="center")
    _arrow(b, (0.75, 0.82), (0.75, 0.72))
    _arrow(b, (0.30, 0.72), (0.42, 0.26))
    b.text(0.03, 0.40,
           "control plane names the target\nand can move bytes;\n"
           "engine need not take part\n(source seg client must be alive)",
           fontsize=7, va="top")

    fig.tight_layout()
    fig.savefig(HERE / "fig-vs-oss-flow.svg", format="svg")
    plt.close(fig)


def main() -> None:
    positioning()
    matrix()
    flow()
    for name in ("fig-vs-oss-positioning.svg", "fig-vs-oss-matrix.svg", "fig-vs-oss-flow.svg"):
        print(f"wrote {HERE / name}")


if __name__ == "__main__":
    main()
