"""生成 kvcm / KV Fabric 与开源方案对比的自绘图（对照工件）。

口径：kvcm @ `4d9dd33`（2026-11-13）；上游快照 2026-11-13（逐家 commit 见正文头部）。

正文的两个问题是「数据依附于谁」与「策略在哪」，所以第一张图把它们摊成两条轴：
纵轴是数据的依附层级（请求 → 引擎进程 → 引擎旁挂 → 池），横轴是策略所在的层
（请求内 → 引擎侧进程 → 多实例之上 → 池侧）。只有最下面一行与最右一列同时成立，
才可能在没有请求的时候动 KV。

字体要求（中文图内文字必需）：仓库不捆绑 CJK 字体，字体文件不入仓。
脚本按 `FONT_CANDIDATES` 的顺序查找系统字体，找到就用中文，找不到就打印警告并
退回英文文案（不会硬失败，便于在别的机器与 CI 上跑）。缺字体时想看到中文，装一个
Noto Sans CJK 即可，例如：

    # Debian / Ubuntu
    apt-get install fonts-noto-cjk            # -> /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
    # 或把任意 NotoSansCJK*.ttc 放到 ~/.fonts/ 下

运行：
    python3 docs/research/kvcm-vs-oss/figures/plot_kvcm_vs_oss.py

输出（本目录下的**自绘图**，非上游原图）：
    fig-ownership.svg            主线图：数据依附于谁 × 策略在哪
    fig-control-placement.svg    部件图：策略在引擎内 vs 在池的 client 上
    fig-evidence.svg             证据图：控制权矩阵 + hint 落地时间线

图内文字用中文；项目名、API 名、字段名保留英文原样。
本目录另有上游原图三张（upstream-ucm-architecture.svg、upstream-lmcache-*.png），不由本脚本生成。
"""

from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

HERE = pathlib.Path(__file__).parent

# 候选 CJK 字体：本机临时下载的、发行版包管理器装的、用户家目录里放的。
FONT_CANDIDATES = (
    "/tmp/NotoSansCJKsc.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "~/.fonts/NotoSansCJKsc.ttc",
    "~/.fonts/NotoSansCJK-Regular.ttc",
    "~/Library/Fonts/NotoSansCJKsc.ttc",
)


def _cjk_family() -> str | None:
    """找一个能渲染中文的字族；找不到返回 None。"""
    for raw in FONT_CANDIDATES:
        path = pathlib.Path(raw).expanduser()
        if not path.exists():
            continue
        known = {f.name for f in fm.fontManager.ttflist}
        try:
            fm.fontManager.addfont(str(path))
        except Exception:  # noqa: BLE001 - 坏字体文件不该让脚本挂掉
            continue
        added = [f.name for f in fm.fontManager.ttflist if f.name not in known]
        for name in added:
            if "CJK" in name:
                # 同一个 ttc 里的子字体按 SC 优先，其次任意 CJK。
                return next((n for n in added if "CJK SC" in n), name)
    for name in ("Noto Sans CJK SC", "Noto Sans CJK JP", "Source Han Sans SC"):
        if any(f.name == name for f in fm.fontManager.ttflist):
            return name
    return None


CJK = _cjk_family()
if CJK:
    plt.rcParams["font.family"] = [CJK, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
else:
    print(
        "warning: 未找到 CJK 字体，图内文字退回英文。"
        "装 Noto Sans CJK 后重跑即可输出中文图。"
    )


def t(zh: str, en: str) -> str:
    """有 CJK 字体时用中文，否则退回英文。"""
    return zh if CJK else en


INK = "#222222"
EDGE = "#555555"
BLUE = "#dbe7f5"
GREEN = "#dfeedd"
AMBER = "#f8e6c8"
GREY = "#ececec"
RED = "#f6dcd9"
EMPTY = "#fcfcfc"
EMPTY_EDGE = "#e6e6e6"
WARN = "#b03a2e"


def _box(ax, x, y, w, h, text, fc=GREY, fs=8.0, weight="normal", ec=EDGE):
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0.004,rounding_size=0.010",
            fc=fc, ec=ec, linewidth=0.9,
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, color=INK, fontweight=weight, linespacing=1.35)


