"""从 results/locality/*.json 生成局部性实验的图与 results/locality.md。

    PYTHONPATH=. python3 -m kvcm_sim.locality_report
"""

from __future__ import annotations

import json
import os

from .report import FIG, RES, TRACE_LABEL, plt  # noqa: F401  （复用中文字体设置）

LOC = os.path.join(RES, "locality")
LFIG = os.path.join(FIG, "locality")
TRACES = ["cc256k", "codex", "qwen_coder"]
TR_SHORT = {"cc256k": "Claude Code", "codex": "Codex", "qwen_coder": "Qwen coder"}
TP_LABEL = {"fabric": "FabricMem（本机 86 / 跨机 78 GB/s）", "roce": "RoCE（本机 47 / 跨机 23.8 GB/s）"}
COST_LABEL = {"flat": "扁平（一律跨机带宽、无争用）", "topo": "拓扑（分本机/跨机）", "contend": "拓扑 + 网卡争用"}
VAR_LABEL = {
    "flat": "扁平共享池（哈希条带、单副本）",
    "local": "+ 写本机放置",
    "local+passive": "+ 写本机 + 被动副本（Mooncake）",
    "hash+hot": "哈希 + 热点复制两选一（TokenLake）",
    "kvcm": "KVCM（现实 hook，哈希放置）",
    "kvcm+loc": "KVCM + 局部性（写本机 + 热页定向副本 + TTFT 路由）",
    "kvcm-oracle+loc": "KVCM 理想 hook + 局部性",
    "local+load": "写本机 + 最短队列路由",
    "local+ttft": "写本机 + 预测 TTFT 路由",
}
E2_VARS = ["flat", "local", "local+passive", "hash+hot", "kvcm", "kvcm+loc", "kvcm-oracle+loc"]
E4_VARS = ["local", "local+load", "local+ttft"]
E3_VARS = ["flat", "hash+hot", "kvcm+loc"]
COLORS = ["#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#ff7f0e", "#d62728", "#7f0000"]
FOOT = ("[trace 回放 + 离散事件] DeepSeek-V4.1（Flash）BF16 KV 3,200 B/token · 14 实例 × 16 NPU，每实例 16 个 prefill 槽 · "
        "切片 1/32（时长 ×32、带宽 ÷32，时间量还原为全规模秒）· 单次运行")


def _load(t):
    p = os.path.join(LOC, f"{t}.json")
    return json.load(open(p)) if os.path.exists(p) else []


def _sel(runs, **kw):
    out = [r for r in runs if all(r.get(k) == v for k, v in kw.items())]
    return out[-1] if out else None


def _ms(x):
    return f"{x * 1000:.1f}"


def _pct(x, d=2):
    return f"{x * 100:.{d}f}%"


def fig_e1(data):
    fig, axes = plt.subplots(2, 3, figsize=(15, 7.5))
    for c, t in enumerate(TRACES):
        runs = [r for r in data.get(t, []) if r["exp"] == "E1"]
        for row, (key, ylab) in enumerate((("exposed_mean", "未掩盖的池读时间 均值（ms）"),
                                            ("des_ttft_pool_p95", "读池请求 TTFT P95（s）"))):
            ax = axes[row][c]
            w = 0.2
            for j, (tp, ov) in enumerate((("fabric", False), ("fabric", True), ("roce", False), ("roce", True))):
                ys = []
                for cost in ("flat", "topo", "contend"):
                    r = _sel(runs, transport=tp, cost=cost, overlap=ov)
                    v = r[key] if r else 0
                    ys.append(v * 1000 if key == "exposed_mean" else v)
                ax.bar([x + (j - 1.5) * w for x in range(3)], ys, w,
                       label=f"{'FabricMem' if tp == 'fabric' else 'RoCE'} · {'与排队重叠' if ov else '不重叠'}",
                       color=["#1f77b4", "#aec7e8", "#d62728", "#ff9896"][j])
            ax.set_xticks(range(3))
            ax.set_xticklabels(["扁平", "拓扑", "拓扑+争用"])
            ax.set_ylabel(ylab)
            if row == 0:
                ax.set_title(TR_SHORT[t])
            ax.grid(axis="y", alpha=0.3)
    h, l = axes[0][0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=4, fontsize=9, bbox_to_anchor=(0.5, 0.955))
    fig.suptitle("E1 成本模型：扁平 vs 拓扑 vs 拓扑+网卡争用（扁平共享池，标称 14 TB）")
    fig.text(0.01, 0.005, FOOT, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.02, 1, 0.92))
    fig.savefig(os.path.join(LFIG, "e1_cost_model.png"), dpi=150)
    plt.close(fig)


