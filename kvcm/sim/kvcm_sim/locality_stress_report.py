"""压力敏感性与 KVCM 增益区间（results/locality/*_stress.json、*_grid*.json）→ 图 + locality.md 新章节。

由 `kvcm_sim.locality_report` 调用。
"""

from __future__ import annotations

import glob
import json
import os

import numpy as np
from matplotlib.patches import Patch

from .locality_report import LFIG, LOC, TR_SHORT, VAR_LABEL, plt
from .params import DEFAULT as P

SFIG = os.path.join(LFIG, "stress")
TRS = ["cc256k", "qwen_coder"]
NPU_CAP = P.n_inst * P.npu_per_inst
POINT_LABEL = {
    "base": "基线", "bw1/4": "跨机带宽 1/4", "bw1/10": "跨机带宽 1/10",
    "bg30": "背景流量 30%", "bg50": "背景流量 50%", "bg70": "背景流量 70%",
    "kv4x": "KV 传输 ×4", "kv8x": "KV 传输 ×8", "C3": "最坏组合",
}
POINTS = list(POINT_LABEL)
SVARS = ["flat", "local", "local+passive", "hash+hot", "kvcm", "kvcm+loc", "kvcm+noloc"]
SLABEL = {**VAR_LABEL, "kvcm+noloc": "KVCM + 局部性，TTFT 路由不计数据位置（消融）",
          "kvcm-oracle": "KVCM（理想 hook，哈希放置）"}
SCOL = ["#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#ff7f0e", "#d62728", "#e377c2"]
GROWS = [("pool", 0.25, "池 3.5 TB"), ("pool", 0.5, "池 7 TB"), ("pool", 1.0, "标称：池 14 TB · HBM 296 GB"),
         ("hbm", 0.5, "HBM 1/2（148 GB）"), ("hbm", 0.25, "HBM 1/4（74 GB）")]
GLOADS = [1.0, 1.5, 2.0]
HOOKS = {"real": ("kvcm", "kvcm+loc", "现实 hook"), "ideal": ("kvcm-oracle", "kvcm-oracle+loc", "理想 hook")}
TH_NPU, TH_P95 = 0.05, 0.10
NOTE = ("【注入】全部压力点与网格都是人为设定；trace 数据集不改。切片 1/32、回放窗口 6 h（前 2 h 预热，"
        "主线为 10 h / 3 h），RoCE，链路 FCFS 排队模型。")


def _merge(pattern):
    out = []
    for p in sorted(glob.glob(os.path.join(LOC, pattern))):
        out += json.load(open(p))
    return out


def _sel(runs, **kw):
    out = [r for r in runs if all(r.get(k) == v for k, v in kw.items())]
    return out[-1] if out else None


def _gpt(f, ax, v, m):
    if ax == "pool":
        return f"pool={v:g}@{m:g}x"
    return f"{ax}={v:g}@{m:g}x"


def _pc(x):
    return "—" if x is None else f"{x * 100:+.0f}%"


def _g(a, b):
    """b 相对 a 的下降比例（正 = KVCM 更好）。"""
    return (a - b) / a if a else 0.0


# ------------------------------------------------------------------ 压力扫描
def fig_stress(data):
    fig, axes = plt.subplots(len(TRS), 3, figsize=(17, 4.6 * len(TRS)))
    for r, t in enumerate(TRS):
        runs = data.get(t, [])
        pts = [p for p in POINTS if any(x["point"] == p for x in runs)]
        for c, (key, ylab, logy) in enumerate((("des_ttft_p95", "TTFT P95（s，对数轴）", True),
                                                ("exposed_mean", "未掩盖池读 均值（ms，对数轴）", True),
                                                ("egress_util_tot_max", "最忙出口负载（含背景，需求/容量）", False))):
            ax = axes[r][c]
            for j, v in enumerate(SVARS):
                ys = []
                for p in pts:
                    x = _sel(runs, point=p, variant=v)
                    y = x[key] if x else np.nan
                    ys.append(max(y * 1000, 1e-3) if key == "exposed_mean" else y)
                ax.plot(range(len(pts)), ys, marker="o", color=SCOL[j], label=SLABEL[v],
                        ls="--" if v == "kvcm+noloc" else "-")
            ax.set_xticks(range(len(pts)))
            ax.set_xticklabels([POINT_LABEL[p] for p in pts], rotation=35, ha="right", fontsize=8)
            if logy:
                ax.set_yscale("log")
            if key == "egress_util_tot_max":
                ax.axhline(1.0, color="#999", lw=0.8, ls=":")
            ax.set_ylabel(ylab)
            ax.set_title(f"{TR_SHORT[t]}")
            ax.grid(alpha=0.3)
    h, l = axes[0][0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=4, fontsize=8, bbox_to_anchor=(0.5, 0.965))
    fig.suptitle("压力敏感性【注入】：单因素 + 最坏组合（RoCE，标称 14 TB 池）")
    fig.text(0.01, 0.005, NOTE, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.02, 1, 0.91))
    fig.savefig(os.path.join(SFIG, "stress_single_factor.png"), dpi=150)
    plt.close(fig)