def _arrow(ax, xy1, xy2, style="->", ls="-", color="#333333", lw=0.9, scale=9):
    ax.add_patch(
        FancyArrowPatch(xy1, xy2, arrowstyle=style, mutation_scale=scale,
                        linewidth=lw, linestyle=ls, color=color)
    )


# ---------------------------------------------------------------------------
# 1. 主线：数据依附于谁 × 策略在哪
# ---------------------------------------------------------------------------

OWN_ROWS = [
    ("随请求生灭", "请求结束即消失",
     "request-scoped", "dies with the request"),
    ("引擎私有内存", "进程退出即消失",
     "engine memory", "dies with the process"),
    ("旁挂进程", "活过引擎，只覆盖一个引擎",
     "beside the engine", "outlives one engine"),
    ("池", "跨引擎、跨节点",
     "pool", "across engines and nodes"),
]

OWN_COLS = [
    ("请求内\n（信封）", "in the request\n(envelope)"),
    ("引擎侧进程\n（引擎内或旁挂）", "engine-side process\n(in-process or beside)"),
    ("多实例之上\n（协调器）", "above the instances\n(coordinator)"),
    ("池侧\n（master 或池的 client）", "pool side\n(master or pool client)"),
]

# (行, 列) -> (标题, 说明, 底色)
OWN_CELLS = {
    (0, 0): (
        "vLLM / SGLang envelope",
        "只有信封结构，没有动作处理；\n动作窗口 = 这一个请求",
        "envelope only, no action handler;\naction window = this request",
        AMBER,
    ),
    (1, 1): (
        "UCM · FlexKV\nAIBrix kvcache",
        "摸得到 HBM / APC，\n但每个引擎版本一套补丁",
        "can touch HBM / APC,\nbut one patch tree per engine version",
        BLUE,
    ),
    (2, 1): (
        "LMCache MP server\nKVCR + Guard",
        "MP server：L1 在本地，逐实例驱逐；\nGuard：旁挂接管，只覆盖一个引擎",
        "MP server: L1 is local, per-instance eviction;\n"
        "Guard: beside the engine, one engine only",
        GREEN,
    ),
    (2, 2): (
        "LMCache coordinator",
        "只做 L2：配额、驱逐、预取、pin；\n视图由事件派生，最终一致",
        "L2 only: quota, eviction, prefetch, pin;\n"
        "the view is event-derived, eventually consistent",
        GREEN,
    ),
}


