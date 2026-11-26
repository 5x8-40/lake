"""热点复制增益场景【注入】的图与表：`results/locality/hotspot.json` → locality.md 新章节 + 图。"""

from __future__ import annotations

import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .hotspot import DEMAND, DOC_PAGES, HS_VARS, NAMES
from .locality_report import LFIG

OUT = os.path.join(os.path.dirname(__file__), "..", "results", "locality")
HFIG = os.path.join(LFIG, "hotspot")
PAIRS = (("local+hot", "local"), ("local+passive", "local"), ("kvcm+loc", "kvcm+route"))
COL = {"flat": "#1f77b4", "local": "#2ca02c", "local+passive": "#9467bd", "local+hot": "#8c564b",
       "local+ttft": "#7f7f7f", "kvcm+route": "#ff7f0e", "kvcm+loc": "#d62728"}


def _load():
    p = os.path.join(OUT, "hotspot.json")
    if not os.path.exists(p):
        return []
    d = {}
    for r in json.load(open(p)):
        d[(r["variant"], r["n_hot"], r["set_x"])] = r
    return list(d.values())


def _get(data, v, h, sx):
    for r in data:
        if r["variant"] == v and r["set_x"] == sx and (r["n_hot"] == h or v == "flat"):
            return r
    return None


def _f(x):
    return f"{x:.3g}" if x < 1000 else f"{x:.2e}"


def fig(data):
    os.makedirs(HFIG, exist_ok=True)
    sets = sorted({r["set_x"] for r in data})
    hots = sorted({r["n_hot"] for r in data if r["variant"] != "flat" and r["set_x"] == max(sets)})
    sx = max(sets)
    fg, ax = plt.subplots(1, 3, figsize=(17, 5.2))
    for v in HS_VARS:
        xs, p95, eg, rp = [], [], [], []
        for h in hots:
            r = _get(data, v, h, sx)
            if r:
                xs.append(h)
                p95.append(r["ttft_p95"])
                eg.append(r["hot_egress_util"])
                rp.append(r["replica_share"] * 100)
        if not xs:
            continue
        kw = dict(color=COL[v], marker="o", label=NAMES[v], ls="--" if v == "flat" else "-")
        ax[0].plot(xs, p95, **kw)
        ax[1].plot(xs, eg, **kw)
        ax[2].plot(xs, rp, **kw)
    ax[0].set_yscale("log")
    ax[0].set_ylabel("TTFT P95（s，对数轴）")
    ax[1].axhline(1.0, color="k", lw=0.8, ls=":")
    ax[1].set_ylabel("热节点出口负载（需求/容量）")
    ax[2].set_ylabel("副本占池（%）")
    for a in ax:
        a.set_xlabel("持有热文档的节点数（14 = 不集中）")
        a.set_xticks(hots)
        a.grid(alpha=.3)
    ax[0].set_title("TTFT P95")
    ax[1].set_title("热节点出口负载")
    ax[2].set_title("副本占池")
    h0, l0 = ax[0].get_legend_handles_labels()
    fg.legend(h0, l0, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=4, fontsize=8)
    fg.suptitle(f"热点复制增益场景【注入】：热集合 = {sx:g}× 集群 HBM，全网池读需求 ≈ 出口容量的 20%（标定目标 {DEMAND:.0%}）",
                y=1.13)
    fg.tight_layout()
    fg.savefig(os.path.join(HFIG, "hotspot_scan.png"), dpi=150, bbox_inches="tight")
    plt.close(fg)


def md(data):
    if not data:
        return ""
    sets = sorted({r["set_x"] for r in data})
    rate = data[0]["rate_full"]
    s = ["\n## 热点复制增益场景【注入】\n",
         f"【注入】合成负载，不改原有 trace。每个会话从共享长文档集合里均匀选一篇（{DOC_PAGES} 页 = 128K token，"
         "跨会话共享、不加盐），之后 1–4 轮问答。热集合取集群 HBM 的 0.5× 与 2×：2× 时热文档在 HBM 里互相挤出，"
         "读真正落到池上。写本机放置下，热文档主副本固定在 n_hot 个节点（等价于这几台节点上的加载器写入）；"
         "扁平哈希池按键条带化，不受影响。到达率在扁平池上标定（"
         f"{rate * 3600:.0f} 会话/h 全规模），全网池读需求约为全网 RoCE 出口容量的 20%，"
         "所以 n_hot 小时热节点出口需求 > 容量，全网总需求 < 总容量。RoCE、链路 FCFS 排队、14 TB 池、"
         f"切片 1/32、回放窗口 3 h（前 1 h 预热；截止时间裁剪）。\n",
         "“净收益”判定：复制变体与同放置、同路由、不复制的对照比 TTFT P95（写本机 + 热点复制 / 被动副本 对 写本机；"
         "KVCM 热页定向副本 对 只路由（KVCM 保留））。\n"]
    for sx in sets:
        hots = sorted({r["n_hot"] for r in data if r["variant"] != "flat" and r["set_x"] == sx})
        n_docs = next(r["n_docs"] for r in data if r["set_x"] == sx)
        s.append(f"\n### 热集合 {sx:g}× 集群 HBM（{n_docs} 篇文档 / 切片）\n")
        s.append("| 持有热文档节点数 | 变体 | 命中率 | prefill NPU | TTFT 均值 (s) | TTFT P95 (s) | 热节点出口负载 | "
                 "全网出口负载 | 读池请求占比 | 复制流量 GB/s | 副本占池 |")
        s.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for h in hots:
            for v in HS_VARS:
                r = _get(data, v, h, sx)
                if not r:
                    continue
                s.append(f"| {h} | {NAMES[v]} | {r['hit_rate']:.2%} | {r['npu_busy']:.1f} | {_f(r['ttft_mean'])} | "
                         f"{_f(r['ttft_p95'])} | {r['hot_egress_util']:.0%} | {r['net_demand']:.0%} | "
                         f"{r['pool_req_frac']:.0%} | {r['copy_gb_per_s']:.1f} | {r['replica_share']:.1%} |")
        s.append("\n复制相对不复制对照的 TTFT P95 变化（负 = 收益）：\n")
        s.append("| 持有热文档节点数 | " + " | ".join(f"{NAMES[a]} 对 {NAMES[b]}" for a, b in PAIRS) + " |")
        s.append("|---:|" + "---:|" * len(PAIRS))
        for h in hots:
            row = []
            for a, b in PAIRS:
                ra, rb = _get(data, a, h, sx), _get(data, b, h, sx)
                row.append(f"{ra['ttft_p95'] / rb['ttft_p95'] - 1:+.0%}" if ra and rb else "—")
            s.append(f"| {h} | " + " | ".join(row) + " |")
    s.append("\n![热点复制扫描](figures/locality/hotspot/hotspot_scan.png)\n")
    return "\n".join(s) + "\n"


def build():
    data = _load()
    if not data:
        return ""
    fig(data)
    return [md(data)]
