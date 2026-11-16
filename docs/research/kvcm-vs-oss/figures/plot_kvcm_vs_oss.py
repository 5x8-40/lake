"""生成 kvcm / KV Fabric 与开源方案对比的自绘图（对照工件）。

口径：kvcm @ `4d9dd33`（2026-11-13）；上游快照 2026-11-13（逐家 commit 见正文头部）。

正文主线是「KV 依附于谁」，所以第一张图直接画这条：
KV 的持有者从「某个请求」到「池」，各家排在同一条轴上。
持有者是引擎进程或请求，动作就只能从请求开始、到请求结束；
持有者是池，KV 才能跨请求、跨引擎存在。

运行：
    python3 docs/research/kvcm-vs-oss/figures/plot_kvcm_vs_oss.py

输出（本目录下的**自绘图**，非上游原图）：
    fig-ownership.svg      主线图：KV 依附于谁，各家落在哪一段
    fig-evidence.svg       证据图：控制权矩阵 + hint 落地时间线
    fig-ucm-schematic.svg  UCM 分层示意（redrawn from 上游 architecture.svg）

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

INK = "#222222"
EDGE = "#555555"
BLUE = "#dbe7f5"
GREEN = "#dfeedd"
AMBER = "#f8e6c8"
GREY = "#ececec"
RED = "#f6dcd9"


def _box(ax, x, y, w, h, text, fc=GREY, fs=8.0, weight="normal"):
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0.004,rounding_size=0.010",
            fc=fc, ec=EDGE, linewidth=0.9,
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, color=INK, fontweight=weight, linespacing=1.35)


def _arrow(ax, xy1, xy2, style="->", ls="-", color="#333333"):
    ax.add_patch(
        FancyArrowPatch(xy1, xy2, arrowstyle=style, mutation_scale=9,
                        linewidth=0.9, linestyle=ls, color=color)
    )


# ---------------------------------------------------------------------------
# 1. 主线：KV 依附于谁
# ---------------------------------------------------------------------------

# (持有者, 这一段的含义, 该段的项目, 底色, 能做到什么)
OWNERS = [
    (
        "request",
        "lives and dies with one request",
        ["vLLM / SGLang envelopes"],
        AMBER,
        "action window = this request only\n(handlers not wired on main)",
    ),
    (
        "engine process",
        "lives in the engine's private memory",
        ["UCM", "FlexKV", "AIBrix kvcache", "KVCR (pre-fix)"],
        BLUE,
        "can touch HBM/APC directly;\nneeds engine patches per version",
    ),
    (
        "sidecar / guard",
        "lives beside the engine, survives it",
        ["KVCR + Guard"],
        GREEN,
        "survives engine death;\nbut only for one engine's cache",
    ),
    (
        "coordinator (event view)",
        "coordinates instances, does not own bytes",
        ["LMCache mp_coordinator"],
        GREEN,
        "fleet view + per-instance commands;\nview is eventually consistent",
    ),
    (
        "pool",
        "lives in the pool; the engine is only a client",
        ["MemCache", "Mooncake master"],
        GREEN,
        "survives engine death;\nauthoritative replica record",
    ),
]

KVCM_NOTE = (
    "kvcm / KV Fabric\n"
    "a client of the pool:\n"
    "decides where KV should be,\n"
    "not where it happens to be"
)


def ownership() -> None:
    fig, ax = plt.subplots(figsize=(13.0, 6.8))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(
        "Who holds the KV?  Everything else follows from this column",
        fontsize=11.5, pad=12,
    )

    n = len(OWNERS)
    top, step = 0.90, 0.145
    # 最后一行（pool）额外下移，让出空隙放分界线与说明文字，避免压住文字
    gap_shift = 0.075
    col_x, col_w = 0.03, 0.42
    act_x = 0.47

    ax.text(col_x + 0.005, top + 0.058, "holder", fontsize=8.2,
            fontweight="bold", color="#5a5a5a")
    ax.text(act_x + 0.005, top + 0.058, "what that buys / costs", fontsize=8.2,
            fontweight="bold", color="#5a5a5a")

    ys = []
    for i, (holder, gloss, projects, fc, note) in enumerate(OWNERS):
        y = top - i * step - (gap_shift if i >= 4 else 0.0)
        ys.append(y)
        _box(ax, col_x, y - 0.125, col_w, 0.125, "", fc=fc)
        ax.text(col_x + 0.015, y - 0.026, holder, fontsize=8.0,
                fontweight="bold", color=INK)
        ax.text(col_x + 0.015, y - 0.052, gloss, fontsize=6.4,
                color="#4a4a4a", style="italic")
        ax.text(col_x + 0.015, y - 0.084, " · ".join(projects), fontsize=7.2, color=INK)

        ax.text(act_x + 0.005, y - 0.048, note, fontsize=6.8, color=INK, linespacing=1.5)

    # 分界线画在 coordinator 与 pool 之间的空隙里，说明文字也放这段空隙
    y_split = (ys[3] - 0.125 + ys[4]) / 2
    ax.plot([0.012, 0.745], [y_split, y_split], color="#b03a2e",
            linewidth=1.3, linestyle=(0, (5, 3)))
    ax.text(0.015, y_split + 0.010,
            "above: the KV needs the engine (or a request) to exist",
            fontsize=6.8, color="#b03a2e")
    ax.text(0.015, y_split - 0.022,
            "below: the pool outlives the engine — this is where kvcm works",
            fontsize=6.8, color="#b03a2e")

    # kvcm 落在 pool 这一行的右侧，明确它不是池本身
    y_pool = ys[4]
    _arrow(ax, (col_x + col_w + 0.005, y_pool - 0.0625),
           (0.795, y_pool - 0.0625), style="->", color="#b03a2e")
    _box(ax, 0.80, y_pool - 0.125, 0.185, 0.125, "", fc=RED)
    ax.text(0.8925, y_pool - 0.0625, KVCM_NOTE, ha="center", va="center",
            fontsize=6.6, fontweight="bold", color=INK, linespacing=1.45)

    ax.text(0.03, 0.018,
            "Reading: a holder that is an engine process or a request can only act between "
            "\"request arrives\" and \"request ends\".\n"
            "A pool can hold KV with no request at all — that is what makes prefetch, keepalive, "
            "cross-engine survival and byte accounting possible.",
            fontsize=6.8, color="#5a5a5a", linespacing=1.6)

    fig.tight_layout()
    fig.savefig(HERE / "fig-ownership.svg", format="svg")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 2. 决策权 / 执行权 / 权威记录 + hint 落地时间线
# ---------------------------------------------------------------------------

AUTH_ROWS = [
    ("UCM (engine plugin)", 2, 1, 0),
    ("FlexKV (engine plugin)", 2, 1, 0),
    ("MemCache (pool)", 1, 1, 2),
    ("Mooncake (pool)", 1, 1, 2),
    ("vLLM / SGLang (envelope)", 1, 0, 0),
    ("KVCR (envelope consumer)", 2, 2, 1),
    ("LMCache coordinator", 2, 2, 1),
    ("llm-d EPP (routing)", 1, 0, 0),
    ("Dynamo router (routing)", 2, 1, 1),
    ("AIBrix gateway (routing)", 1, 0, 1),
    ("kvcm (pool client)", 2, 2, 1),
]
AUTH_COLS = ["decides\n(where KV goes)", "enforces\n(acts on bytes)", "authoritative\nrecord"]
AUTH_TXT = {
    (0, 0): "hit/skip", (0, 1): "local store", (0, 2): "-",
    (1, 0): "hit/skip", (1, 1): "local tier", (1, 2): "-",
    (2, 0): "per put", (2, 1): "node", (2, 2): "meta",
    (3, 0): "per put", (3, 1): "segment", (3, 2): "master",
    (4, 0): "engine,\nunwired", (4, 1): "-", (4, 2): "-",
    (5, 0): "policy", (5, 1): "NIXL pull", (5, 2): "local only",
    (6, 0): "coordinator", (6, 1): "MP server", (6, 2): "derived",
    (7, 0): "request", (7, 1): "-", (7, 2): "-",
    (8, 0): "router", (8, 1): "engine", (8, 2): "derived",
    (9, 0): "gateway", (9, 1): "-", (9, 2): "derived",
    (10, 0): "D8 mgmt\nplane", (10, 1): "kvcm ctrl\nplane", (10, 2): "master\n(read)",
}


def _draw_matrix(ax) -> None:
    grid = np.array([[r[1], r[2], r[3]] for r in AUTH_ROWS])
    cmap = matplotlib.colors.ListedColormap([GREY, AMBER, GREEN])
    ax.imshow(grid, cmap=cmap, vmin=0, vmax=2, aspect="auto")
    ax.set_xticks(range(len(AUTH_COLS)))
    ax.set_xticklabels(AUTH_COLS, fontsize=7.4)
    ax.set_yticks(range(len(AUTH_ROWS)))
    ax.set_yticklabels([r[0] for r in AUTH_ROWS], fontsize=7.4)
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            ax.text(j, i, AUTH_TXT.get((i, j), ""), ha="center", va="center",
                    fontsize=6.6, color=INK)
    ax.set_xticks(np.arange(-0.5, 3, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(AUTH_ROWS), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", length=0)
    ax.set_title(
        "(a) who decides / who acts on bytes / who records authority\n"
        "green = owns it, orange = participates but does not own, grey = absent",
        fontsize=8.2, pad=8,
    )


# (x 月位, 标签, 标记)  标记: o = RFC / 公开, s = 已合入, X = 缺位
# 日期均取自 GitHub issue/PR 的 created_at / closed_at（2026-11-13 核对）。
TIMELINE = {
    "vLLM": [
        (2026.517, "session_id #48048\nmerged", "s"),
        (2026.600, "RFC #51428 KvHint", "o"),
        (2026.643, "#53421 envelope\nRFC opened", "o"),
        (2026.704, "KVCR tier #53624\nmerged (envelope in main)", "s"),
        (2026.880, "no action handler;\nRetention #37003 open", "X"),
    ],
    "SGLang": [
        (2026.436, "RFC #27574\nprogrammatic KV", "o"),
        (2026.648, "envelope #36224\nopened", "o"),
        (2026.810, "KVCR as HiCache\nbackend #32903", "s"),
        (2026.860, "#36224 merged", "s"),
        (2026.890, "no action handler\n(deref/demote/prefetch)", "X"),
    ],
    "KVCR": [
        (2026.648, "repo public\n(2026-08-24)", "o"),
        (2026.690, "parser adapts to\nenvelope (kvcr#18)", "s"),
        (2026.880, "ingest COPY_TO/MOVE_TO\nstill TODO(kvcr-g3)", "X"),
    ],
    "Dynamo": [
        (2026.867, "router hint policies\n#17359 / #17360", "s"),
        (2026.885, "SGLang sidecar\nmaps envelope", "s"),
        (2026.900, "vLLM sidecar still\nkv_hints: None", "X"),
    ],
}

TIMELINE_XMIN, TIMELINE_XMAX = 2026.40, 2026.98


def _draw_timeline(ax) -> None:
    rows = list(TIMELINE)
    for i, name in enumerate(rows):
        y = len(rows) - 1 - i
        ax.axhline(y, color="#dddddd", linewidth=0.8, zorder=0)
        ax.text(TIMELINE_XMIN - 0.012, y, name, ha="right", va="center", fontsize=8.4,
                fontweight="bold", color=INK)
        pts = sorted(TIMELINE[name], key=lambda t: t[0])
        # 距离过近的点归成一组，组内标签按堆叠高度错开，避免互相压字
        groups, cur = [], []
        for pt in pts:
            if cur and pt[0] - cur[-1][0] < 0.055:
                cur.append(pt)
            else:
                if cur:
                    groups.append(cur)
                cur = [pt]
        if cur:
            groups.append(cur)
        for group in groups:
            for k, (x, label, marker) in enumerate(group):
                color = {"o": "#c0392b", "s": "#2e6da4", "X": "#b9770e"}[marker]
                ax.scatter([x], [y], marker="o" if marker != "X" else "X", s=48,
                           color=color, zorder=3,
                           linewidths=1.5 if marker == "X" else 0)
                dy = 0.18 + 0.24 * k
                ax.plot([x, x], [y + 0.05, y + dy - 0.02], color="#cccccc",
                        linewidth=0.7, zorder=1)
                ax.text(x, y + dy, label, fontsize=5.9, ha="center", va="bottom",
                        color="#333333", linespacing=1.3)

    ax.set_xlim(TIMELINE_XMIN, TIMELINE_XMAX)
    ax.set_ylim(-0.30, len(rows) - 0.05)
    ax.set_yticks([])
    ax.set_xticks([2026.42, 2026.50, 2026.58, 2026.67, 2026.75, 2026.83, 2026.92])
    ax.set_xticklabels(["Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], fontsize=7.4)
    ax.set_xlabel("2026: RFC opened (red) / merged or landed (blue) / still missing (orange X)",
                  fontsize=7.2)
    ax.set_title("(b) envelope first, execution missing (status at 2026-11-13)",
                 fontsize=8.6, pad=8)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)


def evidence() -> None:
    fig = plt.figure(figsize=(14.2, 5.8))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.18], wspace=0.22)
    _draw_matrix(fig.add_subplot(gs[0, 0]))
    _draw_timeline(fig.add_subplot(gs[0, 1]))
    fig.savefig(HERE / "fig-evidence.svg", format="svg")
    plt.close(fig)


def ucm_schematic() -> None:
    fig, ax = plt.subplots(figsize=(8.0, 4.4))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title("UCM: engine plugin over a pluggable store (redrawn)", fontsize=10)

    _box(ax, 0.05, 0.76, 0.90, 0.13,
         "applications / inference engines (vLLM · SGLang · MindIE)", fc=BLUE)
    _box(ax, 0.05, 0.56, 0.90, 0.14,
         "connector layer  KVConnectorBase_V1\n"
         "UCMConnector · UCMDirectConnector · UCMLayerWise · UCMPDConnector", fc=AMBER)
    ax.text(0.06, 0.545,
            "versioned patch tree: ucm/integration/vllm/patch/{0.9.2 … v0300, common}/",
            fontsize=6.6, color="#5a5a5a", style="italic")
    _box(ax, 0.05, 0.34, 0.90, 0.14,
         "unified cache layer  lookup / prefetch / load / dump / wait", fc=GREEN)
    _box(ax, 0.05, 0.12, 0.90, 0.14,
         "global KV store  UcmKVStoreBaseV1  (nfs · posix · mooncake · ds3fs · dram …)", fc=GREEN)
    _arrow(ax, (0.50, 0.76), (0.50, 0.70))
    _arrow(ax, (0.50, 0.56), (0.50, 0.48))
    _arrow(ax, (0.50, 0.34), (0.50, 0.26))

    ax.text(0.05, 0.045,
            "redrawn from unified-cache-management docs/source/_static/architecture.svg (architecture section of "
            "README_zh.md), MIT with additional conditions.\nThe upstream figure is the authority for layout; this "
            "schematic only names the layers used in this comparison.",
            fontsize=6.2, color="#5a5a5a", linespacing=1.5)
    fig.tight_layout()
    fig.savefig(HERE / "fig-ucm-schematic.svg", format="svg")
    plt.close(fig)


def _gbox(ax, x, y, w, h, title, body="", fc="#eef7ec", ec="#4e8a4a",
          fs=7.6, body_fs=6.6):
    """上游 MemCache 图同款：深色标题条 + 浅绿内容区的圆角块。"""
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.003,rounding_size=0.008",
        fc=fc, ec=ec, linewidth=0.9))
    if title:
        ax.text(x + 0.008, y + h - 0.016, title, ha="left", va="top",
                fontsize=fs, color="#1f4d1c", fontweight="bold")
    if body:
        ax.text(x + 0.008, y + h - 0.040, body, ha="left", va="top",
                fontsize=body_fs, color="#333333", linespacing=1.45)


def _thick(ax, xy1, xy2, color="#3f7a3a"):
    """数据面：上游图里的粗块状箭头。"""
    ax.add_patch(FancyArrowPatch(
        xy1, xy2, arrowstyle="-|>", mutation_scale=13, linewidth=3.0,
        color=color, shrinkA=1, shrinkB=1))


def _thin(ax, xy1, xy2, color="#4e8a4a", ls="-"):
    """控制面：上游图里的细箭头。"""
    ax.add_patch(FancyArrowPatch(
        xy1, xy2, arrowstyle="-|>", mutation_scale=8, linewidth=1.1,
        color=color, linestyle=ls, shrinkA=1, shrinkB=1))


def _panel(ax, title, subtitle, strategy_side):
    """画一侧。两侧的四个堆叠层完全相同，只有「策略块在哪」不同。"""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(title, fontsize=9.8, pad=14, fontweight="bold")
    ax.text(0.5, 0.958, subtitle, ha="center", va="top", fontsize=7.2,
            color="#4a4a4a", style="italic")

    # 系统边界
    ax.add_patch(FancyBboxPatch(
        (0.012, 0.055), 0.976, 0.875, boxstyle="round,pad=0.004,rounding_size=0.010",
        fc="none", ec="#7a7a7a", linewidth=1.0, linestyle=(0, (6, 4))))

    # ---- 四层（两侧完全相同）----
    _gbox(ax, 0.035, 0.715, 0.56, 0.165,
          "Engine (vLLM / SGLang / MindIE)",
          "connector:  KVConnectorBase_V1\n"
          "compute:    attention / prefill / decode\n"
          "local KV:   HBM APC",
          fs=7.4, body_fs=6.4)

    _gbox(ax, 0.035, 0.505, 0.56, 0.145,
          "Client Library   (python / C++ API)",
          "IO Proxy  ·  MF Proxy  ·  RPC Client",
          fs=7.2, body_fs=6.4)

    _gbox(ax, 0.035, 0.295, 0.56, 0.155,
          "Meta Service / master",
          "RPC Server · Meta Manager\n"
          "Space Allocator · Locality · Eviction\n"
          "metrics · HA",
          fs=7.2, body_fs=6.4)

    _gbox(ax, 0.035, 0.105, 0.265, 0.105, "GVA memory pool",
          "pooled DRAM", fs=6.8, body_fs=6.2)
    _gbox(ax, 0.330, 0.105, 0.265, 0.105, "SSD pool",
          "local NVMe", fs=6.8, body_fs=6.2)

    # ---- 数据面 / 控制面 ----
    _thick(ax, (0.20, 0.715), (0.20, 0.652))     # engine → client lib
    _thick(ax, (0.13, 0.505), (0.13, 0.212))     # client lib → DRAM pool
    _thick(ax, (0.42, 0.505), (0.42, 0.212))     # client lib → SSD pool
    _thin(ax, (0.50, 0.505), (0.50, 0.452))      # client lib → master (RPC)

    if strategy_side == "engine":
        # (a) 策略是引擎进程内的一块，用另一套配色压在引擎层里
        _gbox(ax, 0.315, 0.735, 0.265, 0.128,
              "KV strategy",
              "prefix radix match\n"
              "eviction order\n"
              "prefetch on hit\n"
              "session retention",
              fc="#fdf3e0", ec="#b58a3a", fs=7.0, body_fs=6.0)
        ax.text(0.4475, 0.715, "inside the engine process", ha="center", va="top",
                fontsize=6.4, color="#8a6520", style="italic")
    else:
        # (b) 策略是池的 client，与引擎平级，通过 master 的 RPC 动手
        _gbox(ax, 0.635, 0.715, 0.335, 0.165,
              "kvcm   (pool client)",
              "placement · retention · TTL\n"
              "prefetch target · cross-engine\n"
              "survival · byte accounting",
              fc="#e8eef9", ec="#4a6fa5", fs=7.2, body_fs=6.2)
        ax.text(0.8025, 0.700, "decides; does not run engines", ha="center",
                va="top", fontsize=6.4, color="#35507a", style="italic")
        # 控制：kvcm → master（点名任务 / 读副本权威）
        _thin(ax, (0.80, 0.715), (0.52, 0.452), color="#4a6fa5", ls=(0, (4, 2)))
        ax.text(0.66, 0.575,
                "CreateCopyTask /\ncreate_move_task /\npin / GetReplicaList",
                fontsize=6.0, color="#35507a", ha="center", va="center",
                linespacing=1.4)

    ax.text(0.022, 0.020,
            "thick = data path     thin = RPC / control path",
            fontsize=6.2, color="#5a5a5a")


def control_placement() -> None:
    """同一组部件，两种控制位置：策略在引擎内 vs 在池的 client 上。"""
    fig, axes = plt.subplots(1, 2, figsize=(14.6, 7.4))
    _panel(
        axes[0],
        "(a) open source: the strategy lives in the engine",
        "connector + per-engine patches; each engine decides for itself",
        "engine",
    )
    _panel(
        axes[1],
        "(b) kvcm: the strategy lives beside the pool",
        "backend-coupled: any engine writing into the same pool is covered",
        "kvcm",
    )

    # 两 panel 之间加一条对比注释
    fig.text(0.5, 0.028,
             "Same boxes on both sides. What moves is the strategy block: "
             "left, it sits inside the engine process and needs engine patches; "
             "right, it is a pool client and reaches the pool through master RPCs "
             "(CreateCopyTask / MoveStart+MoveEnd / pin / GetReplicaList).",
             ha="center", va="bottom", fontsize=7.0, color="#4a4a4a", wrap=True)

    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(HERE / "fig-control-placement.svg", format="svg")
    plt.close(fig)


def main() -> None:
    ownership()
    control_placement()
    evidence()
    ucm_schematic()
    for name in (
        "fig-ownership.svg",
        "fig-control-placement.svg",
        "fig-evidence.svg",
        "fig-ucm-schematic.svg",
    ):
        print(f"wrote {HERE / name}")


if __name__ == "__main__":
    main()