def ownership() -> None:
    fig, ax = plt.subplots(figsize=(13.4, 8.2))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(
        t("数据依附于谁 × 策略在哪：两个问题叠起来，才解释得清各家为什么只做得到一段",
          "Who holds the KV x where the policy lives"),
        fontsize=11.5, pad=14,
    )

    grid_x0, col_w, col_gap = 0.212, 0.185, 0.012
    heights, row_gap = (0.152, 0.152, 0.152, 0.190), 0.016

    def col_left(c: int) -> float:
        return grid_x0 + c * (col_w + col_gap)

    tops = []
    y = 0.845
    for h in heights:
        tops.append(y)
        y -= h + row_gap

    # 列头
    for c, (zh, en) in enumerate(OWN_COLS):
        ax.text(col_left(c) + col_w / 2, 0.862, t(zh, en), ha="center", va="bottom",
                fontsize=7.3, color="#3a3a3a", fontweight="bold", linespacing=1.4)
    # 列轴标题
    ax.text(col_left(3) + col_w / 2, 0.965, t("策略在哪 →", "where the policy lives →"),
            ha="center", va="bottom", fontsize=7.6, color=WARN, fontweight="bold")

    # 行标 + 行轴标题
    for r, (zh, gloss, en, en_gloss) in enumerate(OWN_ROWS):
        cy = tops[r] - heights[r] / 2
        color = WARN if r == 3 else INK
        ax.text(0.030, cy + 0.020, t(zh, en),
                ha="left", va="center", fontsize=7.8, color=color, fontweight="bold")
        ax.text(0.030, cy - 0.026, t("— " + gloss, "— " + en_gloss),
                ha="left", va="center", fontsize=6.3, color="#5a5a5a", style="italic")
    ax.text(0.008, 0.50, t("数据依附于谁", "what the data attaches to"),
            ha="center", va="center", fontsize=7.6, color=WARN,
            fontweight="bold", rotation=90)

    # 空格子 + 有内容的格子
    for r in range(4):
        for c in range(4):
            x, y0, h = col_left(c), tops[r] - heights[r], heights[r]
            if (r, c) in OWN_CELLS:
                fc = OWN_CELLS[(r, c)][3]
                ax.add_patch(FancyBboxPatch(
                    (x, y0), col_w, h,
                    boxstyle="round,pad=0.004,rounding_size=0.008",
                    fc=fc, ec=EDGE, linewidth=0.9))
            else:
                ax.add_patch(FancyBboxPatch(
                    (x, y0), col_w, h,
                    boxstyle="round,pad=0.004,rounding_size=0.008",
                    fc=EMPTY, ec=EMPTY_EDGE, linewidth=0.7))

    for (r, c), (title, gloss, en_gloss, _) in OWN_CELLS.items():
        cx, cy = col_left(c) + col_w / 2, tops[r] - heights[r] / 2
        ax.text(cx, cy + 0.030, title, ha="center", va="center",
                fontsize=6.6, color=INK, fontweight="bold", linespacing=1.4)
        ax.text(cx, cy - 0.032, t(gloss, en_gloss), ha="center", va="center",
                fontsize=5.9, color="#3f3f3f", linespacing=1.5)

    # 池这一格：池自身的策略 + kvcm（池的 client），用一条红线分开层次
    cx = col_left(3) + col_w / 2
    top = tops[3]
    ax.text(cx, top - 0.030, "MemCache MetaService\nMooncake master",
            ha="center", va="center", fontsize=6.5, color=INK,
            fontweight="bold", linespacing=1.4)
    ax.text(cx, top - 0.078,
            t("按自身热度策略，\n不感知请求与预算",
              "own heat policy only;\nno view of requests or budget"),
            ha="center", va="center", fontsize=5.9, color="#3f3f3f", linespacing=1.5)
    ax.plot([cx - 0.070, cx + 0.070], [top - 0.112, top - 0.112],
            color=WARN, linewidth=0.9)
    ax.text(cx, top - 0.132, t("kvcm（池的 client）", "kvcm (pool client)"),
            ha="center", va="center", fontsize=6.5, color=WARN, fontweight="bold")
    ax.text(cx, top - 0.168,
            t("权威借 master，\n决策看请求侧输入",
              "authority borrowed from master;\ndecisions read request inputs"),
            ha="center", va="center", fontsize=5.9, color="#3f3f3f", linespacing=1.5)

    # 两条分界：右边一列才够得到池的权威；最下面一行才活过引擎
    x_split = col_left(3) - col_gap / 2
    ax.plot([x_split, x_split], [0.151, 0.845], color=WARN,
            linewidth=1.2, linestyle=(0, (5, 3)))
    y_split = tops[3] + row_gap / 2
    ax.plot([grid_x0, col_left(3) + col_w], [y_split, y_split], color=WARN,
            linewidth=1.2, linestyle=(0, (5, 3)))

    ax.text(0.045, 0.062,
            t("读法：纵轴越往下，KV 活得越久；横轴越往右，策略离请求路径越远。"
              "红线右边才拿得到池的权威（副本在哪个 segment），红线下面才不需要请求就能动 KV。\n"
              "只有最下面一行 × 最右一列同时成立，才有「没事的时候也能搬字节」这个动作窗口——"
              "kvcm 站在这两格的交点上，但它是池的 client，不是池本身。",
              "Reading: the lower the row, the longer the KV lives; the further right the column, "
              "the further the policy sits from the request path."),
            fontsize=6.7, color="#4a4a4a", va="bottom", linespacing=1.75)

    fig.savefig(HERE / "fig-ownership.svg", format="svg")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 2. 决策权 / 执行权 / 权威记录 + hint 落地时间线
