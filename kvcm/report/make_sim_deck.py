"""生成 KVCM-仿真结论-20261126.pptx（python-pptx）。

数字全部摘自 kvcm/sim/results/{summary.md, pd/summary.md, locality.md} 与 kvcm/sim/{params.md, README.md}；
版式沿用 KVCM-汇报-含PD混部-20261124.pptx 的配色与字体。
用法：python3 kvcm/report/make_sim_deck.py
"""
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

HERE = Path(__file__).resolve().parent
RES = HERE.parent / "sim" / "results"
FIG = RES / "figures"
OUT = HERE / "KVCM-仿真结论-20261126.pptx"

NAVY = RGBColor(0x0F, 0x25, 0x4A)
DEEP = RGBColor(0x0B, 0x1C, 0x38)
RED = RGBColor(0xC8, 0x10, 0x2E)
GOLD = RGBColor(0xFD, 0xE6, 0x8A)
INK = RGBColor(0x1B, 0x24, 0x32)
GRAY = RGBColor(0x74, 0x82, 0x96)
LINE = RGBColor(0xD8, 0xE1, 0xEC)
PALE = RGBColor(0xF8, 0xFA, 0xFC)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
SOFT = RGBColor(0xCB, 0xD5, 0xE1)
CARD_DARK = RGBColor(0x1E, 0x3A, 0x6E)
TEAL = RGBColor(0x0D, 0x76, 0x80)
AMBER = RGBColor(0xC2, 0x6A, 0x0A)
GREEN = RGBColor(0x15, 0x80, 0x3D)
FONT = "Noto Sans CJK SC"
TOTAL = 9

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]


def rect(s, x, y, w, h, color, shape=MSO_SHAPE.RECTANGLE, line=None):
    sh = s.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = color
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line
        sh.line.width = Pt(0.75)
    sh.shadow.inherit = False
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        sh.adjustments[0] = 0.08
    return sh


def text(s, x, y, w, h, paras, size=12, color=INK, bold=False, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, spacing=1.1):
    """paras: str | list[str | list[(text, {overrides})]]"""
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.04)
    tf.margin_top = tf.margin_bottom = Inches(0.02)
    if isinstance(paras, str):
        paras = [paras]
    for i, p in enumerate(paras):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align
        para.line_spacing = spacing
        runs = [(p, {})] if isinstance(p, str) else p
        for t, o in runs:
            r = para.add_run()
            r.text = t
            f = r.font
            f.name = FONT
            f.size = Pt(o.get("size", size))
            f.bold = o.get("bold", bold)
            f.color.rgb = o.get("color", color)
    return tb


