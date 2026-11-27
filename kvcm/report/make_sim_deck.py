"""生成 KVCM-仿真结论-20261126.pptx（python-pptx）。

数字全部摘自 kvcm/sim/results/{summary.md, pd/summary.md, locality.md} 与 kvcm/sim/{params.md, README.md}；
版式沿用 KVCM-汇报-含PD混部-20261124.pptx 的配色与字体。
用法：python3 kvcm/report/make_sim_deck.py（10 页）；加 --short 生成精简版 KVCM-仿真结论-精简4页-20261126.pptx；
加 --n11 / --n12 生成 11 / 12 页版 KVCM-仿真结论-{11,12}页-20261127.pptx（6 节点图取 results/figures/n6/）
"""
import sys
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
TOTAL = 10
DATE = "2026-11-26"

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


def header(s, section, title, page, pill_w=1.48, title_size=19):
    rect(s, 0, 0, 13.333, 0.06, NAVY)
    rect(s, 0, 0, 2.3, 0.06, RED)
    pill = rect(s, 0.5, 0.26, pill_w, 0.34, RED, MSO_SHAPE.ROUNDED_RECTANGLE)
    pill.adjustments[0] = 0.22
    text(s, 0.5, 0.26, pill_w, 0.34, section, size=10.5, color=WHITE, bold=True,
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    tx = 0.5 + pill_w + 0.14
    text(s, tx, 0.16, 11.72 - tx, 0.86, title, size=title_size, color=NAVY, bold=True,
         anchor=MSO_ANCHOR.MIDDLE, spacing=1.0)
    text(s, 11.8, 0.26, 1.03, 0.34,
         [[(f"{page:02d}", {"color": RED, "size": 13, "bold": True}),
           (f" / {TOTAL:02d}", {"color": GRAY, "size": 10.5})]],
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
    rect(s, 0.5, 1.08, 12.33, 0.012, LINE)


def footer(s, source):
    rect(s, 0.5, 7.12, 12.33, 0.01, LINE)
    text(s, 0.5, 7.15, 9.4, 0.26, f"数据：{source}", size=8.5, color=GRAY)
    text(s, 9.9, 7.15, 2.93, 0.26, f"KVCM 仿真结论 · {DATE}", size=8.5, color=GRAY,
         align=PP_ALIGN.RIGHT)


def card(s, x, y, w, h, big, caption, accent=RED, big_size=24, cap_size=11):
    rect(s, x, y, w, h, PALE, MSO_SHAPE.ROUNDED_RECTANGLE, line=LINE)
    rect(s, x, y + 0.12, 0.07, h - 0.24, accent)
    text(s, x + 0.2, y + 0.1, w - 0.3, 0.56, big, size=big_size, color=accent, bold=True,
         anchor=MSO_ANCHOR.MIDDLE, spacing=1.0)
    text(s, x + 0.2, y + 0.68, w - 0.3, h - 0.76, caption, size=cap_size, color=INK, spacing=1.15)


def cards(s, items, x=8.75, y=1.3, w=4.08, h=1.82, gap=0.11, big_size=24):
    for i, it in enumerate(items):
        big, caption, *rest = it
        card(s, x, y + i * (h + gap), w, h, big, caption, rest[0] if rest else RED, big_size)


def table(s, x, y, col_w, rows, size=10.5, row_h=0.3, hl_rows=(), hl_color=RGBColor(0xFD, 0xF2, 0xF4),
          left=False, row_hs=None):
    nr, nc = len(rows), len(rows[0])
    gf = s.shapes.add_table(nr, nc, Inches(x), Inches(y), Inches(sum(col_w)), Inches(row_h * nr))
    tbl = gf.table
    tbl.first_row = True
    for j, cw in enumerate(col_w):
        tbl.columns[j].width = Inches(cw)
    for i in range(nr):
        tbl.rows[i].height = Inches(row_hs[i] if row_hs else row_h)
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



def build_short():
    """精简版（6 页，文件名沿用“精简4页”）：措辞与数字沿用 10 页版。"""
    global TOTAL
    TOTAL = 6
    out = HERE / "KVCM-仿真结论-精简4页-20261126.pptx"

    # 1 总结论 + 口径
    s = prs.slides.add_slide(BLANK)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = DEEP
    rect(s, 0, 0, 13.333, 0.08, RED)
    p = rect(s, 0.6, 0.45, 2.9, 0.38, RED, MSO_SHAPE.ROUNDED_RECTANGLE)
    p.adjustments[0] = 0.22
    text(s, 0.6, 0.45, 2.9, 0.38, "KVCM trace 回放仿真 · 结论", size=11, color=WHITE, bold=True,
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 11.8, 0.45, 1.03, 0.38,
         [[("01", {"color": GOLD, "size": 13, "bold": True}), (f" / {TOTAL:02d}", {"color": SOFT, "size": 10.5})]],
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 0.6, 0.95, 12, 0.8, "KVCM 仿真结论", size=36, color=WHITE, bold=True)
    text(s, 0.6, 1.8, 12.1, 1.5,
         [[("保留", {"color": GOLD, "bold": True}), ("（理想 hook）和 ", {}),
           ("PD 动态转换", {"color": GOLD, "bold": True}), ("带来常态收益；", {})],
          [("局部性", {"color": GOLD, "bold": True}), ("在网卡紧张时起作用，网络空闲时无影响；", {})],
          [("冷热复制", {"color": GOLD, "bold": True}),
           ("在现有三条 trace 下无收益；【注入】只在数据被集中到少数节点、热节点出口 ≳ 90% 时有净收益，哈希条带本身没有热点。", {})]],
         size=18, color=WHITE, spacing=1.2)
    rect(s, 0.6, 3.38, 1.65, 0.05, RED)
    table(s, 0.6, 3.52, [1.5, 10.63], [
        ["方向", "结论（关键数字）"],
        ["保留", "CC 3.5 TB 池：理想 hook NPU 251.9 → 210.4，现实 hook 仅 250.9"],
        ["PD 动态转换", "【注入】S3 goodput +18.6%（滞后控制器 + 池 +13.5%）"],
        ["局部性", "网卡紧张时起作用：【注入】最坏组合出口负载 158–758%，仅 KVCM + 局部性稳定在 "
                   "1.56 s / 0.57 s，其余变体失稳；消融里路由不计位置时 CC 带宽 1/4 即 15.7 s、1/10 达 508 s。"
                   "网络空闲时无影响：单因素 P95 变化 < 2%"],
        ["冷热复制", "现有三条 trace 下无收益（触发时 P95 反而变差 2–3 倍，副本占池 28–65%）；"
                     "【注入】数据被集中到少数节点、热节点出口 ≳ 90% 时有净收益（写本机 → 热点复制 P95 −67% / −86% / −98%），"
                     "扁平哈希池 P95 始终约 0.16 s"],
    ], size=10.5, row_h=0.4, left=True, row_hs=[0.36, 0.4, 0.4, 0.66, 0.66])
    text(s, 0.6, 6.25, 12.1, 0.8,
         [[("仿真口径：", {"color": GOLD, "bold": True}),
           ("DeepSeek V4.1-Flash，BF16 KV 3,200 B/token；Ascend A3 14 节点 × 16 die；0.85 → 每实例 296 GB；"
            "HBM（引擎 LRU）+ DRAM 池（KVCM，标称 14 TB）；三条 trace（Claude Code、Codex、Qwen coder）；切片 1/32、seed=1", {})]],
         size=11, color=SOFT, spacing=1.15)

    # 2 保留
    s = prs.slides.add_slide(BLANK)
    header(s, "02  保留", "池在 1–2× HBM 区间，理想 hook 比共享池多命中 0.7–3.45 pt；【注入】增益几乎全来自“保留”且只有理想 hook 拿得到", 2)
    table(s, 0.5, 1.3, [2.0, 0.9, 1.0, 0.85, 0.85], [
        ["CC · DRAM 池 3.5 TB", "命中率", "prefill NPU", "TTFT P95", "相对共享池"],
        ["shared-pool LRU", "91.56%", "251.9", "2.38 s", "—"],
        ["KVCM（现实 hook）", "91.60%", "250.9", "2.31 s", "−0.9"],
        ["KVCM（理想 hook）", "93.03%", "210.4", "1.64 s", "−41.5"],
    ], size=9.5, row_h=0.38, hl_rows=(3,))
    for i, (big, cap, acc) in enumerate([
        ("251.9 → 210.4 NPU", "CC 3.5 TB 理想 hook；TTFT P95 2.38 → 1.64 s；现实 hook 只省 0.9", RED),
        ("CC 8.1–19.9% · Qwen 7.4–13.7%", "【注入】理想 hook 的 NPU 节省；Qwen 只在池 3.5 TB 达标，标称 14 TB 池省 < 4%", TEAL),
        ("现实 hook 0–8.5%", "命中比共享池低 0.0–0.9 pt，NPU 多用 0–8.5%；Codex 已贴着 h*，无增益", GRAY),
    ]):
        card(s, 0.5, 3.0 + i * 1.36, 5.6, 1.26, big, cap, acc, 19)
    picture(s, FIG / "locality" / "stress" / "kvcm_gain_region.png", 6.3, 1.3, w=6.53)
    text(s, 6.3, 5.55, 6.53, 0.6,
         "【注入】负载倍数 = 提高会话到达率；池 3.5/7/14 TB × HBM 296/148/74 GB；达标 = NPU 省 ≥ 5% 或 P95 降 ≥ 10%",
         size=9.5, color=GRAY)
    footer(s, "kvcm/sim/results/summary.md（DRAM 池 0.25 × 标称）；locality.md「KVCM 增益区间【注入】」")

    # 3 PD 动态转换
    s = prs.slides.add_slide(BLANK)
    header(s, "03  PD 转换", "【注入】PD 动态转换在配比变化时有效：S3 goodput +18.6%；无池丢 KV 快切在 S5 只剩 +1.7%", 3)
    picture(s, RES / "pd" / "figures" / "pd_goodput.png", 0.75, 1.18, w=7.5)
    table(s, 0.5, 4.86, [2.6, 1.08, 1.08, 1.08, 1.08, 1.08], [
        ["相对静态最优 P:D", "S1", "S2", "S4", "S5", "S3"],
        ["动态 P:D（理想 · 池）", "+0.0%", "+19.0%", "+0.7%", "+11.7%", "+18.6%"],
        ["滞后一窗 · 逻辑切换 + 池", "+0.0%", "+18.0%", "−2.4%", "+6.5%", "+13.5%"],
        ["统计判据 5 min · 池", "+0.0%", "+19.0%", "−2.7%", "+6.0%", "+17.8%"],
        ["无池丢 KV 重算 · 滞后", "+0.0%", "+17.8%", "−4.9%", "−2.5%", "+12.2%"],
    ], size=10, row_h=0.4, hl_rows=(1,))
    cards(s, [
        ("S3 +18.6%", "总量 + 配比同时变化，动态 P:D 理想上界；滞后一窗 + 池 +13.5%（S1 总量变化 0%）"),
        ("S5 +11.7% vs +1.7%", "配比阶跃：有池 vs 无池丢 KV 重算（oracle）；全天过载窗口 0 vs 6", TEAL),
        ("判据 ≈ 滞后", "S5 统计判据 5 min +6.0%，滞后 1.05× 余量 +6.5%，无余量 −0.2%", GRAY),
    ], big_size=22)
    footer(s, "kvcm/sim/results/pd/summary.md §2、§3a–§3c（S1–S5 为【注入】到达率包络，会话内时间保持 trace 原值）")

    # 4 局部性
    s = prs.slides.add_slide(BLANK)
    header(s, "04  局部性", "【注入】局部性在网卡紧张时起作用，网络空闲时无影响：最坏组合下仅 KVCM + 局部性稳定", 4)
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
        ("< 2%", "网络空闲：【注入】单因素压力（带宽 1/4、1/10，背景 50/70%，KV ×8）下各变体 TTFT P95 变化", GRAY),
        ("1.56 s / 0.57 s", "网卡紧张：【注入】最坏组合出口负载 158–758%，仅 KVCM + 局部性稳定（CC / Qwen），其余变体失稳", GREEN),
        ("15.7 s / 508 s", "【注入】消融：路由不计数据位置时 CC 带宽 1/4、1/10 的 P95；同档 KVCM + 局部性 1.15 s", AMBER),
    ], big_size=23)
    footer(s, "kvcm/sim/results/locality.md「压力敏感性【注入】」；kvcm/sim/README.md「压力敏感性结论」")

    # 5 冷热复制
    s = prs.slides.add_slide(BLANK)
    header(s, "05  冷热复制", "冷热复制在现有三条 trace 下无收益；【注入】只在数据被集中到少数节点、热节点出口 ≳ 90% 时有净收益，哈希条带本身没有热点", 5)
    picture(s, FIG / "locality" / "hotspot" / "hotspot_scan.png", 1.67, 1.15, w=10.0)
    w5 = (12.33 - 4 * 0.1) / 5
    for i, (big, cap, acc) in enumerate([
        ("2.172 → 2.577 s", "现有三条 trace：被动副本占池 28.1%，读池 P95 变差；热点复制单因素下不触发", AMBER),
        ("−67 / −86 / −98%", "【注入】热点集中：写本机 → 热点复制，1 / 2 / 3 个热节点的 P95", RED),
        ("拐点 66–92%", "【注入】热节点出口负载在此区间转为净收益", TEAL),
        ("0.17 s vs 57 s", "【注入】单热节点：KVCM 读者就地副本 + 位置路由 对 只路由", GREEN),
        ("≈ 0.16 s", "【注入】扁平哈希池把热文档条带到全部节点，P95 始终约 0.16 s", GRAY),
    ]):
        card(s, 0.5 + i * (w5 + 0.1), 4.62, w5, 2.36, big, cap, acc, 17)
    footer(s, "kvcm/sim/results/locality.md E2、「热点复制增益场景【注入】」（热集合 2× 集群 HBM）；kvcm/sim/README.md")

    # 6 设计建议 + 口径
    s = prs.slides.add_slide(BLANK)
    header(s, "06  建议", "设计建议：池做简单（哈希条带、单副本），复杂度投在保留策略和路由（数据位置 + 队列 + 命中）", 6)
    table(s, 0.5, 1.3, [1.5, 4.9, 5.93], [
        ["方向", "建议", "依据"],
        ["池放置", "哈希条带、单副本即可避免热点；只有放置被迫集中（如写本机）时才需要热页定向副本 + 位置路由",
         "现有 trace 下被动副本读池 P95 +18%；【注入】扁平哈希池 P95 始终约 0.16 s，单热节点时 KVCM 0.17 s 对只路由 57 s"],
        ["保留策略", "会话感知保留 / 驱逐，投入“预测未来复用”的 hook",
         "CC 3.5 TB：理想 hook 210.4 NPU vs 现实 hook 250.9"],
        ["路由", "路由计入数据位置（网卡紧张时防止排队失稳），并看队列和命中",
         "【注入】不计位置时 CC 带宽 1/4 即 15.7 s；过载区只看 TTFT 会掉命中 1.4–2.0 pt；预测 TTFT 路由 CC P95 2.005 → 1.096 s"],
        ["PD 转换", "同布局逻辑切换 + 池；滞后控制器保留余量",
         "【注入】S3 +13.5%（滞后 + 池）；S5 无池丢 KV 重算 −2.5%"],
        ["监控", "把网卡出口利用率作为局部性的先行指标",
         "单因素下池读额外占出口 ≤ 11%；出口打满后局部性才有收益"],
    ], size=10.5, row_h=0.62, left=True, row_hs=[0.4, 0.66, 0.5, 0.66, 0.5, 0.5])
    note(s, 0.5, 4.75, 12.33, 1.1,
         ["口径与局限：【注入】项包括 PD S1–S5 到达率包络、增益网格、压力点与热点场景；压力 / 增益网格窗口 6 h（主线 10 h），切片 1/32；",
          "理想 hook 是上界；PD 为 5 min 流体模型；单次运行 seed=1，无真机校准；Codex 时间线为合成。结论以相对比较为主。"],
         size=11)
    footer(s, "kvcm/sim/README.md「对 KVCM 设计的含义」「局限」；params.md；summary.md；locality.md；pd/summary.md")

    prs.save(out)
    print(out)
    sys.exit(0)


