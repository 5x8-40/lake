"""生成 kvcm / KV Fabric 与开源方案对比的自绘图（对照工件）。

口径：kvcm @ `4d9dd33`（2026-11-13）；上游快照 2026-11-13（逐家 commit 见正文头部）。
坐标与打分为**判断**，依据逐条写进正文 §1–§3 的符号级事实表。

运行：
    python3 docs/research/kvcm-vs-oss/figures/plot_kvcm_vs_oss.py

输出（全部为本目录下的**自绘图**，非上游原图）：
    fig-layer-map.svg       分层归属（谁在哪层，kvcm 站哪）
    fig-control-modes.svg   三种分发/控制形态
    fig-authority-matrix.svg 决策权 / 执行权 / 权威记录三分
    fig-hint-timeline.svg   信封先落地、执行缺位
    fig-ucm-schematic.svg   UCM 分层示意（redrawn from 上游 architecture.svg）

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
            boxstyle="round,pad=0.006,rounding_size=0.012",
            fc=fc, ec=EDGE, linewidth=0.9,
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, color=INK, fontweight=weight, linespacing=1.35)


def _bracket(ax, x, y0, y1, label, side="left"):
    tx = x - 0.012 if side == "left" else x + 0.012
    ha = "right" if side == "left" else "left"
    ax.plot([x, x], [y0, y1], color="#999999", linewidth=1.0)
    ax.text(tx, (y0 + y1) / 2, label, ha=ha, va="center", fontsize=7.5,
            color="#5a5a5a", linespacing=1.3)


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
    # (层名, 项目, 底色, 关键词)
    ("L5  routing layer", "llm-d EPP · Dynamo router · AIBrix gateway", BLUE,
     "decides where the request goes; KV follows the request"),
    ("L4  request-envelope hint", "vLLM · SGLang · KVCR (+ Dynamo hint producer)", AMBER,
     "router advises in-band; engine resolves, may drop"),
    ("L3  standalone coordinator", "LMCache mp_coordinator", GREEN,
     "fleet views + commands, event-derived, eventually consistent"),
    ("L2  object-pool metadata", "MemCache MetaService · Mooncake master", GREEN,
     "owns bytes: placement, replicas, lease, eviction"),
    ("L1  engine plugin", "UCM · FlexKV (+ AIBrix kvcache, kvcr lib)", BLUE,
     "lives in the engine process; store interface is a library call"),
]


def layer_map() -> None:
    fig, ax = _canvas(9.0, 5.6)
    ax.set_title("Where each project sits (layer first, then capability)",
                 fontsize=10.5, pad=10)

    top, step = 0.90, 0.163
    for i, (name, who, fc, gloss) in enumerate(LAYERS):
        y = top - i * step
        _box(ax, 0.03, y - 0.128, 0.94, 0.128, "", fc=fc)
        ax.text(0.055, y - 0.030, name, fontsize=8.6, fontweight="bold", color=INK)
        ax.text(0.055, y - 0.072, who, fontsize=7.8, color=INK)
        ax.text(0.055, y - 0.105, gloss, fontsize=6.8, color="#4a4a4a", style="italic")

    # kvcm 是 L2 的 client：从 L2 带右侧引出
    y_l2 = top - 3 * step
    _arrow(ax, (0.80, y_l2 - 0.064), (0.905, y_l2 - 0.064), style="->", color="#b03a2e")
    _box(ax, 0.62, y_l2 - 0.128, 0.30, 0.058,
         "kvcm / KV Fabric (this project)", fc=RED, fs=7.6, weight="bold")
    ax.text(0.62, y_l2 - 0.145,
            "client of the pool: issues copy/move/pin, reads master records",
            fontsize=6.4, color="#5a5a5a", style="italic")

    ax.text(0.03, 0.035,
            "Placement is evidence-backed (file:symbol) for every project; see the tables in the text.\n"
            "A project can appear twice when it ships both an envelope and a router (Dynamo).",
            fontsize=6.8, color="#5a5a5a", linespacing=1.5)
    fig.tight_layout()
    fig.savefig(HERE / "fig-layer-map.svg", format="svg")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 2. 三种分发/控制形态
# ---------------------------------------------------------------------------


def control_modes() -> None:
    fig, axes = plt.subplots(1, 3, figsize=(11.6, 4.2))
    for ax in axes:
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

    # (a) 信封 hint
    a = axes[0]
    a.set_title("A. envelope hint (vLLM / SGLang / KVCR)", fontsize=8.8)
    _box(a, 0.04, 0.76, 0.40, 0.14, "router / EPP\n(global index)")
    _box(a, 0.56, 0.76, 0.40, 0.16, "engine\n+ hint consumer lib", fc=AMBER)
    _box(a, 0.56, 0.46, 0.40, 0.16, "policy decides\naccept / clip / drop", fc=AMBER)
    _box(a, 0.56, 0.14, 0.40, 0.16, "local DRAM / peer\n(NIXL, destination pulls)")
    _arrow(a, (0.44, 0.83), (0.56, 0.83))
    a.text(0.50, 0.90, "request\n+ kv_hints", fontsize=6.4, ha="center")
    _arrow(a, (0.76, 0.76), (0.76, 0.62))
    _arrow(a, (0.76, 0.46), (0.76, 0.30))
    a.text(0.04, 0.56,
           "decision + cost judgement\nstay inside the engine;\n"
           "the centre can advise,\ncannot enforce",
           fontsize=7.0, va="top", color=INK, linespacing=1.45)
    a.text(0.56, 0.055, "vLLM / SGLang main: envelope merged, no action handler",
           fontsize=6.2, style="italic", color="#5a5a5a")

    # (b) 中心分发、节点执行
    b = axes[1]
    b.set_title("B. centre decides, node executes (object pool)", fontsize=8.8)
    _box(b, 0.04, 0.78, 0.42, 0.14, "MetaService / master\n(placement + replicas)", fc=GREEN)
    _box(b, 0.56, 0.78, 0.40, 0.12, "client lib (in engine)")
    _box(b, 0.56, 0.46, 0.40, 0.18, "LocalService\nIO / MF proxy", fc=GREEN)
    _box(b, 0.56, 0.14, 0.40, 0.16, "GVA memory pool\n/ SSD pool")
    _arrow(b, (0.46, 0.85), (0.56, 0.85))
    b.text(0.51, 0.91, "alloc / query\n→ addr", fontsize=6.3, ha="center")
    _arrow(b, (0.76, 0.78), (0.76, 0.64))
    _arrow(b, (0.76, 0.46), (0.76, 0.30))
    b.text(0.04, 0.60,
           "the centre owns where bytes\nlive and who may read;\n"
           "the node only performs I/O",
           fontsize=7.0, va="top", color=INK, linespacing=1.45)
    b.text(0.56, 0.055, "MemCache MetaService/LocalService; Mooncake master/segment",
           fontsize=6.2, style="italic", color="#5a5a5a")

    # (c) 中心命令（kvcm）
    c = axes[2]
    c.set_title("C. central command on the pool (kvcm)", fontsize=8.8)
    _box(c, 0.02, 0.72, 0.40, 0.16, "kvcm\n(pool client, own UUID)", fc=RED)
    _box(c, 0.54, 0.80, 0.42, 0.12, "master\n(replica authority)")
    _box(c, 0.54, 0.48, 0.42, 0.22, "seg A (source engine)\nseg B (target engine)\nkvcm seg")
    _box(c, 0.20, 0.12, 0.44, 0.16, "ledger: keepalive / tasks")
    _arrow(c, (0.42, 0.80), (0.54, 0.86))
    c.text(0.48, 0.92, "CreateCopyTask /\ncreate_move_task", fontsize=6.2, ha="center")
    _arrow(c, (0.54, 0.74), (0.42, 0.79))
    c.text(0.48, 0.66, "GetReplicaList → READY", fontsize=6.2, ha="center")
    _arrow(c, (0.75, 0.80), (0.75, 0.70))
    _arrow(c, (0.30, 0.72), (0.40, 0.28))
    c.text(0.02, 0.52,
           "names the target segment\nand can move bytes;\n"
           "engine does not decide —\nbut the source seg client\nmust be online",
           fontsize=7.0, va="top", color=INK, linespacing=1.45)

    fig.tight_layout()
    fig.savefig(HERE / "fig-control-modes.svg", format="svg")
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


def authority_matrix() -> None:
    grid = np.array([[r[1], r[2], r[3]] for r in AUTH_ROWS])
    fig, ax = plt.subplots(figsize=(7.4, 5.4))
    cmap = matplotlib.colors.ListedColormap([GREY, AMBER, GREEN])
    ax.imshow(grid, cmap=cmap, vmin=0, vmax=2, aspect="auto")
    ax.set_xticks(range(len(AUTH_COLS)))
    ax.set_xticklabels(AUTH_COLS, fontsize=8)
    ax.set_yticks(range(len(AUTH_ROWS)))
    ax.set_yticklabels([r[0] for r in AUTH_ROWS], fontsize=8)
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            ax.text(j, i, AUTH_TXT.get((i, j), ""), ha="center", va="center",
                    fontsize=7, color=INK)
    ax.set_xticks(np.arange(-0.5, 3, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(AUTH_ROWS), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", length=0)
    ax.set_title(
        "green = holds it / owns it, orange = participates but does not own, grey = absent",
        fontsize=8.4,
    )
    fig.tight_layout()
    fig.savefig(HERE / "fig-authority-matrix.svg", format="svg")
    plt.close(fig)


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


def hint_timeline() -> None:
    fig, ax = plt.subplots(figsize=(10.6, 5.4))
    rows = list(TIMELINE)
    for i, name in enumerate(rows):
        y = len(rows) - 1 - i
        ax.axhline(y, color="#dddddd", linewidth=0.8, zorder=0)
        ax.text(TIMELINE_XMIN - 0.012, y, name, ha="right", va="center", fontsize=9,
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
                ax.scatter([x], [y], marker="o" if marker != "X" else "X", s=54,
                           color=color, zorder=3,
                           linewidths=1.5 if marker == "X" else 0)
                dy = 0.20 + 0.26 * k
                ax.plot([x, x], [y + 0.05, y + dy - 0.02], color="#cccccc",
                        linewidth=0.7, zorder=1)
                ax.text(x, y + dy, label, fontsize=6.2, ha="center", va="bottom",
                        color="#333333", linespacing=1.3)

    ax.set_xlim(TIMELINE_XMIN, TIMELINE_XMAX)
    ax.set_ylim(-0.30, len(rows) - 0.05)
    ax.set_yticks([])
    ax.set_xticks([2026.42, 2026.50, 2026.58, 2026.67, 2026.75, 2026.83, 2026.92])
    ax.set_xticklabels(["Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], fontsize=8)
    ax.set_xlabel("2026: RFC opened (red) / merged or landed (blue) / still missing (orange X)",
                  fontsize=7.6)
    ax.set_title("Envelope first, execution missing: hint landing status at 2026-11-13",
                 fontsize=10)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    fig.tight_layout()
    fig.savefig(HERE / "fig-hint-timeline.svg", format="svg")
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
    layer_map()
    control_modes()
    authority_matrix()
    hint_timeline()
    ucm_schematic()
    for name in (
        "fig-layer-map.svg",
        "fig-control-modes.svg",
        "fig-authority-matrix.svg",
        "fig-hint-timeline.svg",
        "fig-ucm-schematic.svg",
    ):
        print(f"wrote {HERE / name}")


if __name__ == "__main__":
    main()