def md_stress(data, zipf):
    md = ["", "## 压力敏感性【注入】（单因素 + 最坏组合）", "",
          NOTE + " 压力点：跨机带宽 ×1/4、×1/10；每节点网卡背景流量均值 30/50/70%（各节点取 均值 × U(0.5, 1.5)，"
          "同时占出口与入口）；KV 传输字节 ×4、×8（HBM 与池按 token 容量不变，只放大传输，模拟 KV 大的模型）；"
          "最坏组合 = HBM 1/4 + KV ×8 + 跨机带宽 1/10 + 背景 70%。HBM 缩小见下一节增益网格。",
          "",
          "“最忙出口负载”= 最忙节点（背景 + 池读需求）/ 链路容量；> 100% 表示链路饱和、排队无界增长。"
          "“KVCM + 局部性，TTFT 路由不计数据位置”是消融：放置与副本同 KVCM + 局部性，路由只看排队与命中。", ""]
    for t in TRS:
        runs = data.get(t, [])
        if not runs:
            continue
        md += [f"### {TR_SHORT[t]}：TTFT P95（s）", "",
               "| 压力点 | " + " | ".join(SLABEL[v] for v in SVARS) + " |",
               "|---|" + "---:|" * len(SVARS)]
        for p in POINTS:
            row = [_sel(runs, point=p, variant=v) for v in SVARS]
            if not any(row):
                continue
            md.append(f"| {POINT_LABEL[p]} | " + " | ".join(f"{x['des_ttft_p95']:.3g}" if x else "—" for x in row) + " |")
        md += ["", f"### {TR_SHORT[t]}：逐点明细", "",
               "| 压力点 | 变体 | 命中率 | 读池请求占比 | 未掩盖读 均值 (ms) | 最忙出口负载 | TTFT 均值 (s) | TTFT P95 (s) | prefill NPU | 副本占池 | 热点复制 GB/s |",
               "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for p in POINTS:
            for v in SVARS:
                x = _sel(runs, point=p, variant=v)
                if not x:
                    continue
                md.append(f"| {POINT_LABEL[p]} | {SLABEL[v]} | {x['hit_rate'] * 100:.2f}% | {x['pool_req_frac'] * 100:.1f}% | "
                          f"{x['exposed_mean'] * 1000:.3g} | {x['egress_util_tot_max'] * 100:.0f}% | {x['des_ttft_mean']:.3g} | "
                          f"{x['des_ttft_p95']:.3g} | {x['npu_busy']:.1f} | {x['replica_share'] * 100:.1f}% | {x['copy_gb_per_s']:.2f} |")
        md.append("")
    if zipf:
        md += ["### 合成 Zipf 共享长文档负载在压力下（α = 0.6，最倾斜的一档）", "",
               "| 压力点 | 变体 | 读池请求占比 | 未掩盖读 均值 (ms) | 最忙出口负载 | TTFT 均值 (s) | TTFT P95 (s) | 副本占池 | 热点复制 GB/s |",
               "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for x in sorted(zipf, key=lambda r: (POINTS.index(r["point"]), r["variant"])):
            md.append(f"| {POINT_LABEL[x['point']]} | {SLABEL[x['variant']]} | {x['pool_req_frac'] * 100:.1f}% | "
                      f"{x['exposed_mean'] * 1000:.3g} | {x['egress_util_tot_max'] * 100:.0f}% | {x['des_ttft_mean']:.3g} | "
                      f"{x['des_ttft_p95']:.3g} | {x['replica_share'] * 100:.1f}% | {x['copy_gb_per_s']:.2f} |")
        md.append("")
    return md


# ------------------------------------------------------------------ 增益网格
def gains(runs, ax, v, m):
    pt = _gpt(None, ax, v, m)
    base = _sel(runs, point=pt, variant="flat")
    if not base:
        return None
    out = {"base": base, "over": base["npu_busy"] > NPU_CAP}
    ro = _sel(runs, point=pt, variant="local+ttft")
    out["route_only"] = _g(base["des_ttft_p95"], ro["des_ttft_p95"]) if ro else None
    for h, (vr, vl, _) in HOOKS.items():
        r, lo = _sel(runs, point=pt, variant=vr), _sel(runs, point=pt, variant=vl)
        if not r or not lo:
            continue
        d = {}
        for name, x in (("ret", r), ("loc", lo)):
            d[name] = dict(hit=(x["hit_rate"] - base["hit_rate"]) * 100, npu=_g(base["npu_busy"], x["npu_busy"]),
                           mean=_g(base["des_ttft_mean"], x["des_ttft_mean"]), p95=_g(base["des_ttft_p95"], x["des_ttft_p95"]),
                           run=x)
        # 来源：NPU 只随命中率变；P95 拆成 保留（共享→KVCM）与 局部性/路由（KVCM→KVCM+局部性）
        ret_ok = d["ret"]["npu"] >= TH_NPU or (d["ret"]["p95"] >= TH_P95 and not out["over"])
        loc_ok = d["loc"]["npu"] >= TH_NPU or (d["loc"]["p95"] >= TH_P95 and not out["over"])
        ret_p95 = d["ret"]["p95"]
        loc_p95 = d["loc"]["p95"] - d["ret"]["p95"]
        npu_ok = ret_ok or loc_ok
        p95_ok = False
        if ret_ok:
            src = "保留"
        elif loc_ok:
            src = "路由"
            if out["route_only"] is not None and out["route_only"] >= 0.8 * d["loc"]["p95"]:
                src = "路由（共享池加同样路由也能拿到）"
        else:
            src = ""
        d["hit"] = npu_ok or p95_ok
        d["src"] = src
        d["ret_p95"], d["loc_p95"] = ret_p95, loc_p95
        out[h] = d
    return out


def fig_grid(data):
    fig, axes = plt.subplots(len(HOOKS), len(TRS), figsize=(7.2 * len(TRS), 4.6 * len(HOOKS)))
    for c, t in enumerate(TRS):
        runs = data.get(t, [])
        for r, h in enumerate(HOOKS):
            ax = axes[r][c]
            ax.set_xlim(-0.5, len(GLOADS) - 0.5)
            ax.set_ylim(-0.5, len(GROWS) - 0.5)
            for yi, (gax, gv, _) in enumerate(GROWS):
                for xi, m in enumerate(GLOADS):
                    g = gains(runs, gax, gv, m)
                    if not g or h not in g:
                        ax.add_patch(plt.Rectangle((xi - 0.5, yi - 0.5), 1, 1, color="#eee"))
                        ax.text(xi, yi, "未跑", ha="center", va="center", fontsize=8, color="#999")
                        continue
                    d = g[h]
                    rr, ll = d["ret"], d["loc"]
                    col = ("#2ca02c" if d["src"] == "保留" else "#ff7f0e") if d["hit"] else "#f2f2f2"
                    ax.add_patch(plt.Rectangle((xi - 0.5, yi - 0.5), 1, 1, color=col, alpha=0.55 if d["hit"] else 1))
                    if g["over"]:
                        ax.add_patch(plt.Rectangle((xi - 0.5, yi - 0.5), 1, 1, fill=False, hatch="//", ec="#888", lw=0))
                    p95s = "过载" if g["over"] else f"{-rr['p95'] * 100:+.0f}% → {-ll['p95'] * 100:+.0f}%"
                    ax.text(xi, yi, f"保留：命中 {rr['hit']:+.2f} pt，NPU {-rr['npu'] * 100:+.1f}%\n"
                            f"P95 保留 → +路由 {p95s}" + (f"\n来源：{d['src']}" if d["src"] else ""),
                            ha="center", va="center", fontsize=7)
            ax.set_xticks(range(len(GLOADS)))
            ax.set_xticklabels([f"{m:g}×" for m in GLOADS])
            ax.set_yticks(range(len(GROWS)))
            ax.set_yticklabels([lab for *_, lab in GROWS], fontsize=8)
            ax.set_xlabel("负载倍数【注入】（会话到达率 ×）")
            ax.set_title(f"{TR_SHORT[t]} · KVCM {HOOKS[h][2]} vs 共享池 LRU", fontsize=10)
    fig.legend(handles=[Patch(color="#2ca02c", alpha=0.55, label="增益达标，主要来自保留（命中率）"),
                        Patch(color="#ff7f0e", alpha=0.55, label="增益达标，主要来自路由 / 局部性"),
                        Patch(color="#f2f2f2", label="未达标"),
                        Patch(fill=False, hatch="//", ec="#888", label="共享池基线 prefill 需求 > 224 NPU（过载，P95 不计）")],
               loc="upper center", ncol=4, fontsize=8, bbox_to_anchor=(0.5, 0.96))
    fig.suptitle(f"KVCM 增益区间【注入】：达标 = prefill NPU 省 ≥ {TH_NPU:.0%} 或 TTFT P95 降 ≥ {TH_P95:.0%}（负数 = KVCM 更省 / 更快）")
    fig.text(0.01, 0.005, NOTE, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.02, 1, 0.92))
    fig.savefig(os.path.join(SFIG, "kvcm_gain_region.png"), dpi=150)
    plt.close(fig)


def md_grid(data):
    md = ["", "## KVCM 增益区间【注入】（负载 × 池 / HBM）", "",
          NOTE + " 负载放大用**提高会话到达率**实现（回放器本来就是从 trace 会话模板有放回抽样、加盐隔离，"
          "等价于复制会话）：会话内请求间隔与复用距离不变，变化的是并发会话数，即同时争 HBM / 池容量的工作集变大。"
          "没有压缩 trace 时间（那会缩短会话内停顿、人为提高会话内复用）。", "",
          f"增益 = 相对共享池 LRU（哈希条带、单副本、主线路由）的下降比例；正数 = KVCM 更好。"
          f"“保留”= KVCM（哈希放置、主线路由）相对共享池，只有保留 / 驱逐策略不同；"
          f"“+局部性”= 再加写本机 + 热页定向副本 + 预测 TTFT 路由；“局部性/路由增量”= 两者之差。"
          f"达标：NPU ≥ {TH_NPU:.0%} 或 P95 ≥ {TH_P95:.0%}（共享池基线 prefill 需求 > {NPU_CAP} NPU 时视为过载，只看 NPU）。", ""]
    for t in TRS:
        runs = data.get(t, [])
        if not runs:
            continue
        md += [f"### {TR_SHORT[t]}", "",
               "| 池 / HBM | 负载 | 共享池 命中率 | 共享池 NPU | 共享池 P95 (s) | hook | 保留：Δ命中 (pt) | 保留：NPU 省 | 保留：TTFT 均值 | 保留：P95 | "
               "+局部性：Δ命中 | +局部性：NPU 省 | +局部性：TTFT 均值 | +局部性：P95 | 局部性/路由增量 P95 | 对照：共享池 + TTFT 路由 P95 | 达标 / 来源 |",
               "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for gax, gv, lab in GROWS:
            for m in GLOADS:
                g = gains(runs, gax, gv, m)
                if not g:
                    continue
                b = g["base"]
                for h in HOOKS:
                    if h not in g:
                        continue
                    d = g[h]
                    rr, ll = d["ret"], d["loc"]
                    ov = "（过载）" if g["over"] else ""
                    md.append(f"| {lab} | {m:g}× | {b['hit_rate'] * 100:.2f}% | {b['npu_busy']:.1f}{ov} | {b['des_ttft_p95']:.3g} | "
                              f"{HOOKS[h][2]} | {rr['hit']:+.2f} | {rr['npu'] * 100:+.1f}% | {rr['mean'] * 100:+.0f}% | {rr['p95'] * 100:+.0f}% | "
                              f"{ll['hit']:+.2f} | {ll['npu'] * 100:+.1f}% | {ll['mean'] * 100:+.0f}% | {ll['p95'] * 100:+.0f}% | "
                              f"{d['loc_p95'] * 100:+.0f}% | {_pc(g['route_only'])} | "
                              f"{'是 · ' + d['src'] if d['hit'] else '否'} |")
        md.append("")
    md += ["![kvcm_gain_region](figures/locality/stress/kvcm_gain_region.png)",
           "![stress_single_factor](figures/locality/stress/stress_single_factor.png)", ""]
    return md


def build():
    os.makedirs(SFIG, exist_ok=True)
    sdata = {t: _merge(f"{t}_stress*.json") for t in TRS}
    gdata = {t: _merge(f"{t}_grid*.json") for t in TRS}
    zipf = _merge("zipf_stress*.json")
    md = []
    if any(gdata.values()):
        fig_grid(gdata)
        md += md_grid(gdata)
    if any(sdata.values()):
        fig_stress(sdata)
        md += md_stress(sdata, zipf)
    return md