def label(s, x, y, w, t, color=NAVY):
    text(s, x, y, w, 0.3, t, size=12, color=color, bold=True)


def compose(path, tiles, top=None):
    """把官方图裁成若干面板横排（可加一条图例在上方），只裁剪不改数据。"""
    from PIL import Image
    ims = [Image.open(f).crop(box) for f, box in tiles]
    leg = Image.open(top[0]).crop(top[1]) if top else None
    w = sum(i.width for i in ims)
    h = max(i.height for i in ims) + (leg.height if leg else 0)
    out = Image.new("RGB", (w, h), "white")
    y0 = 0
    if leg:
        out.paste(leg, ((w - leg.width) // 2, 0))
        y0 = leg.height
    x = 0
    for i in ims:
        out.paste(i, (x, y0))
        x += i.width
    out.save(path)
    return path


def build_n11(n12=False):
    """11 页版：6 节点 hook 命中 + 14 节点 PD 转换 / 局部性 / 冷热复制 / hook 命中，每主题“机制与口径”+“图与结论”两页。
    n12=True：12 页版，6 节点改为三页（机制与口径、命中率曲线与理论上限、距上限缺口与闭合率）。"""
    import tempfile
    global TOTAL, DATE
    TOTAL, DATE = (12 if n12 else 11), "2026-11-27"
    out = HERE / f"KVCM-仿真结论-{TOTAL}页-20261127.pptx"
    pages = iter(range(2, TOTAL + 1))
    d = int(n12)
    tmp = Path(tempfile.mkdtemp())
    n6 = FIG / "n6"
    hit3 = compose(tmp / "n6_hit3.png", [(n6 / f"n6_{t}.png", (0, 48, 742, 668))
                                         for t in ("cc256k", "codex", "qwen_coder")])
    stf = FIG / "locality" / "stress" / "stress_single_factor.png"
    stress2 = compose(tmp / "stress_ttft.png", [(stf, (0, 175, 846, 750)), (stf, (0, 760, 846, 1335))],
                      top=(stf, (440, 55, 2110, 125)))
    gap = compose(tmp / "n6_gap.png", [(n6 / "n6_gap_to_ceiling.png", (0, 100, 2268, 1204))])
    PW = 2.05

    # 1 题目
    s = prs.slides.add_slide(BLANK)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = DEEP
    rect(s, 0, 0, 13.333, 0.08, RED)
    p = rect(s, 0.78, 0.9, 2.9, 0.38, RED, MSO_SHAPE.ROUNDED_RECTANGLE)
    p.adjustments[0] = 0.22
    text(s, 0.78, 0.9, 2.9, 0.38, "KVCM trace 回放仿真", size=11, color=WHITE, bold=True,
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 11.8, 0.9, 1.03, 0.38,
         [[("01", {"color": GOLD, "size": 13, "bold": True}), (f" / {TOTAL:02d}", {"color": SOFT, "size": 10.5})]],
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 0.78, 1.45, 11.5, 0.95, "KVCM 仿真结论", size=40, color=WHITE, bold=True)
    text(s, 0.78, 2.45, 11.8, 0.5, "6 节点 hook 命中 · 14 节点 PD 转换、局部性、冷热复制与 hook 命中",
         size=20, color=GOLD, bold=True)
    rect(s, 0.78, 3.12, 1.65, 0.05, RED)
    g = {"color": GOLD, "bold": True}
    text(s, 0.78, 3.3, 11.8, 1.3, [
        [("KVCM", g), ("（KV Centric Manager，以 KV 缓存为中心的管理器）：管理全集群共享 DRAM（主机内存）池里的 KV（注意力键值缓存）", {})],
        [("PD", g), ("：预填充（Prefill）/ 解码（Decode）；", {}), ("HBM", g), ("：高带宽内存，归推理引擎；", {}),
         ("NPU", g), ("：神经网络处理器；", {}), ("LRU", g), ("：最近最少使用", {})],
        [("hook", g), ("：引擎告诉 KVCM 会话何时回来的接口；", {}), ("TTFT", g), ("：首 token 时延；", {}),
         ("P95", g), ("：95 分位；", {}), ("trace", g), ("：公开的 agentic 请求轨迹", {})],
    ], size=13, color=SOFT, spacing=1.35)
    w5 = (11.77 - 4 * 0.14) / 5
    for i, (pg, t, acc) in enumerate([
        ("02 – 04" if n12 else "02 – 03", "6 节点\nhook 命中", RED),
        (f"{4 + d:02d} – {5 + d:02d}", "14 节点\nPD 转换", TEAL),
        (f"{6 + d:02d} – {7 + d:02d}", "14 节点\n局部性", GREEN),
        (f"{8 + d:02d} – {9 + d:02d}", "14 节点\n冷热复制", AMBER),
        (f"{10 + d:02d} – {11 + d:02d}", "14 节点\nhook 命中", RED),
    ]):
        x = 0.78 + i * (w5 + 0.14)
        rect(s, x, 4.75, w5, 1.3, CARD_DARK)
        rect(s, x, 4.75, w5, 0.06, acc)
        text(s, x + 0.18, 4.9, w5 - 0.3, 0.32, pg, size=11, color=GOLD, bold=True)
        text(s, x + 0.18, 5.22, w5 - 0.3, 0.75, t.split("\n"), size=14, color=WHITE, bold=True, spacing=1.1)
    text(s, 0.78, 6.6, 11.8, 0.4,
         "DeepSeek V4.1-Flash · 昇腾 A3 · 三条 trace（Claude Code、Codex、Qwen coder）· 纯 CPU 回放，无真机校准 · 2026-11-27",
         size=11, color=SOFT)

    # 2 6 节点 hook 命中 · 机制与口径
    s = prs.slides.add_slide(BLANK)
    header(s, "6 节点 · hook 命中", "机制与口径：HBM 不动、只换 DRAM 池的保留策略；负载按“预填充占用 70%”标定，理论上限只剩首轮必然未命中", next(pages), PW)
    label(s, 0.5, 1.2, 6.0, "机制：三种 DRAM 池策略")
    table(s, 0.5, 1.52, [2.15, 3.95], [
        ["策略", "DRAM 池怎么管"],
        ["共享池 LRU（最近最少使用）", "全集群一个共享池，去重、任意实例可读，满了驱逐最久未用的块（基线）"],
        ["KVCM 现实 hook", "共享池 + 会话停顿期保活 + 零引用优先驱逐；停顿多久只按该会话的历史间隔预测"],
        ["KVCM 理想 hook", "同上，但知道真实的下一轮时间和会话是否结束：策略能做到的上界"],
    ], size=10.5, row_h=0.6, left=True, row_hs=[0.36, 0.62, 0.62, 0.62])
    text(s, 0.5, 3.85, 6.1, 0.8,
         "HBM（高带宽内存）归推理引擎：每实例 296 GB 前缀缓存、纯 LRU，三种策略相同；KVCM 只管 DRAM（主机内存）池。"
         "hook = 引擎告诉 KVCM“这个会话何时回来”的接口。",
         size=10.5, color=GRAY, spacing=1.15)
    label(s, 6.83, 1.2, 6.0, "配置")
    table(s, 6.83, 1.52, [1.15, 4.85], [
        ["项", "取值"],
        ["模型", "DeepSeek V4.1-Flash；KV 按 BF16（16 位浮点）存，3,200 B/token"],
        ["集群", "昇腾 A3，6 节点 × 16 die（芯片裸片）= 96 NPU（神经网络处理器），6 个实例"],
        ["HBM", "每实例 296 GB，全集群 1.78 TB"],
        ["DRAM 池", "0.18 / 0.44 / 0.89 / 1.78 / 3.55 / 6 TB（6 TB = 6 节点 × 1 TB 标称）；9 / 12 TB 超标称，只验证收敛"],
        ["到达率", "Claude Code 5,123 会话/h、Codex 35,336、Qwen coder 928,967（14 节点的 0.47–0.60 倍）"],
        ["回放", "10 h 窗口（前 3 h 预热），切片 1/32，seed（随机种子）1 / 2 / 3 取均值"],
    ], size=10, row_h=0.42, left=True, row_hs=[0.34, 0.42, 0.42, 0.34, 0.58, 0.58, 0.42])
    label(s, 0.5, 4.72, 6.0, "口径")
    w3 = (12.33 - 2 * 0.12) / 3
    for i, (big, cap, acc) in enumerate([
        ("70% 只算预填充", "每条 trace 单独标定到达率，使 6 TB 共享池 LRU 的预填充 NPU 占用 = 96 NPU 的 70%；"
                         "解码（decode）的算力不在这 70% 里，也不参与过载判断", RED),
        ("理论上限", "无限容量、全局共享时的前缀命中率，只剩每个块首次出现的必然未命中；与池容量和策略无关。"
                    "Claude Code 96.10%、Codex 96.56%、Qwen coder 70.98%", TEAL),
        ("过载点 TTFT 不含排队", "预填充 NPU 利用率 > 100% 标“过载”。TTFT（首 token 时延）为分析式、不含排队，"
                              "过载点的 P95（95 分位）偏乐观；命中率与 NPU 差不受影响", AMBER),
    ]):
        card(s, 0.5 + i * (w3 + 0.12), 5.02, w3, 1.98, big, cap, acc, 17)
    footer(s, "kvcm/sim/params.md §9；kvcm/sim/README.md「6 节点保留收益主口径」；results/n6/summary.md")

    if n12:
        # 6 节点 · 命中率曲线与理论上限
        s = prs.slides.add_slide(BLANK)
        header(s, "6 节点 · hook 命中", "三条 trace 命中率曲线与理论上限：小池命中几乎全在 HBM，理想 hook 在 0.89–3.55 TB 拉开，大池三者向上限收敛", next(pages), PW, 18)
        picture(s, hit3, 0.5, 1.12, w=12.33)
        text(s, 0.5, 4.56, 12.33, 0.3,
             "左起 Claude Code / Codex / Qwen coder（官方主图命中率面板；70% 标定，seed 1/2/3 均值，误差棒 = seed 最小–最大）；"
             "点划线 = 理论上限，灰虚线 = 集群 HBM 1.78 TB，灰底 = 9 / 12 TB 超标称",
             size=9.5, color=GRAY, align=PP_ALIGN.CENTER)
        table(s, 0.5, 4.92, [1.4, 1.05, 1.1, 2.2, 2.2, 2.2, 2.18], [
            ["命中率", "理论上限", "其中 HBM", "0.89 TB：LRU / 现实 / 理想", "1.78 TB：LRU / 现实 / 理想",
             "3.55 TB：LRU / 现实 / 理想", "6 TB：LRU / 现实 / 理想"],
            ["Claude Code", "96.10%", "90.14%", "90.19 / 90.23 / 90.46", "90.86 / 90.85 / 92.71",
             "94.59 / 94.36 / 95.36", "95.28 / 94.93 / 95.83"],
            ["Codex", "96.56%", "94.49%", "95.08 / 95.08 / 95.09", "96.02 / 96.01 / 96.51",
             "96.56 / 96.56 / 96.56", "96.56 / 96.56 / 96.56"],
            ["Qwen coder", "70.98%", "59.99%", "59.99 / 60.25 / 65.09", "60.59 / 60.84 / 65.59",
             "65.86 / 65.75 / 67.95", "68.83 / 67.47 / 69.68"],
        ], size=10, row_h=0.36, row_hs=[0.36, 0.36, 0.36, 0.36])
        text(s, 0.5, 6.42, 12.33, 0.62, [
            "单位 %；“其中 HBM”= HBM 层命中，三策略相同、不随池变。池 ≤ 0.44 TB 时三者相同（Claude Code 90.2%、Qwen coder 60.0%）；"
            "12 TB（超标称）共享池 LRU 已到 95.79% / 70.68%。seed 极差一半 ≤ 0.34 / 0.18 / 0.09 个百分点，小于中池的理想 hook 增益。"],
            size=10, color=GRAY, spacing=1.15)
        footer(s, "results/n6/summary.md 主口径表、「理论命中上限与缺口闭合率」；图：media/sim/n6/n6_cc256k / n6_codex / n6_qwen_coder.png（命中率面板）")

        # 6 节点 · 距上限缺口与闭合率
        s = prs.slides.add_slide(BLANK)
        header(s, "6 节点 · hook 命中", "距理论上限的缺口：理想 hook 在拉开最多的池补回 35–91%；现实 hook 中小池只补 0–5%，大池反而扩大缺口", next(pages), PW, 18)
        picture(s, gap, 0.5, 1.12, w=9.0)
        text(s, 0.5, 5.5, 9.0, 0.26,
             "上：距上限缺口（个百分点，越低越好）；下：闭合率 =（hook − LRU）/（上限 − LRU），误差棒 = 逐 seed 最小–最大",
             size=9, color=GRAY, align=PP_ALIGN.CENTER)
        table(s, 9.68, 1.12, [1.33, 0.95, 0.87], [
            ["trace · 池", "缺口 LRU → 理想", "闭合率 理想 / 现实"],
            ["Claude Code 1.78 TB", "5.24 → 3.38", "35% / 0%"],
            ["Claude Code 3.55 TB", "1.51 → 0.73", "52% / −15%"],
            ["Claude Code 6 TB", "0.82 → 0.27", "68% / −43%"],
            ["Codex 1.78 TB", "0.54 → 0.05", "91% / −2%"],
            ["Codex ≥ 3.55 TB", "0（触顶）", "不给比值"],
            ["Qwen coder 0.89 TB", "10.99 → 5.89", "46% / 2%"],
            ["Qwen coder 1.78 TB", "10.39 → 5.39", "48% / 2%"],
            ["Qwen coder 6 TB", "2.15 → 1.30", "40% / −63%"],
        ], size=9, row_h=0.42, row_hs=[0.5] + [0.42] * 8)
        text(s, 9.68, 5.0, 3.15, 0.45, "缺口单位：个百分点；LRU 缺口 < 0.2 视为触顶，0.2–0.5 只作参考",
             size=8.5, color=GRAY)
        table(s, 0.5, 5.84, [1.85, 2.15, 1.75, 3.25], [
            ["理想 hook 对 LRU", "预填充 NPU", "利用率", "TTFT P95（不含排队）"],
            ["Claude Code 1.78 TB", "121.5 → 98.0（−19.3%）", "127% → 102%（过载）", "2.84 → 1.71 s（−40%，过载点偏乐观）"],
            ["Claude Code 6 TB", "64.6 → 57.4（−11.1%）", "67% → 60%", "1.12 → 1.04 s（−7.6%）"],
            ["Codex 1.78 TB", "75.4 → 67.9（−9.9%）", "79% → 71%", "0.69 → 0.68 s（−1.5%）"],
            ["Qwen coder 0.89 TB", "79.7 → 72.4（−9.3%）", "83% → 75%", "0.65 → 0.61 s（−6.3%）"],
        ], size=9.5, row_h=0.24)
        card(s, 9.68, 5.5, 3.15, 1.55, "现实 hook：大池为负",
             "中小池闭合率 0–5%；6 TB 时 Claude Code 比 LRU 低 0.35 个百分点（NPU +6.9%），Qwen coder 低 1.35（NPU +2.9%）",
             GRAY, 14, 9.5)
        footer(s, "results/n6/summary.md「距理论上限的缺口与闭合率」、主口径表；results/n6/closure.json；图：media/sim/n6/n6_gap_to_ceiling.png")
    else:
        # 3 6 节点 hook 命中 · 图与结论
        s = prs.slides.add_slide(BLANK)
        header(s, "6 节点 · hook 命中", "理想 hook 收益集中在池 0.89–3.55 TB（约 0.5–2 倍集群 HBM），补回距理论上限缺口的 35–91%；现实 hook 基本拿不到", next(pages), PW, 18)
        picture(s, hit3, 0.95, 1.14, w=11.43)
        text(s, 0.5, 4.32, 12.33, 0.3,
             "左起 Claude Code / Codex / Qwen coder 命中率（70% 标定，seed 均值，误差棒 = seed 最小–最大）；点划线 = 理论上限，灰底 = 超标称",
             size=9.5, color=GRAY, align=PP_ALIGN.CENTER)
        table(s, 0.5, 4.68, [1.45, 1.05, 1.45, 2.75, 2.65, 2.98], [
            ["trace", "理论上限", "拉开最多的池", "命中率：共享池 LRU → 理想 hook", "预填充 NPU（全规模等效）", "缺口闭合率（拉开最多处）：理想 / 现实"],
            ["Claude Code", "96.10%", "1.78 TB", "90.86% → 92.71%（+1.86 个百分点）", "121.5 → 98.0（−19.3%，均过载）", "35% / 0%；6 TB 为 68% / −43%"],
            ["Codex", "96.56%", "1.78 TB", "96.02% → 96.51%（+0.49）", "75.4 → 67.9（−9.9%）", "91% / −2%；≥ 3.55 TB 已触顶"],
            ["Qwen coder", "70.98%", "0.89 TB", "59.99% → 65.09%（+5.09）", "79.7 → 72.4（−9.3%）", "46% / 2%；6 TB 为 40% / −63%"],
        ], size=10, row_h=0.38, row_hs=[0.36, 0.38, 0.38, 0.38])
        note(s, 0.5, 6.3, 12.33, 0.74, [
            "闭合率 =（hook − LRU）/（理论上限 − LRU）。过载：Claude Code 池 ≤ 1.78 TB 时共享池 LRU 需 127–136% NPU，理想 hook 在 1.78 TB 降到 102%；"
            "同点 TTFT P95 2.84 → 1.71 s 不含排队、偏乐观。现实 hook（Claude Code、Qwen coder）在 3.55 TB 起比共享池 LRU 低 0.1–1.4 个百分点。",
        ], size=10)
        footer(s, "results/n6/summary.md「理论命中上限与缺口闭合率」与主口径表；图：media/sim/n6/n6_*.png（命中率面板）")

    # 4 PD 转换 · 机制与口径
    s = prs.slides.add_slide(BLANK)
    header(s, "14 节点 · PD 转换", "机制与口径：节点在预填充 / 解码 / 混部角色间切换；KVCM 池让被切节点的 KV 可以直接丢弃、由接收方重读", next(pages), PW)
    label(s, 0.5, 1.2, 6.5, "机制：切换方式与控制器")
    table(s, 0.5, 1.52, [3.1, 1.25, 2.35], [
        ["切换方式", "D→P 耗时", "被切节点的 KV"],
        ["同布局逻辑切换 + KVCM 池", "2.2 s", "丢弃，接收方从池重读"],
        ["逻辑切换 + 无池快切", "2 s", "全丢，按未命中重算"],
        ["逻辑切换 + 无池排空", "1,439 s", "等在途解码跑完"],
        ["休眠 / 唤醒 + 主机权重回填 + 池", "32.2 s", "主机权重副本 510 GB/节点占池"],
    ], size=10.5, row_h=0.4)
    table(s, 0.5, 3.72, [1.5, 5.2], [
        ["控制器", "怎么决定切换"],
        ["理想前瞻", "已知未来需求，作为上界"],
        ["滞后一窗", "看上一窗需求 × 1.05 余量，当前配比承不住才换"],
        ["统计判据", "Evolving Infra 做法：5 / 10 分钟窗，逐 60 s 收益均值 > 2 倍标准误才切"],
    ], size=10.5, row_h=0.4, left=True)
    text(s, 0.5, 5.36, 6.7, 0.3, "D→P = 解码节点转为预填充节点（慢的方向）；P:D = 预填充与解码节点数之比",
         size=9.5, color=GRAY)
    label(s, 7.4, 1.2, 5.4, "配置")
    table(s, 7.4, 1.52, [1.15, 4.28], [
        ["项", "取值"],
        ["集群", "14 节点 × 16 NPU；HBM KV 296 GB/节点"],
        ["节点约束", "预填充节点利用率 ≤ 80%；解码节点 KV 占用 ≤ 90%；混部节点预填充占比 ≤ 50%"],
        ["解码", "每步 25 ms（假设）；TPOT（每输出 token 时延）目标 50 ms"],
        ["模型", "5 分钟窗口流体模型，24 h；命中率取理论上限"],
        ["指标", "goodput（有效吞吐）= 过载窗口 ≤ 1% 时的最大负载倍数，相对静态最优 P:D 报"],
    ], size=10, row_h=0.42, left=True, row_hs=[0.34, 0.4, 0.58, 0.4, 0.4, 0.58])
    note(s, 7.4, 4.36, 5.43, 1.3, [
        "口径：S1–S5 的到达率包络为人为注入，会话内时间保持 trace 原值；流体模型不排队、不出 TTFT 分位；"
        "解码与切换耗时在 A3 上无实测。"], size=10)
    table(s, 0.5, 5.78, [1.761] * 7, [
        ["S0", "S1【注入】", "S2 / S2m【注入】", "S4【注入】", "S5【注入】", "S3【注入】", "负载"],
        ["平稳，未注入", "只变总量：24 h 正弦，峰谷 2:1", "只变配比：Claude Code 需求占比 50% ± 40% / ± 15%",
         "同 S2 幅度，2 h 周期快变", "配比阶跃：预填充占比 0.5 ↔ 0.8，24 h 3 次", "总量 + 配比同时变",
         "Claude Code ↔ Qwen coder 组合"],
    ], size=9.5, row_h=0.32, left=True, row_hs=[0.32, 0.92])
    footer(s, "kvcm/sim/params.md「PD 转换」；README「PD 转换」；results/pd/summary.md §1、§3、§6")

    # 5 PD 转换 · 图与结论
    s = prs.slides.add_slide(BLANK)
    header(s, "14 节点 · PD 转换", "【注入】配比变化时 PD 动态转换有效：S3 有效吞吐 +18.6%；无池丢 KV 快切在 S5 只剩 +1.7%", next(pages), PW)
    picture(s, RES / "pd" / "figures" / "pd_goodput.png", 0.75, 1.18, w=7.5)
    table(s, 0.5, 4.86, [2.6, 1.08, 1.08, 1.08, 1.08, 1.08], [
        ["相对静态最优 P:D", "S1", "S2", "S4", "S5", "S3"],
        ["动态 P:D（理想 · 池）", "+0.0%", "+19.0%", "+0.7%", "+11.7%", "+18.6%"],
        ["滞后一窗 · 逻辑切换 + 池", "+0.0%", "+18.0%", "−2.4%", "+6.5%", "+13.5%"],
        ["统计判据 5 min · 池", "+0.0%", "+19.0%", "−2.7%", "+6.0%", "+17.8%"],
        ["无池丢 KV 重算 · 滞后", "+0.0%", "+17.8%", "−4.9%", "−2.5%", "+12.2%"],
    ], size=10, row_h=0.4, hl_rows=(1,))
    cards(s, [
        ("S3 +18.6%", "总量 + 配比同时变化，动态 P:D 理想上界；滞后一窗 + 池 +13.5%（S1 只变总量，0%）"),
        ("S5 +11.7% vs +1.7%", "配比阶跃：有池 vs 无池丢 KV 重算（理想前瞻）；全天过载窗口 0 vs 6", TEAL),
        ("判据 ≈ 滞后", "S5 统计判据 5 分钟 +6.0%，滞后 1.05× 余量 +6.5%，无余量 −0.2%：余量比判据更关键", GRAY),
    ], big_size=22)
    footer(s, "kvcm/sim/results/pd/summary.md §2、§3a–§3c（S1–S5 为【注入】到达率包络，会话内时间保持 trace 原值）")

    # 6 局部性 · 机制与口径
    s = prs.slides.add_slide(BLANK)
    header(s, "14 节点 · 局部性", "机制与口径：池块有所在节点，读池时间计入排队与网卡争用；KVCM + 局部性让请求跟着数据走", next(pages), PW)
    label(s, 0.5, 1.2, 6.5, "机制：放置、副本与路由")
    table(s, 0.5, 1.52, [1.75, 2.05, 1.35, 1.65], [
        ["变体", "池块放在哪", "副本", "路由"],
        ["扁平共享池", "按页键哈希条带到 14 节点", "单副本", "缓存感知（主线）"],
        ["写本机", "写到写入请求所在节点", "单副本", "缓存感知"],
        ["KVCM + 局部性", "写本机", "热页定向副本", "预测 TTFT，计入数据位置"],
        ["消融", "写本机", "热页定向副本", "预测 TTFT，不计数据位置"],
    ], size=10.5, row_h=0.5, hl_rows=(3,))
    label(s, 0.5, 4.2, 6.5, "读池时间怎么算")
    text(s, 0.5, 4.52, 6.8, 1.3, [
        "· 计入 TTFT 的池读时间 = max(0, 读时长 − 排队等待)",
        "· 每节点出口、入口、本机 H2D（主机到设备拷贝）各一条先到先服务链路",
        "· 每实例 16 个预填充槽，TTFT 含排队；被动副本、热点复制见冷热复制页",
    ], size=11, color=INK, spacing=1.3)
    label(s, 7.5, 1.2, 5.3, "配置")
    table(s, 7.5, 1.52, [1.55, 3.78], [
        ["项", "取值"],
        ["传输", "RoCE（以太网远程直接内存访问）：本机 47、跨机 23.8 GB/s"],
        ["池与 HBM", "池 14 TB；HBM 296 GB/实例"],
        ["回放", "6 h 窗口（前 2 h 预热），切片 1/32，seed 1"],
        ["单因素【注入】", "跨机带宽 1/4、1/10；背景流量 50% / 70%；KV 传输 ×8"],
        ["最坏组合【注入】", "HBM 1/4 + KV ×8 + 跨机带宽 1/10 + 背景 70%"],
    ], size=10, row_h=0.5, left=True, row_hs=[0.34, 0.58, 0.42, 0.42, 0.58, 0.58])
    note(s, 7.5, 4.62, 5.33, 1.5, [
        "口径：这里的 TTFT 来自排队模型、含排队，与 hook 命中页的分析式 TTFT 不可直接比较；"
        "本机读走 H2D、不占网卡是模型假设；Claude Code 最坏组合的消融格未跑完。"], size=10.5)
    footer(s, "kvcm/sim/params.md §6、§7；README「池局部性扩展」「压力敏感性与 KVCM 增益区间」")

    # 7 局部性 · 图与结论
    s = prs.slides.add_slide(BLANK)
    header(s, "14 节点 · 局部性", "【注入】局部性在网卡紧张时起作用，网络空闲时无影响：最坏组合下仅 KVCM + 局部性稳定", next(pages), PW)
    picture(s, stress2, 1.67, 1.15, w=10.0)
    text(s, 0.5, 4.97, 12.33, 0.45, [
        "TTFT P95（对数轴），左 Claude Code、右 Qwen coder。最坏组合 = HBM 1/4 + KV ×8 + 跨机带宽 1/10 + 背景 70%；"
        "其余变体在最坏组合下 P95 为 4.2e+04–1.4e+05 s（Claude Code）、5.2e+03–2.0e+04 s（Qwen coder）；Claude Code 最坏组合的消融格未跑完"],
        size=9.5, color=GRAY, align=PP_ALIGN.CENTER)
    w3 = (12.33 - 2 * 0.12) / 3
    for i, (big, cap, acc) in enumerate([
        ("< 2%", "网络空闲：【注入】单因素压力（带宽 1/4、1/10，背景 50 / 70%，KV ×8）下各变体 TTFT P95 变化", GRAY),
        ("1.56 s / 0.57 s", "网卡紧张：【注入】最坏组合出口负载 158–758%，仅 KVCM + 局部性稳定（Claude Code / Qwen），其余变体失稳", GREEN),
        ("15.7 s / 508 s", "【注入】消融：路由不计数据位置时 Claude Code 带宽 1/4、1/10 的 P95；同档 KVCM + 局部性 1.15 s", AMBER),
    ]):
        card(s, 0.5 + i * (w3 + 0.12), 5.47, w3, 1.55, big, cap, acc, 20)
    footer(s, "kvcm/sim/results/locality.md「压力敏感性【注入】」；README「压力敏感性结论」；图：stress_single_factor.png（TTFT 面板）")

    # 8 冷热复制 · 机制与口径
    s = prs.slides.add_slide(BLANK)
    header(s, "14 节点 · 冷热复制", "机制与口径：比较三种副本做法；【注入】一个热文档集中在少数节点的场景，找复制转为净收益的拐点", next(pages), PW)
    label(s, 0.5, 1.2, 6.5, "机制：何时复制、复制到哪")
    table(s, 0.5, 1.52, [2.05, 1.55, 1.75, 1.55], [
        ["做法", "何时复制", "副本放哪", "代价"],
        ["被动副本（Mooncake 式）", "每次跨机读后", "读者节点留一份", "占池容量"],
        ["热点复制（TokenLake 式）", "目标副本数 r > 1", "出口最闲的节点", "复制流量 + 占池"],
        ["KVCM 热页定向副本", "同上门控", "路由目标处就地留存", "只占池，无额外流量"],
        ["扁平哈希池", "不复制", "按页键条带到全部节点", "—"],
    ], size=10.5, row_h=0.5, row_hs=[0.36, 0.55, 0.55, 0.55, 0.55])
    text(s, 0.5, 4.2, 6.9, 0.9, [
        "复制门控：r = ⌈λ · τ / ρ⌉，λ 为该页池读速率，τ 为单次读时长，ρ = 0.5（按 Little 定律）。",
        "现有三条 trace 的放置与副本实验：14 节点，池 14 TB，RoCE + 网卡争用，读不与排队重叠。",
    ], size=10.5, color=INK, spacing=1.3)
    label(s, 7.6, 1.2, 5.2, "热点场景配置【注入】")
    table(s, 7.6, 1.52, [1.1, 4.13], [
        ["项", "取值"],
        ["负载", "合成：共享 128K token 长文档，每会话 1–4 轮问答"],
        ["热集合", "集群 HBM 的 2 倍（对照 0.5 倍）：HBM 放不下，约一半请求读池"],
        ["集中度", "写本机下热文档主副本只在 1 / 2 / 3 / 4 / 7 / 14 个节点"],
        ["到达率", "全网池读需求 ≈ 出口容量 20%；1 / 2 / 3 个热节点时热节点出口需求 ≈ 271% / 136% / 92%"],
        ["回放", "3 h 窗口（前 1 h 预热），切片 1/32"],
    ], size=10, row_h=0.5, left=True, row_hs=[0.34, 0.58, 0.58, 0.58, 0.78, 0.4])
    note(s, 0.5, 5.45, 12.33, 0.9, [
        "口径：热点场景为合成负载【注入】，不改原有 trace；TTFT 含排队；出口负载 = 需求 / 容量（> 100% 即需求超过网卡容量）。"], size=11)
    footer(s, "kvcm/sim/params.md §6、§8；README「池局部性扩展」「热点复制增益场景【注入】」")

    # 9 冷热复制 · 图与结论
    s = prs.slides.add_slide(BLANK)
    header(s, "14 节点 · 冷热复制", "冷热复制在现有三条 trace 下无收益；【注入】只在数据被集中到少数节点、热节点出口 ≳ 90% 时有净收益，哈希条带本身没有热点", next(pages), PW)
    picture(s, FIG / "locality" / "hotspot" / "hotspot_scan.png", 1.67, 1.15, w=10.0)
    w5 = (12.33 - 4 * 0.1) / 5
    for i, (big, cap, acc) in enumerate([
        ("2.172 → 2.577 s", "现有三条 trace：被动副本占池 28.1%，读池 P95 变差；热点复制单因素下不触发", AMBER),
        ("−67 / −86 / −98%", "【注入】热点集中：写本机 → 热点复制，1 / 2 / 3 个热节点的 P95", RED),
        ("拐点 66–92%", "【注入】热节点出口负载在此区间转为净收益", TEAL),
        ("0.17 s vs 57 s", "【注入】单热节点：KVCM 读者就地副本 + 位置路由 对 只路由", GREEN),
        ("≈ 0.16 s", "【注入】扁平哈希池把热文档条带到全部节点，P95 始终约 0.16 s", GRAY),
    ]):
        card(s, 0.5 + i * (w5 + 0.1), 4.62, w5, 2.36, big, cap, acc, 17)
    footer(s, "kvcm/sim/results/locality.md E2、「热点复制增益场景【注入】」（热集合 2× 集群 HBM）；README")

    # 10 14 节点 hook 命中 · 机制与口径
    s = prs.slides.add_slide(BLANK)
    header(s, "14 节点 · hook 命中", "机制与口径：hook 机制同 6 节点；负载按“理想命中下预填充占 50%”标定，单 seed，作为基线", next(pages), PW)
    label(s, 0.5, 1.2, 8.0, "配置：14 节点基线与 6 节点主口径对照")
    table(s, 0.5, 1.52, [1.4, 3.75, 3.15], [
        ["项", "14 节点基线", f"6 节点主口径（第 2–{3 + d} 页）"],
        ["集群", "14 × 16 = 224 NPU，HBM 共 4.15 TB", "6 × 16 = 96 NPU，HBM 共 1.78 TB"],
        ["DRAM 池", "0.44–28 TB（标称 14 TB），重点看 3.5 / 14 TB", "0.18–6 TB（标称 6 TB）+ 9 / 12 TB"],
        ["策略", "实例私有 LRU、共享池 LRU、KVCM 现实 / 理想 hook", "共享池 LRU、KVCM 现实 / 理想 hook"],
        ["负载锚点", "理想命中下预填充占 NPU 50%（只算预填充）", "6 TB 共享池 LRU 实际预填充占 70%"],
        ["到达率", "Claude Code 10,871 会话/h、Codex 59,259、Qwen coder 1,626,604", "为左列的 0.471 / 0.596 / 0.571 倍"],
        ["seed", "1", "1 / 2 / 3"],
        ["理论上限", "96.24% / 96.54% / 70.97%", "96.10% / 96.56% / 70.98%"],
    ], size=10, row_h=0.44, left=True, row_hs=[0.36, 0.44, 0.44, 0.44, 0.44, 0.58, 0.36, 0.36])
    label(s, 8.95, 1.2, 3.9, "hook 细节")
    cards(s, [
        ("驱逐顺序", "保活钉住（不可驱逐）> 有活会话引用 > 零引用；零引用的块先被驱逐", RED),
        ("保活范围", "只钉预计停顿 ≤ 2 × 池 LRU 驻留时间的会话；钉住预算占池 50%（现实 hook 10%）", TEAL),
        ("现实 hook 怎么猜", "停顿 = 该会话最近 5 次间隔的几何均值；理想 hook 用真实间隔", GRAY),
    ], x=8.95, y=1.52, w=3.88, h=1.42, gap=0.1, big_size=17)
    note(s, 0.5, 5.95, 12.33, 1.05, [
        "口径：TTFT 为分析式、不含排队；Claude Code 3.5 TB 池共享池需 251.9 NPU，超过 224 NPU 属过载点，P95 偏乐观；"
        "50% 锚点同样只算预填充、不含解码；切片 1/32，10 h 窗口（前 3 h 预热）。"], size=10.5)
    footer(s, "kvcm/sim/params.md §1–§5；README「方法」「hook 三档」；results/summary.md")

    # 11 14 节点 hook 命中 · 图与结论
    s = prs.slides.add_slide(BLANK)
    header(s, "14 节点 · hook 命中", "池在 1–2 倍集群 HBM 时，理想 hook 比共享池多命中 0.7–3.45 个百分点；现实 hook 拿不到，Codex 已贴近理论上限", next(pages), PW)
    picture(s, FIG / "mrc_cc256k.png", 0.5, 1.22, w=8.0)
    table(s, 0.5, 4.95, [2.6, 1.2, 1.7, 1.2, 1.3], [
        ["Claude Code · DRAM 池 3.5 TB", "命中率", "预填充 NPU", "TTFT P95", "相对共享池"],
        ["共享池 LRU", "91.56%", "251.9（过载）", "2.38 s", "—"],
        ["KVCM（现实 hook）", "91.60%", "250.9（过载）", "2.31 s", "−0.9 NPU"],
        ["KVCM（理想 hook）", "93.03%", "210.4", "1.64 s", "−41.5 NPU"],
    ], size=10.5, row_h=0.4, hl_rows=(3,))
    text(s, 0.5, 6.62, 8.0, 0.45,
         "1–2 倍集群 HBM（4.15 / 8.29 TB）理想 hook − 共享池：Claude Code +1.39 / +0.71，Qwen coder +3.45 / +1.28 个百分点；"
         "过载 = 超过 224 NPU，TTFT 不含排队",
         size=9.5, color=GRAY)
    cards(s, [
        ("251.9 → 210.4", "Claude Code 3.5 TB 池理想 hook 的预填充 NPU（−41.5），回到 224 NPU 以内；现实 hook 只省 0.9"),
        ("Qwen +3.70 个百分点", "3.5 TB 池：共享池 64.82% → 理想 hook 68.52%，NPU 127.4 → 118.0；14 TB 只差 +0.35", TEAL),
        ("Codex ≈ 0", "3.5 TB 共享池已 96.49%（理论上限 96.54%）：96% 的停顿 < 60 s，没有可保留的长停顿", GRAY),
    ], big_size=21)
    footer(s, "kvcm/sim/results/summary.md（命中率–容量表、TTFT 与等效 NPU 表）；图：mrc_cc256k.png")

    prs.save(out)
    print(out)
    sys.exit(0)


if "--n11" in sys.argv:
    build_n11()
if "--n12" in sys.argv:
    build_n11(n12=True)


if "--short" in sys.argv:
    build_short()


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
text(s, 0.78, 1.35, 7.8, 0.85, "KVCM 仿真结论", size=38, color=WHITE, bold=True)
text(s, 0.78, 2.1, 7.9, 1.7,
     [[("保留", {"color": GOLD, "bold": True}), ("（理想 hook）和 ", {}),
       ("PD 动态转换", {"color": GOLD, "bold": True}), ("带来常态收益；", {})],
      [("局部性", {"color": GOLD, "bold": True}), ("在网卡紧张时起作用，网络空闲时无影响；", {})],
      [("冷热复制", {"color": GOLD, "bold": True}),
       ("在现有三条 trace 下无收益；【注入】只在数据被集中到少数节点、热节点出口 ≳ 90% 时有净收益，哈希条带本身没有热点。", {})]],
     size=18, color=WHITE, spacing=1.2)
rect(s, 0.78, 3.8, 1.65, 0.05, RED)
table(s, 0.78, 3.93, [1.35, 6.35], [
    ["方向", "结论（关键数字）"],
    ["保留", "CC 3.5 TB 池：理想 hook NPU 251.9 → 210.4，现实 hook 仅 250.9"],
    ["PD 动态转换", "【注入】S3 goodput +18.6%（滞后控制器 + 池 +13.5%）"],
    ["局部性", "网卡紧张时起作用：【注入】最坏组合出口负载 158–758%，仅 KVCM + 局部性稳定在 "
               "1.56 s / 0.57 s，其余变体失稳；消融里路由不计位置时 CC 带宽 1/4 即 15.7 s、1/10 达 508 s。"
               "网络空闲时无影响：单因素 P95 变化 < 2%"],
    ["冷热复制", "现有三条 trace 下无收益（触发时 P95 反而变差 2–3 倍，副本占池 28–65%）；"
                 "【注入】数据被集中到少数节点、热节点出口 ≳ 90% 时有净收益（写本机 → 热点复制 P95 −67% / −86% / −98%），"
                 "扁平哈希池 P95 始终约 0.16 s"],
], size=10.5, row_h=0.4, left=True, row_hs=[0.36, 0.4, 0.4, 0.88, 0.88])
text(s, 0.78, 6.98, 7.8, 0.36,
     "DeepSeek V4.1-Flash · Ascend A3 14 节点 × 16 die · 三条 trace · 单次运行（seed=1），无真机校准",
     size=10.5, color=SOFT)
text(s, 9.2, 0.9, 3.8, 0.4, "目录", size=13.5, color=GOLD, bold=True)
toc = [("01", "总结论"), ("02", "仿真口径"), ("03", "命中率与 NPU / TTFT"),
       ("04", "KVCM 增益区间【注入】"), ("05", "PD 动态转换 goodput【注入】"),
       ("06", "网络空闲：局部性与冷热复制"), ("07", "热点场景：热点复制【注入】"),
       ("08", "网卡紧张：局部性【注入】"), ("09", "设计建议"), ("10", "口径与局限")]
for i, (n, t) in enumerate(toc):
    yy = 1.4 + i * 0.56
    rect(s, 9.2, yy, 3.75, 0.46, CARD_DARK)
    rect(s, 9.2, yy, 0.07, 0.46, [RED, TEAL, AMBER, GREEN][i % 4])
    text(s, 9.4, yy, 3.5, 0.46, [[(n + "  ", {"color": GOLD, "bold": True}), (t, {})]],
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
header(s, "04  局部性", "网络空闲时局部性无影响；现有三条 trace 下冷热复制无收益，热点只在数据被集中到少数节点时出现（见下页）", 6)
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

# ---------- 7 热点场景 ----------
s = prs.slides.add_slide(BLANK)
header(s, "04  冷热复制", "【注入】热点复制只在数据被集中到少数节点、热节点出口 ≳ 90% 时有净收益；哈希条带本身没有热点", 7)
picture(s, FIG / "locality" / "hotspot" / "hotspot_scan.png", 1.27, 1.15, w=10.8)
w4 = (12.33 - 3 * 0.12) / 4
for i, (big, cap, acc) in enumerate([
    ("−67% / −86% / −98%", "写本机 → TokenLake 热点复制，1 / 2 / 3 个热节点的 P95（559 → 184、135 → 18.9、12.5 → 0.26 s）", RED),
    ("≈ 0.16 s", "扁平哈希池把热文档条带到全部节点，热节点出口始终 20%，P95 始终约 0.16 s", TEAL),
    ("0.17 s vs 57 s", "单热节点：KVCM 读者就地副本 + 位置路由 对 只路由；副本占池 22.6%，无额外复制流量", GREEN),
    ("拐点 66–92%", "热节点出口在 66%（4 节点）与 92%（3 节点）之间转为净收益；被动副本仍是净成本（副本占池 48%，命中 −5.3 pt）", AMBER),
]):
    card(s, 0.5 + i * (w4 + 0.12), 5.0, w4, 2.0, big, cap, acc, 19)
footer(s, "kvcm/sim/results/locality.md「热点复制增益场景【注入】」（热集合 2× 集群 HBM）；kvcm/sim/README.md 同名节")

# ---------- 8 压力与最坏组合 ----------
s = prs.slides.add_slide(BLANK)
header(s, "04  局部性", "【注入】网卡紧张时局部性是防止排队失稳的关键：最坏组合下仅 KVCM + 局部性稳定", 8)
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
header(s, "05  建议", "设计建议：池做简单（哈希条带、单副本），复杂度投在保留策略和路由（数据位置 + 队列 + 命中）", 9)
table(s, 0.5, 1.35, [1.6, 3.0, 3.4], [
    ["方向", "建议", "依据"],
    ["池放置", "哈希条带、单副本即可避免热点；只有放置被迫集中（如写本机）时才需要热页定向副本 + 位置路由",
     "现有 trace 下被动副本读池 P95 +18%；【注入】扁平哈希池 P95 始终约 0.16 s，单热节点时 KVCM 0.17 s 对只路由 57 s"],
    ["保留策略", "会话感知保留 / 驱逐，投入“预测未来复用”的 hook",
     "CC 3.5 TB：理想 hook 210.4 NPU vs 现实 hook 250.9"],
    ["路由", "路由计入数据位置（网卡紧张时防止排队失稳），并看队列和命中",
     "【注入】不计位置时 CC 带宽 1/4 即 15.7 s；过载区只看 TTFT 会掉命中 1.4–2.0 pt；预测 TTFT 路由 CC P95 2.005 → 1.096 s"],
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
header(s, "06  局限", "口径与局限：注入场景、6 h 窗口和 1/32 切片都是近似，结论以相对比较为主", 10)
table(s, 0.5, 1.35, [2.0, 6.0], [
    ["项", "说明"],
    ["【注入】项", "PD S1–S5 到达率包络；增益网格的负载倍数 / 池 / HBM；压力点（带宽、背景流量、KV ×8、最坏组合）；热点场景（共享 128K 长文档集中在少数节点，窗口 3 h）"],
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