# ---------------------------------------------------------------------------

AUTH_ROWS = [
    ("UCM" + t("（引擎插件）", " (engine plugin)"), 2, 1, 0),
    ("FlexKV" + t("（引擎插件）", " (engine plugin)"), 2, 1, 0),
    ("MemCache" + t("（池）", " (pool)"), 1, 1, 2),
    ("Mooncake" + t("（池）", " (pool)"), 1, 1, 2),
    ("vLLM / SGLang" + t("（信封）", " (envelope)"), 1, 0, 0),
    ("KVCR" + t("（信封消费者）", " (envelope consumer)"), 2, 2, 1),
    ("LMCache coordinator", 2, 2, 1),
    ("llm-d EPP" + t("（路由）", " (routing)"), 1, 0, 0),
    ("Dynamo router" + t("（路由）", " (routing)"), 2, 1, 1),
    ("AIBrix gateway" + t("（路由）", " (routing)"), 1, 0, 1),
    ("kvcm" + t("（池的 client）", " (pool client)"), 2, 2, 1),
]
AUTH_COLS = [
    t("决定\n（KV 放哪）", "decides\n(where KV goes)"),
    t("执行\n（动字节）", "enforces\n(acts on bytes)"),
    t("权威记录", "authoritative\nrecord"),
]
AUTH_TXT = {
    (0, 0): t("命中/跳过", "hit/skip"), (0, 1): t("本地存储", "local store"), (0, 2): "-",
    (1, 0): t("命中/跳过", "hit/skip"), (1, 1): t("本地分层", "local tier"), (1, 2): "-",
    (2, 0): t("按次写", "per put"), (2, 1): t("节点", "node"), (2, 2): "meta",
    (3, 0): t("按次写", "per put"), (3, 1): "segment", (3, 2): "master",
    (4, 0): t("引擎，\n未接", "engine,\nunwired"), (4, 1): "-", (4, 2): "-",
    (5, 0): t("策略", "policy"), (5, 1): "NIXL pull", (5, 2): t("仅本地", "local only"),
    (6, 0): "coordinator", (6, 1): "MP server", (6, 2): t("派生", "derived"),
    (7, 0): t("请求", "request"), (7, 1): "-", (7, 2): "-",
    (8, 0): "router", (8, 1): t("引擎", "engine"), (8, 2): t("派生", "derived"),
    (9, 0): t("网关", "gateway"), (9, 1): "-", (9, 2): t("派生", "derived"),
    (10, 0): t("D8 管理面", "D8 mgmt\nplane"),
    (10, 1): t("kvcm 控制面", "kvcm ctrl\nplane"),
    (10, 2): t("master\n（只读）", "master\n(read)"),
}


