"""从 results/<trace>.json 生成图（PNG）与汇总表（results/summary.md）。

    PYTHONPATH=kvcm/sim python3 -m kvcm_sim.report
"""

from __future__ import annotations

import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .params import DEFAULT as P  # noqa: E402

RES = os.path.join(os.path.dirname(__file__), "..", "results")
FIG = os.path.join(RES, "figures")
TRACE_LABEL = {
    "cc256k": "AgentX Claude Code (cc-traces-weka-062126-256k)",
    "codex": "Codex × SWE-bench Pro (合成时间线)",
    "qwen_coder": "Qwen-Bailian coder",
}
POL_LABEL = {
    "local": "per-instance LRU",
    "shared": "shared-pool LRU",
    "kvcm-none": "KVCM（无 hook）",
    "kvcm-real": "KVCM（现实 hook）",
    "kvcm-oracle": "KVCM（理想 hook）",
    "kvcm-real-hint": "变体：KVCM（现实 hook）+ HBM 预取提示",
    "kvcm-oracle-hint": "变体：KVCM（理想 hook）+ HBM 预取提示",
}
POL_STYLE = {
    "local": dict(color="#888888", marker="o"),
    "shared": dict(color="#1f77b4", marker="s"),
    "kvcm-real": dict(color="#ff7f0e", marker="^"),
    "kvcm-oracle": dict(color="#d62728", marker="D"),
    "kvcm-oracle-hint": dict(color="#9467bd", marker="v", ls="--"),
}
MAIN_POLS = ["local", "shared", "kvcm-real", "kvcm-oracle"]
NOMINAL_TB = P.dram_kv_total / 1e12
HBM_TB = P.hbm_kv_per_inst * P.n_inst / 1e12
MODEL_NOTE = (f"HBM 归推理引擎：每实例 {P.hbm_kv_per_inst / 1e9:.0f} GB 前缀缓存、纯 LRU，所有策略相同且不随 x 轴变化；"
              f"KVCM 只管 DRAM 池（停顿期池保活 + 零引用优先）。“HBM 预取提示”是单列变体"
              f"（kvcm/design/kv-prefetch.md 方案 A：预取请求把池里前缀载入引擎 HBM，进 LRU、不钉）。")

