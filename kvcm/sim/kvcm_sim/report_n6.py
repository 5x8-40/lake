"""6 节点保留收益主口径：`results/n6/` → `results/n6/summary.md` + `results/figures/n6/*.png`。

    PYTHONPATH=. python3 -m kvcm_sim.report_n6

主口径：每条 trace 分别标定到达率，使 6 TB 共享池 LRU 的实际 prefill NPU 占用 = 70%；切片 k=32，seed 1/2/3。
敏感性：同一锚点取 60%（`results/n6/u60/`，seed 1）；切片对照：k=16（seed 1，到达率同 k=32）。
`results/n6/ideal_anchor_coarse/` 是被弃用的“理想命中下占 70%”锚点的粗扫，只作记录。
"""

from __future__ import annotations

import glob
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker  # noqa: F401
import numpy as np

from .report import TRACE_LABEL as TRACE_NAMES

RES = os.path.join(os.path.dirname(__file__), "..", "results")
N6 = os.path.join(RES, "n6")
FIG = os.path.join(RES, "figures", "n6")
POLS = ("shared", "kvcm-real", "kvcm-oracle")
PNAME = {"shared": "共享池 LRU", "kvcm-real": "KVCM（现实 hook）", "kvcm-oracle": "KVCM（理想 hook）"}
COL = {"shared": "#1f77b4", "kvcm-real": "#ff7f0e", "kvcm-oracle": "#2ca02c"}
TRACES = ("cc256k", "codex", "qwen_coder")
N_NPU = 96
HBM_TB = 1.776
MAIN_K = 32
NOMINAL_TB = 6.0


def _load(sub=""):
    out = {}
    for t in TRACES:
        p = os.path.join(N6, sub, f"{t}.json")
        rows = [r for r in json.load(open(p)) if r["policy"] in POLS] if os.path.exists(p) else []
        uniq = {(r["policy"], r["pool_tb"], r.get("seed", 1), r["k"]): r for r in rows}
        out[t] = list(uniq.values())
    return out


def agg(rows, k):
    """(policy, pool_tb) → {metric: (mean, min, max, n)}。"""
    g = {}
    for r in rows:
        if r["k"] != k:
            continue
        g.setdefault((r["policy"], r["pool_tb"]), []).append(r)
    out = {}
    for key, rs in g.items():
        d = {}
        for m in ("hit_rate", "npu_busy", "npu_util", "ttft_p95", "ttft_mean", "hbm_hit_rate", "ideal_hit_rate"):
            v = np.array([x[m] for x in rs])
            d[m] = (float(v.mean()), float(v.min()), float(v.max()), len(v))
        d["seeds"] = sorted(x.get("seed", 1) for x in rs)
        out[key] = d
    return out


def _pools(a):
    return sorted({p for _, p in a})


def fig_trace(t, main, sens):
    rows = [(main, "70% NPU 标定（主口径，seed 均值，误差棒 = seed 最小–最大）")]
    if sens:
        rows.append((sens, "60% NPU 标定（敏感性，seed 1）"))
    fg, ax = plt.subplots(len(rows), 3, figsize=(16, 4.6 * len(rows)), squeeze=False)
    for i, (a, title) in enumerate(rows):
        pools = _pools(a)
        for p in POLS:
            xs = [x for x in pools if (p, x) in a]
            if not xs:
                continue
            for j, (m, sc) in enumerate((("hit_rate", 100), ("npu_busy", 1), ("ttft_p95", 1))):
                mu = np.array([a[(p, x)][m][0] for x in xs]) * sc
                lo = mu - np.array([a[(p, x)][m][1] for x in xs]) * sc
                hi = np.array([a[(p, x)][m][2] for x in xs]) * sc - mu
                ax[i][j].errorbar(xs, mu, yerr=[lo, hi], marker="o", capsize=3, color=COL[p], label=PNAME[p])
        ax[i][0].set_ylabel(f"{title}\n命中率（%）", fontsize=9)
        ax[i][1].set_ylabel("prefill 占用 NPU（全规模等效）")
        ax[i][1].axhline(N_NPU, color="r", ls=":", lw=1)
        ax[i][1].text(pools[0], N_NPU, " 集群 96 NPU（超过 = 算力过载）", color="r", fontsize=8, va="bottom")
        ax[i][2].set_ylabel("TTFT P95（s，分析式、不含排队）")
        for j in range(3):
            ax[i][j].set_xscale("log")
            ax[i][j].set_xticks(pools)
            ax[i][j].set_xticklabels([f"{x:g}" for x in pools])
            ax[i][j].xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
            ax[i][j].axvline(HBM_TB, color="gray", ls="--", lw=0.8)
            if pools[-1] > NOMINAL_TB:
                ax[i][j].axvspan(NOMINAL_TB * 1.15, pools[-1] * 1.2, color="gray", alpha=0.12)
            ax[i][j].set_xlabel("DRAM 池容量（TB）；灰虚线 = 集群 HBM 1.78 TB")
            ax[i][j].grid(alpha=.3)
    ax[0][0].legend(fontsize=8)
    if any(p > NOMINAL_TB for p in _pools(main)):
        ax[0][2].text(0.99, 0.98, "灰底 = 9 / 12 TB：超过 6 节点 × 1 TB 标称，\n只用于验证大池收敛", transform=ax[0][2].transAxes,
                      ha="right", va="top", fontsize=8, color="dimgray")
    fg.suptitle(f"{TRACE_NAMES.get(t, t)}：6 节点 × 16 die，HBM 296 GB/实例（共 1.78 TB），池容量扫描", y=1.0)
    fg.tight_layout()
    os.makedirs(FIG, exist_ok=True)
    fg.savefig(os.path.join(FIG, f"n6_{t}.png"), dpi=140, bbox_inches="tight")
    plt.close(fg)