def _draw_matrix(ax) -> None:
    grid = np.array([[r[1], r[2], r[3]] for r in AUTH_ROWS])
    cmap = matplotlib.colors.ListedColormap([GREY, AMBER, GREEN])
    ax.imshow(grid, cmap=cmap, vmin=0, vmax=2, aspect="auto")
    ax.set_xticks(range(len(AUTH_COLS)))
    ax.set_xticklabels(AUTH_COLS, fontsize=6.9, linespacing=1.45)
    ax.set_yticks(range(len(AUTH_ROWS)))
    ax.set_yticklabels([r[0] for r in AUTH_ROWS], fontsize=6.9)
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            ax.text(j, i, AUTH_TXT.get((i, j), ""), ha="center", va="center",
                    fontsize=6.2, color=INK, linespacing=1.35)
    ax.set_xticks(np.arange(-0.5, 3, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(AUTH_ROWS), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", length=0)
    ax.set_title(
        t("(a) 谁决定 / 谁动字节 / 谁的记录算数\n"
          "绿 = 自己拥有　橙 = 参与但不拥有　灰 = 缺位",
          "(a) who decides / who acts on bytes / who records authority\n"
          "green = owns it, orange = participates but does not own, grey = absent"),
        fontsize=7.9, pad=8, linespacing=1.5,
    )


# (x 月位, 标签, 标记)  标记: o = RFC / 公开, s = 已合入, X = 缺位
# 日期均取自 GitHub issue/PR 的 created_at / closed_at（2026-11-13 核对）。
TIMELINE = {
    "vLLM": [
        (2026.517, t("session_id #48048\n合入", "session_id #48048\nmerged"), "s"),
        (2026.600, "RFC #51428 KvHint", "o"),
        (2026.643, t("#53421 信封\nRFC 提出", "#53421 envelope\nRFC opened"), "o"),
        (2026.704, t("KVCR 分层 #53624\n合入（信封入 main）",
                     "KVCR tier #53624\nmerged (envelope in main)"), "s"),
        (2026.880, t("无动作处理；\nRetention #37003 未闭",
                     "no action handler;\nRetention #37003 open"), "X"),
    ],
    "SGLang": [
        (2026.436, t("RFC #27574\n可编程 KV", "RFC #27574\nprogrammatic KV"), "o"),
        (2026.648, t("信封 #36224\n提出", "envelope #36224\nopened"), "o"),
        (2026.810, t("KVCR 作 HiCache\n后端 #32903", "KVCR as HiCache\nbackend #32903"), "s"),
        (2026.860, "#36224 " + t("合入", "merged"), "s"),
        (2026.890, t("无动作处理\n（deref/demote/prefetch）",
                     "no action handler\n(deref/demote/prefetch)"), "X"),
    ],
    "KVCR": [
        (2026.648, t("仓库公开\n（2026-08-24）", "repo public\n(2026-08-24)"), "o"),
        (2026.690, t("parser 适配\n信封（kvcr#18）", "parser adapts to\nenvelope (kvcr#18)"), "s"),
        (2026.880, t("ingest COPY_TO/MOVE_TO\n仍 TODO(kvcr-g3)",
                     "ingest COPY_TO/MOVE_TO\nstill TODO(kvcr-g3)"), "X"),
    ],
    "Dynamo": [
        (2026.867, t("router hint 策略\n#17359 / #17360",
                     "router hint policies\n#17359 / #17360"), "s"),
        (2026.885, t("SGLang 侧车\n映射信封", "SGLang sidecar\nmaps envelope"), "s"),
        (2026.900, t("vLLM 侧车仍\nkv_hints: None", "vLLM sidecar still\nkv_hints: None"), "X"),
    ],
}

TIMELINE_XMIN, TIMELINE_XMAX = 2026.40, 2026.98


def _draw_timeline(ax) -> None:
    rows = list(TIMELINE)
    for i, name in enumerate(rows):
        y = len(rows) - 1 - i
        ax.axhline(y, color="#dddddd", linewidth=0.8, zorder=0)
        ax.text(TIMELINE_XMIN - 0.012, y, name, ha="right", va="center", fontsize=8.0,
                fontweight="bold", color=INK)
        pts = sorted(TIMELINE[name], key=lambda t_: t_[0])
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
                dy = 0.18 + 0.26 * k
                ax.plot([x, x], [y + 0.05, y + dy - 0.02], color="#cccccc",
                        linewidth=0.7, zorder=1)
                ax.text(x, y + dy, label, fontsize=5.5, ha="center", va="bottom",
                        color="#333333", linespacing=1.35)

    ax.set_xlim(TIMELINE_XMIN, TIMELINE_XMAX)
    ax.set_ylim(-0.30, len(rows) - 0.02)
    ax.set_yticks([])
    ax.set_xticks([2026.42, 2026.50, 2026.58, 2026.67, 2026.75, 2026.83, 2026.92])
    months = ["6 月", "7 月", "8 月", "9 月", "10 月", "11 月", "12 月"]
    ax.set_xticklabels(months if CJK else
                       ["Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
                       fontsize=7.2)
    ax.set_xlabel(
        t("2026 年：RFC 提出（红）／ 已合入或落地（蓝）／ 仍然缺位（橙 X）",
          "2026: RFC opened (red) / merged or landed (blue) / still missing (orange X)"),
        fontsize=6.9,
    )
    ax.set_title(
        t("(b) 信封先到，执行缺位（2026-11-13 状态）",
          "(b) envelope first, execution missing (status at 2026-11-13)"),
        fontsize=8.3, pad=8,
    )
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)


def evidence() -> None:
    fig = plt.figure(figsize=(15.0, 6.0))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.02, 1.18], wspace=0.20)
    _draw_matrix(fig.add_subplot(gs[0, 0]))
    _draw_timeline(fig.add_subplot(gs[0, 1]))
    fig.savefig(HERE / "fig-evidence.svg", format="svg")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 3. UCM 分层示意（重绘）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. LMCache coordinator 数据流（重绘）