def fig_e2(data):
    cases = (("fabric", 1.0), ("roce", 1.0), ("roce", 0.25))
    fig, axes = plt.subplots(3, 3, figsize=(16, 11))
    for c, t in enumerate(TRACES):
        runs = [r for r in data.get(t, []) if r["exp"] == "E2"]
        for row, (key, ylab) in enumerate((("des_ttft_pool_p95", "读池请求 TTFT P95（s）"),
                                            ("egress_max_over_mean", "出口字节 max/mean"),
                                            ("hit_rate", "命中率（%）"))):
            ax = axes[row][c]
            w = 0.12
            for j, v in enumerate(E2_VARS):
                ys = []
                for tp, f in cases:
                    r = _sel(runs, variant=v, transport=tp, f=f)
                    y = r[key] if r else 0
                    ys.append(y * 100 if key == "hit_rate" else y)
                ax.bar([x + (j - 3) * w for x in range(len(cases))], ys, w, color=COLORS[j],
                       label=VAR_LABEL[v] if (row == 0 and c == 0) else None)
            ax.set_xticks(range(len(cases)))
            ax.set_xticklabels(["FabricMem·14 TB", "RoCE·14 TB", "RoCE·3.5 TB"])
            ax.set_ylabel(ylab)
            if key == "hit_rate":
                vals = [r["hit_rate"] * 100 for r in runs]
                if vals:
                    ax.set_ylim(min(vals) - 0.3, max(vals) + 0.2)
            if key == "des_ttft_pool_p95":
                ax.set_yscale("log")
                ax.set_ylabel(ylab + "，对数轴")
            if row == 0:
                ax.set_title(TR_SHORT[t])
            ax.grid(axis="y", alpha=0.3)
    fig.legend(loc="upper center", ncol=4, fontsize=8, bbox_to_anchor=(0.5, 0.965))
    fig.suptitle("E2 放置与副本：单副本 vs 热点复制（拓扑 + 网卡争用，读不与排队重叠）")
    fig.text(0.01, 0.005, FOOT, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.02, 1, 0.92))
    fig.savefig(os.path.join(LFIG, "e2_placement_replication.png"), dpi=150)
    plt.close(fig)


def fig_e3(runs):
    if not runs:
        return
    alphas = sorted({r["alpha"] for r in runs})
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.6))
    for ax, (key, ylab) in zip(axes, (("des_ttft_pool_p95", "读池请求 TTFT P95（s）"),
                                      ("egress_util_max", "最忙节点出口利用率"),
                                      ("hit_rate", "命中率（%）"))):
        for j, v in enumerate(E3_VARS):
            for tp, ls in (("fabric", "--"), ("roce", "-")):
                ys = []
                for a in alphas:
                    r = _sel(runs, variant=v, transport=tp, alpha=a)
                    y = r[key] if r else float("nan")
                    ys.append(y * 100 if key == "hit_rate" else y)
                ax.plot(alphas, ys, ls, marker=["o", "x", "s"][j], ms=[9, 7, 5][j],
                        mfc="none" if j == 0 else None, color=[COLORS[0], COLORS[3], COLORS[5]][j],
                        label=f"{VAR_LABEL[v]} · {'FabricMem' if tp == 'fabric' else 'RoCE'}")
        ax.set_xlabel("Zipf α（文档热度倾斜）")
        ax.set_ylabel(ylab)
        ax.grid(alpha=0.3)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=3, fontsize=8, bbox_to_anchor=(0.5, 0.93))
    fig.text(0.5, 0.80, "扁平与热点复制两条线重合：热点复制从未触发", ha="center", fontsize=8, color="#555")
    fig.suptitle("E3 倾斜度扫描：合成“共享长文档”负载（文档 32K token，全规模 80,000 篇）")
    fig.text(0.01, 0.005, FOOT, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 0.78))
    fig.savefig(os.path.join(LFIG, "e3_skew_scan.png"), dpi=150)
    plt.close(fig)