for fam in ("Noto Sans CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "SimHei", "Droid Sans Fallback"):
    try:
        from matplotlib import font_manager
        font_manager.findfont(fam, fallback_to_default=False)
        plt.rcParams["font.family"] = fam
        break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False


def _load(trace):
    p = os.path.join(RES, f"{trace}.json")
    return json.load(open(p)) if os.path.exists(p) else []


def _get(runs, pol, f):
    for r in runs:
        if r["policy"] == pol and abs(r["f"] - f) < 1e-9:
            return r
    return None


def fig_mrc(trace, runs):
    fs = sorted({r["f"] for r in runs if r["policy"] in POL_STYLE})
    if not fs:
        return None
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 5.0))
    ideal = None
    for pol, sty in POL_STYLE.items():
        pts = [(f * NOMINAL_TB, _get(runs, pol, f)) for f in fs]
        pts = [(x, r) for x, r in pts if r]
        if not pts:
            continue
        xs = [x for x, _ in pts]
        ax.plot(xs, [100 * r["hit_rate"] for _, r in pts], label=POL_LABEL[pol], **sty)
        ax2.plot(xs, [max(1e-3, 100 * r["lost_reuse"]) for _, r in pts], label=POL_LABEL[pol], **sty)
        ideal = pts[-1][1]["ideal_hit_rate"]
    if ideal is not None:
        ax.axhline(100 * ideal, ls="--", color="k", lw=1, label=f"理想 h*（无限容量全局共享）{100 * ideal:.1f}%")
    for a_ in (ax, ax2):
        a_.axvline(NOMINAL_TB, ls=":", color="#2ca02c", lw=1.2)
        a_.set_xscale("log", base=2)
        a_.set_xlabel(f"DRAM 池容量（TB，全集群；HBM 固定 {HBM_TB:.2f} TB 不扫）")
        a_.grid(alpha=0.3, which="both")
    for a_ in (ax, ax2):
        a_.text(NOMINAL_TB, 0.03, f"标称池 {NOMINAL_TB:.0f} TB ", color="#2ca02c", fontsize=8, ha="right",
                transform=a_.get_xaxis_transform())
    ax.set_ylabel("token 命中率（%）")
    ax.set_title("命中率–容量", fontsize=10)
    h_, l_ = ax.get_legend_handles_labels()
    fig.legend(h_, l_, fontsize=7.5, loc="lower center", ncol=3, bbox_to_anchor=(0.5, 0.04), frameon=False)
    ax2.set_yscale("log")
    ax2.set_ylabel("lost KV reuse = 1 − h/h*（%，对数）")
    ax2.set_title("相对理想命中的损失", fontsize=10)
    fig.suptitle(TRACE_LABEL.get(trace, trace), fontsize=10)
    fig.text(0.01, 0.01, f"[trace 回放] DeepSeek V4.1（假设同 V4-Pro 布局，BF16）· 昇腾 A3 224 NPU · HBM = 引擎 LRU "
             f"{P.hbm_kv_per_inst / 1e9:.0f} GB/实例 × {P.n_inst}，各策略相同 · KVCM 只管池 · 单次运行",
             fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.13, 1, 0.95))
    p = os.path.join(FIG, f"mrc_{trace}.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def fig_lost(trace, runs, fs=(1 / 8, 1.0)):
    pols = MAIN_POLS + ["kvcm-oracle-hint"]
    fig, axes = plt.subplots(1, len(fs), figsize=(10, 4.0), sharey=True)
    for ax, f in zip(axes, fs):
        vals = []
        for pol in pols:
            r = _get(runs, pol, f)
            vals.append(100 * r["lost_reuse"] if r else 0)
        bars = ax.bar([POL_LABEL[p] for p in pols], vals, color=[POL_STYLE[p]["color"] for p in pols])
        bars[-1].set_hatch("//")
        bars[-1].set_alpha(0.6)
        r = _get(runs, "local", f)
        if r and "any_hit_rate" in r:
            # per-instance：损失拆成“集群里有但本实例看不见”与“已被驱逐”
            invis = 100 * (r["any_hit_rate"] - r["hit_rate"]) / r["ideal_hit_rate"]
            ax.bar([POL_LABEL["local"]], [invis], color="#444444", label="其中：集群内有但本实例不可见")
            ax.legend(fontsize=7)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.1f}%", ha="center", va="bottom", fontsize=8)
        ax.set_title(f"DRAM 池 = {f:g} × 标称（{f * NOMINAL_TB:.2f} TB）", fontsize=9)
        ax.tick_params(axis="x", labelsize=6.5, rotation=18)
        ax.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("lost KV reuse = 1 − h/h*（%）")
    fig.suptitle(f"相对理想命中的复用损失 · {TRACE_LABEL.get(trace, trace)}", fontsize=10)
    fig.text(0.01, 0.01, "[trace 回放] HBM = 引擎 LRU（各策略相同）· 斜线柱 = 变体（HBM 预取提示）· 单次运行",
             fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    p = os.path.join(FIG, f"lost_{trace}.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


ABL_CHAIN = {
    "realistic": ["local", "shared", "real+ka", "kvcm-real", "kvcm-real-hint"],
    "oracle": ["local", "shared", "oracle+ka", "kvcm-oracle", "kvcm-oracle-hint"],
}
ABL_LABEL = ["per-instance\nLRU", "+ 共享池/\n全局视图", "+ 池保活", "+ 零引用优先\n（= KVCM）", "+ HBM 预取提示\n（变体）"]


def fig_ablation(trace, runs, fs=(1 / 8, 1.0)):
    fig, axes = plt.subplots(1, len(fs), figsize=(9.5, 3.9), sharey=False)
    for ax, f in zip(axes, fs):
        w = 0.38
        for j, (hook, chain) in enumerate(ABL_CHAIN.items()):
            vals = [(_get(runs, p, f) or {}).get("hit_rate", float("nan")) * 100 for p in chain]
            xs = [i + (j - 0.5) * w for i in range(len(chain))]
            bars = ax.bar(xs, vals, width=w, label=f"{'现实' if hook == 'realistic' else '理想'} hook",
                          color="#ff7f0e" if hook == "realistic" else "#d62728", alpha=0.85)
            bars[-1].set_hatch("//")
            bars[-1].set_alpha(0.5)
            for x, v in zip(xs, vals):
                ax.text(x, v, f"{v:.1f}", ha="center", va="bottom", fontsize=6.5)
        ideal = (_get(runs, "shared", f) or {}).get("ideal_hit_rate")
        if ideal:
            ax.axhline(100 * ideal, ls="--", color="k", lw=1)
        lo = min((_get(runs, "local", f) or {}).get("hit_rate", 1), (_get(runs, "real+ka", f) or {}).get("hit_rate", 1))
        ax.set_ylim(100 * lo - 2, 100 * (ideal or 1) + 1)
        ax.set_xticks(range(5))
        ax.set_xticklabels(ABL_LABEL, fontsize=7, rotation=0)
        ax.set_title(f"DRAM 池 = {f:g} × 标称（{f * NOMINAL_TB:.2f} TB）", fontsize=9)
        ax.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("token 命中率（%）")
    axes[0].legend(fontsize=7)
    fig.suptitle(f"消融：逐项打开 KVCM 机制（累计）· {TRACE_LABEL.get(trace, trace)}", fontsize=10)
    fig.text(0.01, 0.01, "[trace 回放] 虚线 = 理想 h* · HBM = 引擎 LRU（各策略相同）· 斜线柱 = 变体，不属于 KVCM 主线 · 单次运行",
             fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 0.94))
    p = os.path.join(FIG, f"ablation_{trace}.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def table_ttft(trace, runs, f):
    rows = []
    base = _get(runs, "local", f)
    sh = _get(runs, "shared", f)
    for pol in ["local", "shared", "kvcm-none", "kvcm-real", "kvcm-oracle", "kvcm-real-hint", "kvcm-oracle-hint"]:
        r = _get(runs, pol, f)
        if not r:
            continue
        sv_l = base["npu_busy"] - r["npu_busy"] if base else float("nan")
        sv_s = sh["npu_busy"] - r["npu_busy"] if sh else float("nan")
        rows.append(f"| {POL_LABEL[pol]} | {100 * r['hit_rate']:.2f}% | {100 * r['hbm_hit_rate']:.2f}% | "
                    f"{r['ttft_mean']:.2f} | {r['ttft_p50']:.2f} | {r['ttft_p95']:.2f} | {r['ttft_roce_p95']:.2f} | "
                    f"{r['npu_busy']:.1f} | {sv_l:+.1f} | {sv_s:+.1f} |")
    head = ("| 策略 | 命中率 | 其中 HBM | TTFT 均值 (s) | P50 | P95 | P95（池走 ROCE） | prefill 占用 NPU | "
            "相对 per-instance 省 | 相对 shared-pool 省 |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    return head + "\n" + "\n".join(rows)


def table_gap(trace, runs, f):
    pols = MAIN_POLS + ["kvcm-oracle-hint"]
    labs = None
    lines = []
    for pol in pols:
        r = _get(runs, pol, f)
        if not r:
            continue
        b = r["gap_buckets"]
        labs = list(b.keys())
        cells = []
        for lab in labs:
            ideal, hit, cnt = b[lab]
            cells.append(f"{100 * (1 - hit / ideal):.1f}%" if ideal > 0 else "—")
        lines.append(f"| {POL_LABEL[pol]} | " + " | ".join(cells) + " |")
    if not labs:
        return ""
    cnts = _get(runs, "local", f)["gap_buckets"]
    tot = sum(v[2] for v in cnts.values())
    head = "| 策略 \\ 前一停顿 | " + " | ".join(f"{lab}（{100 * cnts[lab][2] / tot:.0f}% 请求）" for lab in labs) + " |"
    return head + "\n|---|" + "---:|" * len(labs) + "\n" + "\n".join(lines)


def main():
    os.makedirs(FIG, exist_ok=True)
    md = ["# KVCM trace 回放结果（自动生成）", "",
          "由 `python3 -m kvcm_sim.report` 从 `results/*.json` 生成；参数与假设见 [`../params.md`](../params.md)，方法见 [`../README.md`](../README.md)。",
          "全部为 **[trace 回放] + [解析]**，每个点单次运行（seed=1），无真机校准。", "",
          f"模型：{MODEL_NOTE}x 轴与表中“池容量”只指 DRAM 池（标称 {NOMINAL_TB:.0f} TB = 14 节点 × 1 TB）。", ""]
    cal = json.load(open(os.path.join(RES, "calibration.json"))) if os.path.exists(os.path.join(RES, "calibration.json")) else {}
    for trace in ("cc256k", "codex", "qwen_coder"):
        runs = _load(trace)
        if not runs:
            continue
        k = runs[0]["k"]
        md += [f"## {TRACE_LABEL[trace]}", ""]
        rate = cal.get(f"{trace}@k{k}")
        if rate:
            r1 = _get(runs, "shared", 1.0)
            md.append(f"- 负载：全规模 {rate * 3600:.0f} 会话/h"
                      + (f"，{r1['requests'] / (36000 - 10800) * k:.1f} req/s，理想命中 h* = {100 * r1['ideal_hit_rate']:.2f}%"
                         if r1 else "") + f"；仿真切片 1/{k}。")
        md.append("")
        for fn in (fig_mrc, fig_lost, fig_ablation):
            p = fn(trace, runs)
            if p:
                md.append(f"![{os.path.basename(p)}](figures/{os.path.basename(p)})")
        md.append("")
        md += ["### 命中率–容量（MRC）数据", "",
               "| 池容量 × 标称 | 池 TB | " + " | ".join(POL_LABEL[p] for p in POL_STYLE) + " | 理想 h* |",
               "|---:|---:|" + "---:|" * (len(POL_STYLE) + 1)]
        for f in sorted({r["f"] for r in runs}):
            cells = []
            for p in POL_STYLE:
                r = _get(runs, p, f)
                cells.append(f"{100 * r['hit_rate']:.2f}%" if r else "—")
            ideal = next((r["ideal_hit_rate"] for r in runs if abs(r["f"] - f) < 1e-9), None)
            md.append(f"| {f:g} | {f * NOMINAL_TB:.2f} | " + " | ".join(cells) + f" | {100 * ideal:.2f}% |")
        md.append("")
        for f in (1 / 8, 1 / 4, 1.0):
            if not _get(runs, "local", f):
                continue
            md += [f"### TTFT 与等效 NPU（DRAM 池 = {f:g} × 标称 = {f * NOMINAL_TB:.2f} TB）", "",
                   "A3 TTFT = 2 × 950 拟合 TTFT + 未掩盖 KV 加载；NPU 数为全规模等效的 prefill 平均占用（D8a §8.2）。", "",
                   table_ttft(trace, runs, f), "",
                   f"按前一停顿长度分组的复用损失（1 − h/h*，DRAM 池 = {f:g} × 标称）：", "",
                   table_gap(trace, runs, f), ""]
    open(os.path.join(RES, "summary.md"), "w").write("\n".join(md) + "\n")
    print("wrote", os.path.join(RES, "summary.md"))


if __name__ == "__main__":
    main()