# ---------------------------------------------------------------------------

def _gbox(ax, x, y, w, h, title, body="", fc="#eef7ec", ec="#4e8a4a",
          fs=7.2, body_fs=6.2, tc="#1f4d1c"):
    """深色标题条 + 浅色内容区的圆角块。"""
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.003,rounding_size=0.008",
        fc=fc, ec=ec, linewidth=0.9))
    if title:
        ax.text(x + 0.010, y + h - 0.014, title, ha="left", va="top",
                fontsize=fs, color=tc, fontweight="bold")
    if body:
        ax.text(x + 0.010, y + h - 0.046, body, ha="left", va="top",
                fontsize=body_fs, color="#333333", linespacing=1.5)


def _thick(ax, xy1, xy2, color="#3f7a3a"):
    ax.add_patch(FancyArrowPatch(
        xy1, xy2, arrowstyle="-|>", mutation_scale=13, linewidth=2.6,
        color=color, shrinkA=1, shrinkB=1))


def _thin(ax, xy1, xy2, color="#4e8a4a", ls="-", scale=8):
    ax.add_patch(FancyArrowPatch(
        xy1, xy2, arrowstyle="-|>", mutation_scale=scale, linewidth=1.1,
        color=color, linestyle=ls, shrinkA=1, shrinkB=1))