def fig_e4(data):
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
    for ax, t in zip(axes, TRACES):
        runs = [r for r in data.get(t, []) if r["exp"] == "E4"]
        w = 0.25
        for j, v in enumerate(E4_VARS):
            ys = [(_sel(runs, variant=v, transport=tp) or {}).get("des_ttft_p95", 0) for tp in ("fabric", "roce")]
            ax.bar([x + (j - 1) * w for x in range(2)], ys, w, color=COLORS[j + 1], label=VAR_LABEL[v])
        ax.set_xticks(range(2))
        ax.set_xticklabels(["FabricMem", "RoCE"])
        ax.set_ylabel("全部请求 TTFT P95（s）")
        ax.set_title(TR_SHORT[t])
        ax.grid(axis="y", alpha=0.3)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=3, fontsize=9, bbox_to_anchor=(0.5, 0.93))
    fig.suptitle("E4 路由：主线规则 vs 最短队列 vs 预测 TTFT（写本机放置，标称池）")
    fig.text(0.01, 0.005, FOOT, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 0.86))
    fig.savefig(os.path.join(LFIG, "e4_routing.png"), dpi=150)
    plt.close(fig)


def fig_e5(data):
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
    for ax, t in zip(axes, TRACES):
        runs = [r for r in data.get(t, []) if r["exp"] == "E5"]
        for j, (tp, kvb) in enumerate((("fabric", 890.0), ("fabric", 3200.0), ("roce", 890.0), ("roce", 3200.0))):
            r = _sel(runs, transport=tp, kv_bytes=kvb)
            if not r or not r.get("extra_by_ctx"):
                continue
            xs = [f"{int(a) // 1024}K–" + ("∞" if b == float("inf") or b > 1e12 else f"{int(b) // 1024}K")
                  for a, b, *_ in r["extra_by_ctx"]]
            ys = [e[3] * 1000 for e in r["extra_by_ctx"]]
            ax.plot(range(len(xs)), ys, marker="o", ls="-" if tp == "roce" else "--",
                    color=["#1f77b4", "#d62728"][kvb > 1000],
                    label=f"{'FabricMem' if tp == 'fabric' else 'RoCE'} · {'BF16 3,200' if kvb > 1000 else 'FP4 890'} B/token")
            ax.set_xticks(range(len(xs)))
            ax.set_xticklabels(xs, fontsize=8)
        ax.set_xlabel("输入长度")
        ax.set_ylabel("跨机相对本机的额外 TTFT 均值（ms）")
        ax.set_title(TR_SHORT[t])
        ax.grid(alpha=0.3)
    h, l = axes[1].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=4, fontsize=9, bbox_to_anchor=(0.5, 0.93), handlelength=3)
    fig.suptitle("E5 跨机代价敏感度：按输入长度分桶（扁平共享池，拓扑无争用）")
    fig.text(0.01, 0.005, FOOT, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 0.86))
    fig.savefig(os.path.join(LFIG, "e5_cross_node_sensitivity.png"), dpi=150)
    plt.close(fig)