def header(s, section, title, page):
    rect(s, 0, 0, 13.333, 0.06, NAVY)
    rect(s, 0, 0, 2.3, 0.06, RED)
    pill = rect(s, 0.5, 0.26, 1.48, 0.34, RED, MSO_SHAPE.ROUNDED_RECTANGLE)
    pill.adjustments[0] = 0.22
    text(s, 0.5, 0.26, 1.48, 0.34, section, size=10.5, color=WHITE, bold=True,
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 2.12, 0.16, 9.6, 0.86, title, size=19, color=NAVY, bold=True,
         anchor=MSO_ANCHOR.MIDDLE, spacing=1.0)
    text(s, 11.8, 0.26, 1.03, 0.34,
         [[(f"{page:02d}", {"color": RED, "size": 13, "bold": True}),
           (f" / {TOTAL:02d}", {"color": GRAY, "size": 10.5})]],
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
    rect(s, 0.5, 1.08, 12.33, 0.012, LINE)


def footer(s, source):
    rect(s, 0.5, 7.12, 12.33, 0.01, LINE)
    text(s, 0.5, 7.15, 9.4, 0.26, f"数据：{source}", size=8.5, color=GRAY)
    text(s, 9.9, 7.15, 2.93, 0.26, "KVCM 仿真结论 · 2026-11-26", size=8.5, color=GRAY,
         align=PP_ALIGN.RIGHT)


def card(s, x, y, w, h, big, caption, accent=RED, big_size=24):
    rect(s, x, y, w, h, PALE, MSO_SHAPE.ROUNDED_RECTANGLE, line=LINE)
    rect(s, x, y + 0.12, 0.07, h - 0.24, accent)
    text(s, x + 0.2, y + 0.1, w - 0.3, 0.56, big, size=big_size, color=accent, bold=True,
         anchor=MSO_ANCHOR.MIDDLE, spacing=1.0)
    text(s, x + 0.2, y + 0.68, w - 0.3, h - 0.76, caption, size=11, color=INK, spacing=1.15)


def cards(s, items, x=8.75, y=1.3, w=4.08, h=1.82, gap=0.11, big_size=24):
    for i, it in enumerate(items):
        big, caption, *rest = it
        card(s, x, y + i * (h + gap), w, h, big, caption, rest[0] if rest else RED, big_size)


def table(s, x, y, col_w, rows, size=10.5, row_h=0.3, hl_rows=(), hl_color=RGBColor(0xFD, 0xF2, 0xF4),
          left=False):
    nr, nc = len(rows), len(rows[0])
    gf = s.shapes.add_table(nr, nc, Inches(x), Inches(y), Inches(sum(col_w)), Inches(row_h * nr))
    tbl = gf.table
    tbl.first_row = True
    for j, cw in enumerate(col_w):
        tbl.columns[j].width = Inches(cw)
    for i in range(nr):
        tbl.rows[i].height = Inches(row_h)
        for j in range(nc):
            c = tbl.cell(i, j)
            c.margin_left = c.margin_right = Inches(0.06)
            c.margin_top = c.margin_bottom = Inches(0.02)
            c.vertical_anchor = MSO_ANCHOR.MIDDLE
            c.fill.solid()
            if i == 0:
                c.fill.fore_color.rgb = NAVY
            elif i in hl_rows:
                c.fill.fore_color.rgb = hl_color
            else:
                c.fill.fore_color.rgb = WHITE if i % 2 else PALE
            tf = c.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT if (j == 0 or left) else PP_ALIGN.CENTER
            r = p.add_run()
            r.text = str(rows[i][j])
            r.font.name = FONT
            r.font.size = Pt(size)
            r.font.bold = i == 0 or i in hl_rows
            r.font.color.rgb = WHITE if i == 0 else INK
    return gf


def picture(s, path, x, y, w=None, h=None):
    kw = {}
    if w:
        kw["width"] = Inches(w)
    if h:
        kw["height"] = Inches(h)
    return s.shapes.add_picture(str(path), Inches(x), Inches(y), **kw)


def note(s, x, y, w, h, t, size=10):
    rect(s, x, y, w, h, NAVY)
    rect(s, x, y, 0.1, h, RED)
    text(s, x + 0.2, y, w - 0.3, h, t, size=size, color=GOLD, anchor=MSO_ANCHOR.MIDDLE)


# ---------- 1 总结论 ----------
s = prs.slides.add_slide(BLANK)
s.background.fill.solid()
s.background.fill.fore_color.rgb = DEEP
rect(s, 8.82, 0, 4.513, 7.5, NAVY)
rect(s, 8.82, 0, 0.07, 7.5, RED)
rect(s, 0, 0, 13.333, 0.08, RED)
p = rect(s, 0.78, 0.9, 2.9, 0.38, RED, MSO_SHAPE.ROUNDED_RECTANGLE)
p.adjustments[0] = 0.22
text(s, 0.78, 0.9, 2.9, 0.38, "KVCM trace 回放仿真 · 结论", size=11, color=WHITE, bold=True,
     align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
text(s, 0.78, 1.45, 7.8, 0.9, "KVCM 仿真结论", size=40, color=WHITE, bold=True)
text(s, 0.78, 2.45, 7.8, 1.5,
     [[("价值在 ", {}), ("保留", {"color": GOLD, "bold": True}), (" 和 ", {}),
       ("PD 动态转换", {"color": GOLD, "bold": True}), ("，", {})],
      "不在局部性和冷热复制；",
      [("保留收益要靠 ", {}), ("理想 hook", {"color": GOLD, "bold": True}), ("。", {})]],
     size=22, color=WHITE, spacing=1.2)
rect(s, 0.78, 4.15, 1.65, 0.05, RED)
table(s, 0.78, 4.4, [1.55, 6.15], [
    ["方向", "结论（关键数字）"],
    ["保留", "CC 3.5 TB 池：理想 hook NPU 251.9 → 210.4，现实 hook 仅 250.9"],
    ["PD 动态转换", "【注入】S3 goodput +18.6%（滞后控制器 + 池 +13.5%）"],
    ["局部性", "单因素压力下 TTFT P95 变化 < 2%；只在最坏组合起作用"],
    ["冷热复制", "被动副本占池 28%，读池 P95 +18%；热点复制单因素下不触发"],
], size=11, row_h=0.42, left=True)
text(s, 0.78, 6.75, 7.8, 0.4,
     "DeepSeek V4.1-Flash · Ascend A3 14 节点 × 16 die · 三条 trace · 单次运行（seed=1），无真机校准",
     size=10.5, color=SOFT)
text(s, 9.2, 0.9, 3.8, 0.4, "目录", size=13.5, color=GOLD, bold=True)
toc = [("01", "总结论"), ("02", "仿真口径"), ("03", "命中率与 NPU / TTFT"),
       ("04", "KVCM 增益区间【注入】"), ("05", "PD 动态转换 goodput【注入】"),
       ("06", "局部性与冷热复制"), ("07", "压力与最坏组合【注入】"),
       ("08", "设计建议"), ("09", "口径与局限")]
for i, (n, t) in enumerate(toc):
    yy = 1.45 + i * 0.6
    rect(s, 9.2, yy, 3.75, 0.5, CARD_DARK)
    rect(s, 9.2, yy, 0.07, 0.5, [RED, TEAL, AMBER, GREEN][i % 4])
    text(s, 9.4, yy, 3.5, 0.5, [[(n + "  ", {"color": GOLD, "bold": True}), (t, {})]],
         size=12, color=WHITE, anchor=MSO_ANCHOR.MIDDLE)

# ---------- 2 仿真口径 ----------
s = prs.slides.add_slide(BLANK)
header(s, "01  口径", "仿真口径：DeepSeek V4.1-Flash 跑在 A3 14 × 16 集群，HBM 由引擎管、KVCM 只管 DRAM 池", 2)
table(s, 0.5, 1.3, [2.0, 6.0], [
    ["项", "取值"],
    ["模型", "DeepSeek V4.1-Flash；全局 KV 按 BF16 存 3,200 B/token（官方 FP4 为 890 B）"],
    ["集群", "Ascend A3，14 节点 × 16 die = 224 NPU"],
    ["HBM", "gpu_memory_utilization 0.85 → 18.5 GB/die → 每实例 296 GB（全集群 4.15 TB），引擎纯 LRU"],
    ["DRAM 池", "KVCM 管理，标称 14 TB（14 节点 × 1 TB）；收益工作区间 ≈ 1–2× 集群 HBM（4.1–8.3 TB）"],
    ["回放", "切片 1/32，主线窗口 10 h（前 3 h 预热），单次运行 seed=1"],
], size=10.5, row_h=0.5, left=True)
table(s, 0.5, 4.5, [2.6, 1.6, 1.6, 2.2], [
    ["trace（全规模）", "req/s", "理想命中 h*", "备注"],
    ["AgentX Claude Code 256k", "383.1", "96.24%", "10,871 会话/h"],
    ["Codex × SWE-bench Pro", "543.0", "96.54%", "时间线为合成"],
    ["Qwen-Bailian coder", "735.7", "70.97%", "会话短、首轮占 62%"],
], size=10.5, row_h=0.42)
cards(s, [
    ("3,200 B/token", "BF16 全局 KV；FP4 口径 890 B 只用于敏感度对照"),
    ("296 GB / 实例", "每 die 64 GB × 0.85 − 权重 31.9 − 激活 4 = 18.5 GB，× 16 die"),
    ("3 条 trace", "Claude Code、Codex、Qwen coder；HBM + DRAM 池两层"),
], big_size=22)
footer(s, "kvcm/sim/params.md、kvcm/sim/results/summary.md")

# ---------- 3 命中率与 NPU/TTFT ----------
s = prs.slides.add_slide(BLANK)
header(s, "02  保留", "池在 1–2× HBM 区间，理想 hook 比共享池多命中 0.7–3.45 pt；Codex 已贴着 h*，无增益", 3)
picture(s, FIG / "mrc_cc256k.png", 0.5, 1.22, w=8.0)
table(s, 0.5, 4.95, [2.6, 1.3, 1.6, 1.25, 1.25], [
    ["CC · DRAM 池 3.5 TB", "命中率", "prefill NPU", "TTFT P95", "相对共享池"],
    ["shared-pool LRU", "91.56%", "251.9", "2.38 s", "—"],
    ["KVCM（现实 hook）", "91.60%", "250.9", "2.31 s", "−0.9 NPU"],
    ["KVCM（理想 hook）", "93.03%", "210.4", "1.64 s", "−41.5 NPU"],
], size=10.5, row_h=0.4, hl_rows=(3,))
text(s, 0.5, 6.62, 8.0, 0.45,
     "1–2× HBM（4.15 / 8.29 TB）理想 hook − 共享池：CC +1.39 / +0.71 pt，Qwen +3.45 / +1.28 pt",
     size=10, color=GRAY)
cards(s, [
    ("251.9 → 210.4", "CC 3.5 TB 池，理想 hook 的 prefill 占用 NPU（−41.5）；现实 hook 只省 0.9"),
    ("2.38 → 1.64 s", "同一配置 TTFT P95；1–10 min 停顿组复用损失 42.7% → 23.1%"),
    ("Codex ≈ 0", "共享池 3.5 TB 已达 96.49%（h* 96.54%）：停顿 96% < 60 s，没有可保留的长停顿", GRAY),
], big_size=23)
footer(s, "kvcm/sim/results/summary.md（MRC 表、TTFT 与等效 NPU 表，DRAM 池 0.25 × 标称）")

# ---------- 4 增益区间 ----------
s = prs.slides.add_slide(BLANK)
header(s, "02  保留", "【注入】增益区间存在，但几乎全来自“保留”且只有理想 hook 拿得到；现实 hook 不达标", 4)
picture(s, FIG / "locality" / "stress" / "kvcm_gain_region.png", 0.5, 1.3, w=8.1)
text(s, 0.5, 6.72, 8.2, 0.36,
     "【注入】负载倍数 = 提高会话到达率；池 3.5/7/14 TB × HBM 296/148/74 GB；达标 = NPU 省 ≥ 5% 或 P95 降 ≥ 10%",
     size=9.5, color=GRAY)
cards(s, [
    ("CC 8.1–19.9%", "理想 hook 在 CC 所有格子 NPU 省 ≥ 5%：标称池 1× 省 8.1%，7 TB × 2× 最大 19.9%"),
    ("Qwen 7.4–13.7%", "Qwen 只在池 3.5 TB 达标（命中 +3.7 ~ +7.4 pt）；标称 14 TB 池省 < 4%", TEAL),
    ("现实 hook 0–8.5%", "命中比共享池低 0.0–0.9 pt，NPU 多用 0–8.5%；P95 大降来自路由，共享池加同样路由也能拿到", GRAY),
], big_size=22)
footer(s, "kvcm/sim/results/locality.md「KVCM 增益区间【注入】」、kvcm/sim/README.md「增益区间结论」")

# ---------- 5 PD 动态转换 ----------
s = prs.slides.add_slide(BLANK)
header(s, "03  PD 转换", "【注入】PD 动态转换在配比变化时有效：S3 goodput +18.6%；无池丢 KV 快切在 S5 只剩 +1.7%", 5)
picture(s, RES / "pd" / "figures" / "pd_goodput.png", 0.75, 1.18, w=7.5)
table(s, 0.5, 4.86, [2.6, 1.08, 1.08, 1.08, 1.08, 1.08], [
    ["相对静态最优 P:D", "S1", "S2", "S4", "S5", "S3"],
    ["动态 P:D（理想 · 池）", "+0.0%", "+19.0%", "+0.7%", "+11.7%", "+18.6%"],
    ["滞后一窗 · 逻辑切换 + 池", "+0.0%", "+18.0%", "−2.4%", "+6.5%", "+13.5%"],
    ["统计判据 5 min · 池", "+0.0%", "+19.0%", "−2.7%", "+6.0%", "+17.8%"],
    ["无池丢 KV 重算 · 滞后", "+0.0%", "+17.8%", "−4.9%", "−2.5%", "+12.2%"],
], size=10, row_h=0.4, hl_rows=(1,))
cards(s, [
    ("S3 +18.6%", "总量 + 配比同时变化，动态 P:D 理想上界；滞后一窗 + 池 +13.5%（S2m +9.5%、S1 总量变化 0%）"),
    ("S5 +11.7% vs +1.7%", "配比阶跃：有池 vs 无池丢 KV 重算（oracle）；全天过载窗口 0 vs 6，判据控制器 3 vs 59", TEAL),
    ("判据 ≈ 滞后", "S5 统计判据 5 min +6.0%，滞后 1.05× 余量 +6.5%，无余量 −0.2%：余量比判据更关键", GRAY),
], big_size=22)
footer(s, "kvcm/sim/results/pd/summary.md §2、§3a–§3c（S1–S5 为【注入】到达率包络，会话内时间保持 trace 原值）")

# ---------- 6 局部性与冷热 ----------
s = prs.slides.add_slide(BLANK)
header(s, "04  局部性", "局部性与冷热复制在常规和单因素压力下没有收益；被动副本和热点复制是净成本", 6)
table(s, 0.5, 1.35, [3.0, 1.15, 1.15, 1.4, 1.3], [
    ["CC · RoCE · 池 14 TB（E2）", "命中率", "副本占池", "读池 P95", "prefill NPU"],
    ["扁平共享池（哈希条带、单副本）", "95.52%", "0.0%", "2.172 s", "137.9"],
    ["+ 写本机放置", "95.52%", "0.0%", "2.172 s", "137.9"],
    ["+ 写本机 + 被动副本（Mooncake）", "95.18%", "28.1%", "2.577 s", "147.6"],
    ["哈希 + 热点复制（TokenLake）", "95.52%", "0.0%", "2.172 s", "137.9"],
], size=10.5, row_h=0.46, hl_rows=(3,))
table(s, 0.5, 4.0, [3.0, 1.66, 1.66, 1.68], [
    ["池读规模（E1，标称 14 TB）", "Codex", "Claude Code", "Qwen coder"],
    ["读池请求占比", "1.1%", "3.9%", "10.5%"],
    ["未掩盖池读均值（RoCE）", "≤ 0.1 ms", "≤ 0.6 ms", "0.0 ms"],
    ["三种成本模型读池 P95", "0.60 s", "2.17 s", "0.39 s"],
], size=10.5, row_h=0.46)
text(s, 0.5, 6.0, 8.0, 0.9,
     ["热点复制按 r = ceil(λ·τ/ρ) 触发：池读毫秒级，λ·τ ≪ ρ=0.5，副本数始终为 1。",
      "写本机只把 CC 本机字节 7% → 12%，TTFT 变化 0.0%。"],
     size=10.5, color=GRAY)
cards(s, [
    ("< 2%", "【注入】单因素压力（带宽 1/4、1/10，背景 50/70%，KV ×8）下扁平、写本机、副本、热点复制、KVCM 的 TTFT P95 变化"),
    ("+18.6%", "被动副本占池 28.1%，命中 95.52% → 95.18%，读池 P95 2.172 → 2.577 s，NPU +9.7", AMBER),
    ("0 GB/s", "热点复制在单因素下从不触发；最坏组合触发后 P95 是扁平池的 2–3 倍", GRAY),
], big_size=24)
footer(s, "kvcm/sim/results/locality.md E1、E2、「压力敏感性」；kvcm/sim/README.md「池局部性扩展 · 结论」")

# ---------- 7 压力与最坏组合 ----------
s = prs.slides.add_slide(BLANK)
header(s, "04  局部性", "【注入】只有网卡打满（最坏组合）时局部性才起作用：仅 KVCM + 局部性稳定，靠的是路由跟着数据走", 7)
table(s, 0.5, 1.3, [3.9, 2.05, 2.05], [
    ["最坏组合 · TTFT P95", "Claude Code", "Qwen coder"],
    ["扁平共享池（哈希条带、单副本）", "7.15e+04 s", "7.14e+03 s"],
    ["+ 写本机放置", "6.91e+04 s", "6.62e+03 s"],
    ["+ 写本机 + 被动副本（Mooncake）", "4.21e+04 s", "5.23e+03 s"],
    ["哈希 + 热点复制（TokenLake）", "1.44e+05 s", "1.97e+04 s"],
    ["KVCM（现实 hook，哈希放置）", "7.12e+04 s", "7.03e+03 s"],
    ["KVCM + 局部性", "1.56 s", "0.57 s"],
    ["消融：TTFT 路由不计数据位置", "未跑完", "2.31e+03 s"],
], size=10.5, row_h=0.38, hl_rows=(6,))
table(s, 0.5, 4.5, [3.9, 2.05, 2.05], [
    ["CC 单因素 · TTFT P95", "KVCM + 局部性", "消融（不计位置）"],
    ["跨机带宽 1/4", "1.15 s", "15.7 s"],
    ["跨机带宽 1/10", "1.15 s", "508 s"],
    ["背景流量 70%", "1.15 s", "322 s"],
    ["KV 传输 ×8", "1.15 s", "319 s"],
], size=10.5, row_h=0.38)
text(s, 0.5, 6.5, 8.0, 0.55,
     "最坏组合 = HBM 1/4 + KV ×8 + 跨机带宽 1/10 + 背景 70%；消融里放置、副本、命中率与 KVCM + 局部性相同，只是路由不跟数据走",
     size=10, color=GRAY)
cards(s, [
    ("158–758%", "最坏组合下最忙出口负载（需求/容量）；读池请求升到 28–32%，其余变体全部失稳"),
    ("1.56 s / 0.57 s", "KVCM + 局部性的 P95（CC / Qwen），未掩盖读 37 ms / 0.7 ms：读走本机 H2D，不占网卡", GREEN),
    ("81.2% 读池", "消融（带宽 1/4）让 CC 读池请求从 5.7% 升到 81.2%，P95 1.15 → 15.7 s", AMBER),
], big_size=23)
footer(s, "kvcm/sim/results/locality.md「压力敏感性【注入】」；kvcm/sim/README.md「压力敏感性结论」")

# ---------- 8 设计建议 ----------
s = prs.slides.add_slide(BLANK)
header(s, "05  建议", "设计建议：池做简单（哈希条带、单副本），复杂度投在保留策略和看队列 + 命中的路由", 8)
table(s, 0.5, 1.35, [1.6, 3.0, 3.4], [
    ["方向", "建议", "依据"],
    ["池放置", "哈希条带、单副本；不做写本机 / 被动副本 / 热点复制",
     "池读仅占 1–10% 请求，未掩盖读 ≤ 0.6 ms；被动副本读池 P95 +18%"],
    ["保留策略", "会话感知保留 / 驱逐，投入“预测未来复用”的 hook",
     "CC 3.5 TB：理想 hook 210.4 NPU vs 现实 hook 250.9"],
    ["路由", "看队列 + 命中，保住前缀亲和；数据位置作为负载信号",
     "预测 TTFT 路由 CC P95 2.005 → 1.096 s；过载区只看 TTFT 会掉命中 1.4–2.0 pt"],
    ["PD 转换", "同布局逻辑切换 + 池；滞后控制器保留余量",
     "【注入】S3 +13.5%（滞后 + 池）；S5 无池丢 KV 重算 −2.5%"],
    ["监控", "把网卡出口利用率作为局部性的先行指标",
     "单因素下池读额外占出口 ≤ 11%；出口打满后局部性才有收益"],
], size=10.5, row_h=0.86, left=True)
cards(s, [
    ("哈希 + 单副本", "常规与单因素压力下，所有放置 / 副本变体的 TTFT P95 变化 < 2%"),
    ("−41.5 NPU", "CC 3.5 TB 池理想 hook 相对共享池；现实 hook 只有 −0.9，差距就是预测复用的价值", TEAL),
    ("−45% P95", "E4：共享池只换预测 TTFT 路由，CC 全部请求 P95 2.005 → 1.096 s", AMBER),
], big_size=23)
footer(s, "kvcm/sim/README.md「对 KVCM 设计的含义」；summary.md；locality.md E4；pd/summary.md")

# ---------- 9 口径与局限 ----------
s = prs.slides.add_slide(BLANK)
header(s, "06  局限", "口径与局限：注入场景、6 h 窗口和 1/32 切片都是近似，结论以相对比较为主", 9)
table(s, 0.5, 1.35, [2.0, 6.0], [
    ["项", "说明"],
    ["【注入】项", "PD S1–S5 到达率包络；增益网格的负载倍数 / 池 / HBM；压力点（带宽、背景流量、KV ×8、最坏组合）"],
    ["6 h 窗口", "压力与增益网格窗口 6 h、前 2 h 预热（主线 10 h / 3 h）；CC 标称点命中 95.72%（主线 95.52%）"],
    ["切片 1/32", "计算时长 ×32、带宽 ÷32；会话内间隔不放大，突发更密，排队偏保守"],
    ["理想 hook", "按真实未来复用保留 / 驱逐，是上界；现实 hook 只靠停顿期保活 + 零引用优先"],
    ["PD 流体模型", "5 min 窗口平均、不出 TTFT 分位；decode 25 ms/步为 roofline 假设，A3 无实测"],
    ["其他", "单次运行 seed=1，无真机校准；Codex 时间线合成；本机读走 H2D 不占网卡是假设"],
], size=10.5, row_h=0.72, left=True)
cards(s, [
    ("seed = 1", "每个点单次运行、无真机校准；A3 TTFT = 2 × 950 拟合值（按 8/49 激活参数比缩放）"),
    ("1 个最坏组合", "单因素与最坏组合之间未二分出拐点；CC 最坏组合的消融格未跑完", AMBER),
    ("Codex 合成", "trace 无时间戳，停顿按 lognormal（P50 5.2 s）合成，hook 结果只看结构", GRAY),
], big_size=23)
footer(s, "kvcm/sim/params.md §7、kvcm/sim/README.md「局限」、pd/summary.md §6")

prs.save(OUT)
print(OUT)