def _panel(ax, title, subtitle, strategy_side):
    """画一侧。两侧都画两个引擎共享同一个池，只让「策略块在哪」不同。"""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(title, fontsize=9.4, pad=14, fontweight="bold")
    ax.text(0.5, 0.968, subtitle, ha="center", va="top", fontsize=6.9,
            color="#4a4a4a", style="italic", linespacing=1.5)

    ax.add_patch(FancyBboxPatch(
        (0.012, 0.030), 0.976, 0.880, boxstyle="round,pad=0.004,rounding_size=0.010",
        fc="none", ec="#7a7a7a", linewidth=1.0, linestyle=(0, (6, 4))))

    # ---- 两个引擎（并排，主栈只占左侧 0.035–0.775，右侧留给 kvcm）----
    e_w, e_h, e_y = 0.355, 0.235, 0.690
    e1_x, e2_x = 0.035, 0.400

    engines = [
        (e1_x, t("引擎 A   vLLM-Ascend", "Engine A   vLLM-Ascend"),
         "AscendStoreConnector"),
        (e2_x, t("引擎 B   SGLang", "Engine B   SGLang"), "HiCache / FlexKV"),
    ]

    for ex, name, strat in engines:
        _gbox(ax, ex, e_y, e_w, e_h, name,
              t("connector：  KVConnectorBase_V1\n"
                "计算：        attention / prefill / decode\n"
                "本地 KV：     HBM APC",
                "connector:  KVConnectorBase_V1\n"
                "compute:    attention / prefill / decode\n"
                "local KV:   HBM APC"),
              fs=6.9, body_fs=5.9)
        # (a) 策略在引擎进程内：每个引擎里各一份，写出具体开源方案名
        if strategy_side == "engine":
            _gbox(ax, ex + 0.007, e_y + 0.007, e_w - 0.014, 0.112,
                  t("策略在引擎内（本进程）", "KV strategy (in THIS process)"),
                  f"{strat}\n" + t("radix · 驱逐 · 预取 · 保留",
                                   "radix · eviction · prefetch\nretention"),
                  fc="#fdf3e0", ec="#b58a3a", fs=6.2, body_fs=5.5)

    if strategy_side == "engine":
        ax.text(0.405, e_y - 0.012,
                t("同一个策略存两份，一个引擎一份：\n"
                  "改 vLLM-Ascend、改 SGLang —— 每个引擎版本一套补丁",
                  "two copies of the strategy, one per engine:\n"
                  "patch vLLM-Ascend, patch SGLang - one patch tree per engine version"),
                ha="center", va="top", fontsize=6.1, color="#8a6520",
                style="italic", linespacing=1.6)

    # ---- Client Library：每个引擎一个（数据面直连池）----
    cl_y, cl_h = 0.425, 0.100
    for ex in (e1_x, e2_x):
        _gbox(ax, ex, cl_y, e_w, cl_h,
              t("Client Library（python / C++ API）", "Client Library  (python / C++ API)"),
              "IO Proxy · MF Proxy · RPC Client",
              fs=6.4, body_fs=5.7)

    # ---- 共享池：一个整体区域，两个引擎都连进来 ----
    pool_x, pool_y, pool_w, pool_h = 0.035, 0.050, 0.740, 0.330
    ax.add_patch(FancyBboxPatch(
        (pool_x, pool_y), pool_w, pool_h,
        boxstyle="round,pad=0.004,rounding_size=0.010",
        fc="#f4faf3", ec="#3f7a3a", linewidth=1.2))
    ax.text(pool_x + 0.010, pool_y + pool_h - 0.012,
            t("THE POOL：两个引擎共用一个池（Mooncake / MemCache）",
              "THE POOL - shared by both engines   (Mooncake / MemCache)"),
            fontsize=6.9, fontweight="bold", color="#1f4d1c", va="top")

    _gbox(ax, pool_x + 0.012, 0.240, pool_w - 0.024, 0.088,
          "Meta Service / master",
          t("RPC Server · Meta Manager · 空间分配 · 局部性 · 驱逐 · HA",
            "RPC Server · Meta Manager · Space Allocator · Locality · Eviction · HA"),
          fs=6.4, body_fs=5.6)
    _gbox(ax, pool_x + 0.012, 0.062, 0.345, 0.076, t("GVA 内存池", "GVA memory pool"),
          t("池化的 DRAM", "pooled DRAM"), fs=6.0, body_fs=5.5)
    _gbox(ax, pool_x + 0.380, 0.062, 0.345, 0.076, t("SSD 池", "SSD pool"),
          t("本机 NVMe", "local NVMe"), fs=6.0, body_fs=5.5)
    _thin(ax, (pool_x + 0.185, 0.240), (pool_x + 0.185, 0.138))
    _thin(ax, (pool_x + 0.552, 0.240), (pool_x + 0.552, 0.138))

    # ---- 数据面：每个引擎各有一条粗箭头进池 ----
    for cx in (e1_x + e_w / 2, e2_x + e_w / 2):
        _thick(ax, (cx, e_y), (cx, cl_y + cl_h))
        _thick(ax, (cx, cl_y), (cx, pool_y + pool_h))

    # (b) 策略是池的一个 client：一份，在池外右侧，只经 master 动手
    if strategy_side == "kvcm":
        kx, kw, ky, kh = 0.793, 0.190, 0.145, 0.360
        ax.add_patch(FancyBboxPatch(
            (kx, ky), kw, kh, boxstyle="round,pad=0.003,rounding_size=0.008",
            fc="#e8eef9", ec="#4a6fa5", linewidth=1.1))
        ax.text(kx + kw / 2, ky + kh - 0.012, "kvcm", ha="center", va="top",
                fontsize=8.0, fontweight="bold", color="#26405f")
        ax.text(kx + kw / 2, ky + kh - 0.040,
                t("一个池 client\n覆盖两个引擎", "one pool client\ncovers BOTH engines"),
                ha="center", va="top", fontsize=5.7, color="#26405f",
                linespacing=1.5)
        ax.text(kx + 0.008, ky + 0.205,
                t("放置\n保留 · TTL\n预取目标\n跨引擎存活\n字节记账",
                  "placement\nretention · TTL\nprefetch target\ncross-engine survival\n"
                  "byte accounting"),
                ha="left", va="top", fontsize=5.6, color="#333333",
                linespacing=1.6)
        # 只一条短控制线：kvcm → master，穿过池边界
        _thin(ax, (kx, 0.288), (pool_x + pool_w, 0.288),
              color="#4a6fa5", ls=(0, (4, 2)))
        ax.text(kx + kw / 2, ky + 0.008,
                t("CreateCopyTask\ncreate_move_task\npin / GetReplicaList",
                  "CreateCopyTask\ncreate_move_task\npin / GetReplicaList"),
                ha="center", va="bottom", fontsize=5.1, color="#35507a",
                linespacing=1.5)
        ax.text(0.405, e_y - 0.012,
                t("引擎里不带放置策略；换引擎对 kvcm 没有影响 ——\n"
                  "它对接的是池，不是引擎",
                  "engines carry no placement strategy; swapping an engine\n"
                  "changes nothing in kvcm - it talks to the pool, not the engine"),
                ha="center", va="top", fontsize=6.1, color="#35507a",
                style="italic", linespacing=1.6)

    ax.text(0.022, 0.014,
            t("粗线 = 数据面　　细线 = RPC / 控制面",
              "thick = data path     thin = RPC / control path"),
            fontsize=5.9, color="#5a5a5a")


