"""生成 kvcm / KV Fabric 与开源方案对比的自绘图（对照工件）。

口径：kvcm @ `4d9dd33`（2026-11-13）；上游快照 2026-11-13（逐家 commit 见正文头部）。
坐标与打分为**判断**，依据逐条写进正文 §1–§3 的符号级事实表。

运行：
    python3 docs/research/kvcm-vs-oss/figures/plot_kvcm_vs_oss.py

输出（本目录下的**自绘图**，非上游原图）：
    fig-overview.svg        分层归属 + 三种分发/控制形态（左：谁在哪层；右：谁说了算）
    fig-evidence.svg        按控制权的矩阵 + hint 落地时间线（左：权威；右：落地状态）
    fig-ucm-schematic.svg   UCM 分层示意（redrawn from 上游 architecture.svg）

图数刻意压到三张：两张对照工件各承载一组问题，一张是上游架构的重绘。
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


def _canvas(w=8.4, h=6.0):
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    return fig, ax


# ---------------------------------------------------------------------------
# 1. 分层归属
# ---------------------------------------------------------------------------

LAYERS = [
    # (层名, 项目, 底色, 关键词)；层名不用 L 编号：D8 已有 L0/L1/L2 三环，会撞
    ("routing", "llm-d EPP · Dynamo router · AIBrix gateway", BLUE,
     "decides where the request goes; KV follows the request"),
    ("request envelope", "vLLM · SGLang · KVCR (+ Dynamo hint producer)", AMBER,
     "router advises in-band; engine resolves, may drop"),
    ("standalone coordinator", "LMCache mp_coordinator", GREEN,
     "fleet views + commands, event-derived, eventually consistent"),
    ("object pool", "MemCache MetaService · Mooncake master", GREEN,
     "owns bytes: placement, replicas, lease, eviction"),
    ("engine plugin", "UCM · FlexKV (+ AIBrix kvcache, kvcr lib)", BLUE,
     "lives in the engine process; store interface is a library call"),
]


def _draw_layers(ax) -> None:
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title("(a) which layer each project sits in", fontsize=9.4, pad=8)

    # 图层留在左侧 0.02–0.70，右侧 0.72–1.0 留给 kvcm 标注，避免压住层内文字
    top, step = 0.93, 0.168
    for i, (name, who, fc, gloss) in enumerate(LAYERS):
        y = top - i * step
        _box(ax, 0.02, y - 0.132, 0.68, 0.132, "", fc=fc)
        ax.text(0.038, y - 0.028, name, fontsize=8.0, fontweight="bold", color=INK)
        ax.text(0.038, y - 0.070, who, fontsize=6.9, color=INK)
        ax.text(0.038, y - 0.104, gloss, fontsize=6.0, color="#4a4a4a", style="italic")

    # kvcm 是 L2 的 client：从 L2 带右缘引出，落在右侧独立栏位
    y_l2 = top - 3 * step
    _arrow(ax, (0.705, y_l2 - 0.066), (0.745, y_l2 - 0.066), style="->", color="#b03a2e")
    _box(ax, 0.755, y_l2 - 0.104, 0.235, 0.078, "", fc=RED)
    ax.text(0.872, y_l2 - 0.052, "kvcm /\nKV Fabric", ha="center", va="center",
            fontsize=7.4, fontweight="bold", color=INK, linespacing=1.3)
    ax.text(0.872, y_l2 - 0.075, "(this project)", ha="center", va="center",
            fontsize=6.2, color="#5a5a5a")

    ax.text(0.02, 0.015,
            "A project can appear twice when it ships both an envelope and a router (Dynamo).\n"
            "Placement is evidence-backed (file:symbol) for every project; see the tables in §3.",
            fontsize=6.2, color="#5a5a5a", linespacing=1.5)


# ---------------------------------------------------------------------------
# 2. 三种分发/控制形态
# ---------------------------------------------------------------------------

# (标题, 左框, 中框, 右框, 箭头标签, 结论)
MODES = [
    (
        "A. envelope hint — vLLM / SGLang / KVCR",
        ("router / EPP\n(global index)", GREY),
        ("engine + hint consumer", AMBER),
        ("policy accepts, clips\nor drops; then pulls", AMBER),
        "request + kv_hints",
        "decision and cost judgement stay inside the engine: the centre can advise, cannot enforce.\n"
        "On main the envelope is merged, the action handler is not.",
    ),
    (
        "B. centre decides, node executes — object pool",
        ("MetaService / master\n(placement + replicas)", GREEN),
        ("client lib in engine", GREY),
        ("LocalService: I/O only", GREEN),
        "alloc / query -> addr",
        "the centre owns where bytes live and who may read; the node performs I/O.\n"
        "MemCache MetaService/LocalService; Mooncake master/segment.",
    ),
    (
        "C. central command on the pool — kvcm",
        ("kvcm\n(pool client, own UUID)", RED),
        ("master\n(replica authority)", GREY),
        ("seg A source · seg B target\n· kvcm seg", GREY),
        "CreateCopyTask /\ncreate_move_task",
        "names the target segment and can move bytes; the engine does not decide, but the source seg\n"
        "client must be online. Verification: query_task + GetReplicaList -> READY.",
    ),
]


def _draw_modes(ax) -> None:
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title("(b) how control is distributed", fontsize=9.4, pad=8)

    band_h = 1.0 / len(MODES)
    for i, (title, left, mid, right, arrow_label, conclusion) in enumerate(MODES):
        top = 1.0 - i * band_h
        y_row = top - 0.20
        ax.text(0.005, top - 0.030, title, fontsize=8.2, fontweight="bold", color=INK)

        _box(ax, 0.005, y_row, 0.30, 0.105, left[0], fc=left[1], fs=7.0)
        _box(ax, 0.375, y_row, 0.26, 0.105, mid[0], fc=mid[1], fs=7.0)
        _box(ax, 0.705, y_row, 0.29, 0.105, right[0], fc=right[1], fs=7.0)

        _arrow(ax, (0.305, y_row + 0.052), (0.373, y_row + 0.052))
        _arrow(ax, (0.637, y_row + 0.052), (0.703, y_row + 0.052))
        ax.text(0.339, y_row + 0.082, arrow_label, fontsize=5.9, ha="center",
                va="bottom", color="#4a4a4a", linespacing=1.2)

        ax.text(0.005, y_row - 0.012, conclusion, fontsize=6.2, va="top",
                color="#5a5a5a", linespacing=1.45)


def overview() -> None:
    fig = plt.figure(figsize=(13.4, 6.2))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.42], wspace=0.05)
    _draw_layers(fig.add_subplot(gs[0, 0]))
    _draw_modes(fig.add_subplot(gs[0, 1]))
    fig.savefig(HERE / "fig-overview.svg", format="svg")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 3. 决策权 / 执行权 / 权威记录
# ---------------------------------------------------------------------------

AUTH_ROWS = [
    ("UCM (engine plugin)", 2, 1, 0),
    ("FlexKV (engine plugin)", 2, 1, 0),
    ("MemCache (object pool)", 1, 1, 2),
    ("Mooncake (object pool)", 1, 1, 2),
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


# ---------------------------------------------------------------------------
# 4. hint 演进时间线：信封先落地，执行缺位
# ---------------------------------------------------------------------------

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
    fig, ax = _canvas(8.0, 4.4)
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


def main() -> None:
    overview()
    evidence()
    ucm_schematic()
    for name in (
        "fig-overview.svg",
        "fig-evidence.svg",
        "fig-ucm-schematic.svg",
    ):
        print(f"wrote {HERE / name}")


if __name__ == "__main__":
    main()