def _f(v, d=2):
    return f"{v:.{d}f}"


def md_trace(t, a, label):
    pools = _pools(a)
    if not pools:
        return []
    s = [f"\n#### {label}\n",
         "| 池 TB | 策略 | 命中率 | 其中 HBM | prefill NPU | NPU 利用率 | TTFT P95 (s) | 命中率差（pt） | NPU 差 | P95 差 | seed |",
         "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for x in pools:
        b = a.get(("shared", x))
        for p in POLS:
            r = a.get((p, x))
            if not r:
                continue
            util = r["npu_util"][0]
            flag = "（过载）" if util > 1 else ""
            if p == "shared" or not b:
                dh = dn = dp = "—"
            else:
                dh = f"{(r['hit_rate'][0] - b['hit_rate'][0]) * 100:+.2f}"
                dn = f"{r['npu_busy'][0] / b['npu_busy'][0] - 1:+.1%}"
                dp = f"{r['ttft_p95'][0] / b['ttft_p95'][0] - 1:+.1%}"
            sp = r["hit_rate"]
            rng = f" ±{(sp[2] - sp[1]) * 50:.2f}" if sp[3] > 1 else ""
            s.append(f"| {x:g}{'（超标称）' if x > NOMINAL_TB else ''} | {PNAME[p]} | {sp[0]:.2%}{rng} | {r['hbm_hit_rate'][0]:.2%} | {r['npu_busy'][0]:.1f} | "
                     f"{util:.0%}{flag} | {_f(r['ttft_p95'][0])} | {dh} | {dn} | {dp} | {','.join(map(str, r['seeds']))} |")
    return s


def md_converge(a):
    s = ["\n#### 大池收敛检查（理想 hook / 现实 hook 相对共享池 LRU）\n",
         "| 池 TB | 理想 hook 命中率差（pt） | 理想 hook NPU 差 | 现实 hook 命中率差（pt） | 现实 hook NPU 差 |", "|---:|---:|---:|---:|---:|"]
    for x in [p for p in _pools(a) if p >= 3.5]:
        b = a.get(("shared", x))
        o, r = a.get(("kvcm-oracle", x)), a.get(("kvcm-real", x))
        if not (b and o and r):
            continue
        s.append(f"| {x:g}{'（超标称）' if x > NOMINAL_TB else ''} | {(o['hit_rate'][0] - b['hit_rate'][0]) * 100:+.2f} | "
                 f"{o['npu_busy'][0] / b['npu_busy'][0] - 1:+.1%} | {(r['hit_rate'][0] - b['hit_rate'][0]) * 100:+.2f} | "
                 f"{r['npu_busy'][0] / b['npu_busy'][0] - 1:+.1%} |")
    return s


def md_slice(t, rows):
    a32, a16 = agg(rows, 32), agg(rows, 16)
    keys = sorted(set(a16) & set(a32), key=lambda k: (k[1], POLS.index(k[0])))
    if not keys:
        return []
    s = [f"\n#### 切片对照（k=32 对 k=16，seed 1）\n", "| 池 TB | 策略 | 命中率 k=32 | 命中率 k=16 | 差（pt） | NPU k=32 | NPU k=16 |",
         "|---:|---|---:|---:|---:|---:|---:|"]
    for key in keys:
        r32 = [r for r in rows if r["k"] == 32 and r.get("seed", 1) == 1 and (r["policy"], r["pool_tb"]) == key]
        r16 = [r for r in rows if r["k"] == 16 and (r["policy"], r["pool_tb"]) == key]
        if not r32 or not r16:
            continue
        h32, h16 = r32[0]["hit_rate"], r16[0]["hit_rate"]
        s.append(f"| {key[1]:g} | {PNAME[key[0]]} | {h32:.2%} | {h16:.2%} | {(h16 - h32) * 100:+.2f} | "
                 f"{r32[0]['npu_busy']:.1f} | {r16[0]['npu_busy']:.1f} |")
    return s


def build():
    main, sens = _load(), _load("u60")
    cal = json.load(open(os.path.join(N6, "calibration.json")))
    old = json.load(open(os.path.join(RES, "calibration.json")))
    md = ["# KVCM 保留收益：6 节点主口径（自动生成）\n",
          "由 `python3 -m kvcm_sim.report_n6` 从 `results/n6/` 生成；参数见 [`../../params.md`](../../params.md) §9，"
          "方法与结论见 [`../../README.md`](../../README.md)「6 节点保留收益主口径」。14 节点结果保留在 "
          "[`../summary.md`](../summary.md) 作为基线。\n",
          "DeepSeek-V4.1-Flash，BF16 KV 3,200 B/token；Ascend A3 6 节点 × 16 die = 96 NPU，每实例 HBM 296 GB（共 1.78 TB），"
          "HBM 为引擎纯 LRU（各策略相同），KVCM 只管 DRAM 池。TTFT 为分析式（不含排队），NPU 利用率 > 100% 的格子标“过载”。\n",
          "## 到达率标定\n",
          "锚点：6 TB 共享池 LRU 的实际 prefill NPU 占用（迭代外推到相对误差 < 0.5%，seed 1）。比值相对 14 节点主线（理想命中下占 50%）的到达率。"
          "被弃用的“理想命中下占 70%”锚点（约 0.595×）会让 Claude Code 在 1.78 TB 需要 204% NPU，见 `ideal_anchor_coarse/`。\n",
          "| trace | 14 节点主线（会话/h） | 6 节点 70%（主口径） | 比值 | 6 节点 60%（敏感性） | 比值 |",
          "|---|---:|---:|---:|---:|---:|"]
    for t in TRACES:
        r14 = old[f"{t}@k32"]
        r70 = cal.get(f"{t}@k32@u0.7@lru")
        if not r70:
            continue
        sc = json.load(open(os.path.join(N6, "u60", "calibration.json"))) if os.path.exists(
            os.path.join(N6, "u60", "calibration.json")) else {}
        r60 = sc.get(f"{t}@k32@u0.6@lru")
        md.append(f"| {TRACE_NAMES.get(t, t)} | {r14 * 3600:,.0f} | {r70 * 3600:,.0f} | {r70 / r14:.3f} | "
                  + (f"{r60 * 3600:,.0f} | {r60 / r14:.3f} |" if r60 else "— | — |"))
    for t in TRACES:
        a = agg(main[t], MAIN_K)
        if not a:
            continue
        s1 = agg(sens[t], MAIN_K) if sens[t] else {}
        fig_trace(t, a, s1)
        md.append(f"\n## {TRACE_NAMES.get(t, t)}\n")
        md.append(f"![n6_{t}](../figures/n6/n6_{t}.png)\n")
        md += md_trace(t, a, "主口径：70% NPU 标定，k=32，seed 均值（± 为 seed 极差的一半）")
        md += md_converge(a)
        if s1:
            md += md_trace(t, s1, "敏感性：60% NPU 标定，k=32，seed 1")
        md += md_slice(t, main[t])
    with open(os.path.join(N6, "summary.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print("wrote", os.path.join(N6, "summary.md"))


if __name__ == "__main__":
    build()