def control_placement() -> None:
    """两个引擎共享一个池；策略在引擎内（各一份）vs 在池的 client 上（一份）。"""
    fig, axes = plt.subplots(1, 2, figsize=(15.2, 7.9))
    _panel(
        axes[0],
        t("(a) 策略在引擎内：一个引擎一份", "(a) open source: one strategy copy per engine"),
        t("connector + 逐引擎补丁；每个引擎各自决定",
          "connector + per-engine patches; each engine decides for itself"),
        "engine",
    )
    _panel(
        axes[1],
        t("(b) 策略在池的 client 上：全池一份", "(b) kvcm: one strategy for the shared pool"),
        t("backend-coupled：写进这个池的引擎都被覆盖",
          "backend-coupled: any engine writing into the pool is covered"),
        "kvcm",
    )

    fig.text(0.5, 0.024,
             t("两侧部件相同：两个引擎、一个共享池。变的只是策略块在哪 —— "
               "左边在每个引擎进程内（要改引擎），右边是一个池 client 经 master RPC 动手。",
               "Same boxes on both sides: two engines, one shared pool. What changes is where "
               "the strategy block lives - left, inside each engine process (needs engine "
               "patches); right, one pool client reaching the pool through master RPCs."),
             ha="center", va="bottom", fontsize=6.9, color="#4a4a4a")

    fig.tight_layout(rect=(0, 0.048, 1, 1))
    fig.savefig(HERE / "fig-control-placement.svg", format="svg")
    plt.close(fig)


def main() -> None:
    ownership()
    control_placement()
    evidence()
    for name in (
        "fig-ownership.svg",
        "fig-control-placement.svg",
        "fig-evidence.svg",
    ):
        print(f"wrote {HERE / name}")


if __name__ == "__main__":
    main()