def md_tables(data, zipf):
    md = ["# KVCM 池局部性实验（自动生成）", "",
          "由 `python3 -m kvcm_sim.locality_report` 从 `results/locality/*.json` 生成；模型与参数见 "
          "[`../README.md`](../README.md)「池局部性扩展」节与 [`../params.md`](../params.md) §6。", "",
          "口径：共享 DRAM 池的块有所在节点；池→HBM 读本机走 H2D、跨机走 FabricMem 或 RoCE；"
          "“拓扑+争用”下每节点出口按 `B/(1+在途流数)` 共享；计入 TTFT 的读时间为 `max(0, τ − W)`（W = prefill 排队）；"
          "副本占池容量。TTFT 来自离散事件仿真（每实例 16 个 prefill 槽），与主线 summary 的无排队 TTFT 不可直接比较。", ""]
    md += ["## E1 成本模型：扁平 vs 拓扑 vs 争用（扁平共享池，标称 14 TB）", "",
           "| trace | 传输 | 成本模型 | 读与排队 | 读池请求占比 | 本机字节占比 | 未掩盖读 均值 (ms) | 未掩盖读占 TTFT | TTFT 均值 (s) | TTFT P95 (s) | 读池请求 TTFT P95 (s) | 最忙出口利用率 |",
           "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for t in TRACES:
        for tp in ("fabric", "roce"):
            for cost in ("flat", "topo", "contend"):
                for ov in (False, True):
                    r = _sel([x for x in data.get(t, []) if x["exp"] == "E1"], transport=tp, cost=cost, overlap=ov)
                    if not r:
                        continue
                    md.append(f"| {TR_SHORT[t]} | {'FabricMem' if tp == 'fabric' else 'RoCE'} | {COST_LABEL[cost]} | "
                              f"{'重叠' if ov else '不重叠'} | {_pct(r['pool_req_frac'], 1)} | {_pct(r['local_byte_share'], 0)} | "
                              f"{_ms(r['exposed_mean'])} | {_pct(r['exposed_share'], 2)} | {r['des_ttft_mean']:.3f} | "
                              f"{r['des_ttft_p95']:.3f} | {r['des_ttft_pool_p95']:.3f} | {_pct(r['egress_util_max'], 1)} |")
    md += ["", "## E2 放置与副本（拓扑 + 网卡争用，读不与排队重叠）", "",
           "相对扁平共享池的变化：ΔP95 为读池请求 TTFT P95 的相对变化；负数 = 更好。", ""]
    for t in TRACES:
        runs = [x for x in data.get(t, []) if x["exp"] == "E2"]
        if not runs:
            continue
        md += [f"### {TR_SHORT[t]}", "",
               "| 传输 · 池 | 变体 | 命中率 | 本机字节占比 | 副本占池 | 读池请求 TTFT P95 (s) | ΔP95 vs 扁平 | TTFT 均值 (s) | Δ均值 vs 扁平 | 出口 max/mean | 最忙出口利用率 | prefill 占用 NPU |",
               "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for tp, f in (("fabric", 1.0), ("roce", 1.0), ("roce", 0.25)):
            base = _sel(runs, variant="flat", transport=tp, f=f)
            for v in E2_VARS:
                r = _sel(runs, variant=v, transport=tp, f=f)
                if not r or not base:
                    continue
                d95 = r["des_ttft_pool_p95"] / max(base["des_ttft_pool_p95"], 1e-9) - 1
                dm = r["des_ttft_mean"] / max(base["des_ttft_mean"], 1e-9) - 1
                md.append(f"| {'FabricMem' if tp == 'fabric' else 'RoCE'} · {14 * f:g} TB | {VAR_LABEL[v]} | {_pct(r['hit_rate'])} | "
                          f"{_pct(r['local_byte_share'], 0)} | {_pct(r['replica_share'], 1)} | {r['des_ttft_pool_p95']:.3f} | "
                          f"{d95 * 100:+.1f}% | {r['des_ttft_mean']:.3f} | {dm * 100:+.1f}% | {r['egress_max_over_mean']:.2f} | "
                          f"{_pct(r['egress_util_max'], 1)} | {r['npu_busy']:.1f} |")
        md.append("")
    if zipf:
        md += ["## E3 倾斜度扫描（合成共享长文档负载，标称池）", "",
               "| α | 传输 | 变体 | 命中率 | 读池请求占比 | 读池请求 TTFT P95 (s) | ΔP95 vs 扁平 | 出口 max/mean | 最忙出口利用率 | 副本占池 |",
               "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for a in sorted({r["alpha"] for r in zipf}):
            for tp in ("fabric", "roce"):
                base = _sel(zipf, variant="flat", transport=tp, alpha=a)
                for v in E3_VARS:
                    r = _sel(zipf, variant=v, transport=tp, alpha=a)
                    if not r or not base:
                        continue
                    d95 = r["des_ttft_pool_p95"] / max(base["des_ttft_pool_p95"], 1e-9) - 1
                    md.append(f"| {a} | {'FabricMem' if tp == 'fabric' else 'RoCE'} | {VAR_LABEL[v]} | {_pct(r['hit_rate'])} | "
                              f"{_pct(r['pool_req_frac'], 1)} | {r['des_ttft_pool_p95']:.3f} | {d95 * 100:+.1f}% | "
                              f"{r['egress_max_over_mean']:.2f} | {_pct(r['egress_util_max'], 1)} | {_pct(r['replica_share'], 1)} |")
        md.append("")
    md += ["## E4 路由（写本机放置，标称池，拓扑 + 争用）", "",
           "| trace | 传输 | 路由 | 命中率 | lost reuse | 本机字节占比 | TTFT 均值 (s) | TTFT P95 (s) | 排队 P95 (s) |",
           "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for t in TRACES:
        for tp in ("fabric", "roce"):
            for v in E4_VARS:
                r = _sel([x for x in data.get(t, []) if x["exp"] == "E4"], variant=v, transport=tp)
                if r:
                    md.append(f"| {TR_SHORT[t]} | {'FabricMem' if tp == 'fabric' else 'RoCE'} | {VAR_LABEL[v]} | "
                              f"{_pct(r['hit_rate'])} | {_pct(r['lost_reuse'])} | {_pct(r['local_byte_share'], 0)} | "
                              f"{r['des_ttft_mean']:.3f} | {r['des_ttft_p95']:.3f} | {r['wait_p95']:.3f} |")
    md += ["", "## E5 跨机代价敏感度（扁平共享池，拓扑无争用）", "",
           "额外 TTFT = 跨机读字节 × (1/B_跨机 − 1/B_本机)，即同一请求的池读若全在本机能省下的时间。", "",
           "| trace | 传输 | KV 口径 | 输入长度 | 请求数 | 额外 TTFT 均值 (ms) | P95 (ms) | 该桶 TTFT 均值 (s) |",
           "|---|---|---|---|---:|---:|---:|---:|"]
    for t in TRACES:
        for tp in ("fabric", "roce"):
            for kvb in (3200.0, 890.0):
                r = _sel([x for x in data.get(t, []) if x["exp"] == "E5"], transport=tp, kv_bytes=kvb)
                if not r:
                    continue
                for a, b, n, mean, p95, tt in r.get("extra_by_ctx", []):
                    rng = f"{int(a) // 1024}K–" + ("∞" if b > 1e12 else f"{int(b) // 1024}K")
                    md.append(f"| {TR_SHORT[t]} | {'FabricMem' if tp == 'fabric' else 'RoCE'} | "
                              f"{'BF16 3,200' if kvb > 1000 else 'FP4 890'} B | {rng} | {n} | {mean * 1000:.2f} | "
                              f"{p95 * 1000:.2f} | {tt:.3f} |")
    md += ["", "## 图", ""]
    for f in ("e1_cost_model", "e2_placement_replication", "e3_skew_scan", "e4_routing", "e5_cross_node_sensitivity"):
        md.append(f"![{f}](figures/locality/{f}.png)")
    return md


def main():
    os.makedirs(LFIG, exist_ok=True)
    data = {t: _load(t) for t in TRACES}
    zipf = _load("zipf")
    fig_e1(data)
    fig_e2(data)
    fig_e3(zipf)
    fig_e4(data)
    fig_e5(data)
    md = md_tables(data, zipf)
    with open(os.path.join(RES, "locality.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print("wrote", os.path.join(RES, "locality.md"))


if __name__ == "__main__":
    main()
