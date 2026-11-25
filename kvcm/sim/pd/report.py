"""PD 实验的图与 summary.md。"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .pd_params import DEFAULT as Q  # noqa: E402

for fam in ("Noto Sans CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "SimHei", "Droid Sans Fallback"):
    try:
        from matplotlib import font_manager
        font_manager.findfont(fam, fallback_to_default=False)
        plt.rcParams["font.family"] = fam
        break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False

POL = [
    ("static-mixed", "静态全混部", "#888888"),
    ("static-pd", "静态最优 P:D", "#1f77b4"),
    ("static-best3", "静态最优 P/D/混部", "#17becf"),
    ("dyn-pd-oracle@ideal", "动态 P:D（理想·零代价）", "#d62728"),
    ("dyn-3-oracle@ideal", "动态三态（理想·零代价）", "#9467bd"),
]
TIER_ORDER = ["L-pool", "L-d2d", "L-drain", "S-pool", "S-drain"]
TIER_SHORT = {
    "L-pool": "逻辑切换\n+KVCM 池",
    "L-d2d": "逻辑切换\n+点对点迁移",
    "L-drain": "逻辑切换\n+排空",
    "S-pool": "sleep/wake\n+KVCM 池",
    "S-drain": "sleep/wake\n+排空",
}


S5_ROWS = [
    ("static-pd", "静态最优 P:D"),
    ("static-best3", "静态最优 P/D/混部"),
    ("L-pool|oracle", "逻辑+池 · oracle"),
    ("L-pool|lag", "逻辑+池 · 滞后"),
    ("L-d2d|oracle", "逻辑+点对点 · oracle"),
    ("L-d2d|lag", "逻辑+点对点 · 滞后"),
    ("L-drain|oracle", "逻辑+排空 · oracle"),
    ("L-drain|lag", "逻辑+排空 · 滞后"),
    ("S-pool|oracle", "sleep/wake+池 · oracle"),
    ("S-pool|lag", "sleep/wake+池 · 滞后"),
    ("S-drain|oracle", "sleep/wake+排空 · oracle"),
    ("S-drain|lag", "sleep/wake+排空 · 滞后"),
]


def figures(out, series, fig_dir):
    os.makedirs(fig_dir, exist_ok=True)
    paths = []
    scs = list(out["scenarios"])

    # 1. 需求序列
    fig, axes = plt.subplots(len(scs), 1, figsize=(10, 2.3 * len(scs)), sharex=True)
    for ax, name in zip(axes, scs):
        dm, nP, nD, share = series[name]
        h = dm.t / 3600
        ax.stackplot(h, nP, nD, labels=["P 需求（节点）", "D 需求（节点）"], colors=["#ff9f40", "#4c9be8"], alpha=0.8)
        ax.axhline(Q.n_nodes, color="k", lw=0.8, ls="--")
        ax2 = ax.twinx()
        ax2.plot(h, share, color="#c00", lw=1.2, label="P 占比")
        ax2.set_ylim(0, 1)
        ax2.set_ylabel("P 占比", color="#c00")
        ax.set_ylabel("节点")
        ax.set_title(out["scenarios"][name]["label"], fontsize=9, loc="left")
        if name == scs[0]:
            ax.legend(loc="upper left", fontsize=8)
    axes[-1].set_xlabel("小时（测量段，参考负载 α=1）")
    fig.tight_layout()
    p = os.path.join(fig_dir, "pd_demand.png")
    fig.savefig(p, dpi=130)
    plt.close(fig)
    paths.append(p)

    # 2. goodput：基线与理想动态
    fig, ax = plt.subplots(figsize=(10, 4))
    x = np.arange(len(scs))
    w = 0.8 / (len(POL) + 1)
    for i, (k, lab, c) in enumerate(POL + [("L-pool", "动态 P:D（滞后一窗·逻辑切换+池）", "#ff7f0e")]):
        vals = []
        for s in scs:
            r = out["scenarios"][s]
            v = r["tiers"]["L-pool"]["dyn-pd-lag"] if k == "L-pool" else r["policies"][k]
            vals.append(v / r["policies"]["static-pd"])
        ax.bar(x + (i - len(POL) / 2) * w, vals, w, label=lab, color=c)
    ax.axhline(1, color="k", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(scs)
    ax.set_ylabel("goodput / 静态最优 P:D")
    ax.set_title("goodput（过载窗口 ≤1% 的最大负载倍数），相对静态最优 P:D", fontsize=10)
    ax.legend(fontsize=7, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.1))
    fig.set_size_inches(10, 4.8)
    fig.tight_layout()
    p = os.path.join(fig_dir, "pd_goodput.png")
    fig.savefig(p, dpi=130)
    plt.close(fig)
    paths.append(p)

    # 3. 切换代价分档
    fig, ax = plt.subplots(figsize=(10, 4))
    w = 0.8 / len(scs)
    for i, s in enumerate(scs):
        r = out["scenarios"][s]
        vals = [r["tiers"][t]["dyn-pd-lag"] / r["policies"]["static-pd"] for t in TIER_ORDER]
        ax.bar(np.arange(len(TIER_ORDER)) + (i - len(scs) / 2 + 0.5) * w, vals, w, label=s)
    ax.axhline(1, color="k", lw=0.8)
    ax.set_xticks(np.arange(len(TIER_ORDER)))
    ax.set_xticklabels([TIER_SHORT[t] for t in TIER_ORDER], fontsize=8)
    ax.set_ylabel("goodput / 静态最优 P:D")
    ax.set_title("动态 P:D（滞后一窗控制器）在各切换代价档下的 goodput", fontsize=10)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = os.path.join(fig_dir, "pd_tiers.png")
    fig.savefig(p, dpi=130)
    plt.close(fig)
    paths.append(p)

    # S5：阶跃时间线 + 过载栅格；逐次阶跃过载分钟
    if "s5_steps" in out and "S5" in series:
        st = out["s5_steps"]
        dm, nP, nD, share = series["S5"]
        h = dm.t / 3600
        keys = [k for k, _ in S5_ROWS]
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [1, 1.6]})
        a1.stackplot(h, nP, nD, labels=["P 需求", "D 需求"], colors=["#ff9f40", "#4c9be8"], alpha=0.8)
        a1.set_ylabel("节点（α=1）")
        b = a1.twinx()
        b.plot(h, share, color="#c00", lw=1.3)
        b.set_ylim(0, 1)
        b.set_ylabel("P 占比", color="#c00")
        for s in st["steps_h"]:
            a1.axvline(s, color="k", lw=0.6, ls=":")
        a1.legend(loc="lower left", fontsize=8)
        a1.set_title(out["scenarios"]["S5"]["label"], fontsize=9, loc="left")
        for i, k in enumerate(keys):
            ov = np.array(st["policies"][k]["ov"], bool)
            a2.scatter(h[ov], np.full(ov.sum(), i), marker="|", s=90, color="#d62728")
        for s in st["steps_h"]:
            a2.axvline(s, color="k", lw=0.6, ls=":")
        a2.set_yticks(range(len(keys)))
        a2.set_yticklabels([lab for _, lab in S5_ROWS], fontsize=8)
        a2.invert_yaxis()
        a2.set_xlabel("小时（测量段）")
        a2.set_title(f"过载窗口（红色竖线，5 分钟/格），共同负载 α*={st['alpha']:.3f}", fontsize=9, loc="left")
        fig.tight_layout()
        p = os.path.join(fig_dir, "pd_s5_timeline.png")
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)

        fig, ax = plt.subplots(figsize=(11, 4.2))
        nst = len(st["steps_h"])
        bars = [(k, lab) for k, lab in S5_ROWS if not k.endswith("|oracle")]
        cols = ["#1f77b4", "#17becf", "#ff7f0e", "#ffbb78", "#7f7f7f", "#9467bd", "#c5b0d5"]
        w = 0.8 / len(bars)
        for i, (k, lab) in enumerate(bars):
            vals = [r["over_min"] for r in st["policies"][k]["per_step"]]
            ax.bar(np.arange(nst) + (i - len(bars) / 2 + 0.5) * w, vals, w, label=lab, color=cols[i])
        ax.set_xticks(np.arange(nst))
        ax.set_xticklabels([f"{'↑' if j % 2 == 0 else '↓'} {s:.0f}h" for j, s in enumerate(st["steps_h"])])
        ax.set_ylabel("该段过载分钟")
        ax.set_title("S5 每次阶跃后（到下次阶跃前）的过载分钟（动态均为滞后控制器；oracle 各档几乎为 0，见表）；↑ = P 占比升高，↓ = 回落", fontsize=9)
        ax.legend(fontsize=8, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.12))
        fig.tight_layout()
        p = os.path.join(fig_dir, "pd_s5_steps.png")
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)

    # 4. E4：一次 D→P 切换
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    for ax, tr in zip(axes, ("cc256k", "qwen_coder")):
        e = out["e4"][tr]
        items = [("无池·排空", e["drain_s"]["p95"]), ("无池·点对点迁移", e["d2d_s"]["p95"]),
                 ("KVCM 池·刷尾页", e["pool_flush_s"]["p95"]), ("KVCM 池·接收方重读\n（节点·秒）", e["pool_reload_node_s"]["p95"])]
        ax.barh([a for a, _ in items], [max(b, 1e-4) for _, b in items], color=["#888", "#1f77b4", "#ff7f0e", "#ffbb78"])
        ax.set_xscale("log")
        for j, (_, b) in enumerate(items):
            ax.text(max(b, 1e-4) * 1.2, j, f"{b:.3g} s", va="center", fontsize=8)
        ax.set_title(f"{tr}：一次 D→P 切换（P95），节点 KV {e['kv_gb']['p50']:.0f} GB / {e['seqs']['p50']:.0f} 条", fontsize=9)
        ax.set_xlabel("秒（对数）")
    fig.tight_layout()
    p = os.path.join(fig_dir, "pd_switch_cost.png")
    fig.savefig(p, dpi=130)
    plt.close(fig)
    paths.append(p)
    return paths


def _pct(v):
    return f"{(v - 1) * 100:+.1f}%"


def summary(out, path):
    L = []
    a = L.append
    q = out["params"]
    a("# PD 转换仿真结果（E1 流体模型 + E4 切换代价，自动生成）\n")
    a("由 `PYTHONPATH=. python3 -m pd.run` 生成；方法与假设见 [`../../README.md`](../../README.md)「PD 转换」节与 "
      "[`../../params.md`](../../params.md)「PD 转换」节。全部为 **[trace 回放] + [解析]**，单次运行（seed=1），无真机校准。\n")
    a(f"口径：BF16 KV {q['kv_bytes_per_token']:.0f} B/token；HBM KV {q['hbm_kv_per_node'] / 1e9:.0f} GB/节点（gpu_memory_utilization 0.85）；"
      f"14 节点 × 16 NPU；decode 每步 {q['t_step'] * 1e3:.0f} ms、TPOT SLO {q['tpot_slo'] * 1e3:.0f} ms、KV 填充 ≤ {q['kv_fill']:.0%}；"
      f"P 节点利用率 ≤ {q['rho_p']:.0%}；混部节点 prefill 占比 ≤ {out['f_mixed']:.0%}；窗口 {q['window']:.0f} s；"
      f"goodput = 过载窗口 ≤ {q['overload_tol']:.0%} 的最大负载倍数 α（α=1 为参考负载，约 12 个节点的平均需求）。\n")
    a("**场景里带【注入】的，到达率包络或 trace 组合是人为设定的；会话内时间一律保持 trace 原值。**\n")

    a("## 1. 需求形态\n")
    a("| 场景 | 注入 | 平均需求（节点） | 峰值（节点） | 总量 CV | P 占比范围 | 闭式上界 s_max−s_min |")
    a("|---|---|---:|---:|---:|---|---:|")
    for k, r in out["scenarios"].items():
        a(f"| {k}：{r['label']} | {'是' if r['injected'] else '否'} | {r['mean_nodes']:.2f} | {r['peak_nodes']:.2f} | "
          f"{r['total_cv']:.1%} | {r['p_share_min']:.2f}–{r['p_share_max']:.2f} | {r['closed_form_overprov']:.2f} |")
    a("\n每会话平均需求（节点·秒）：" + "；".join(
        f"{k} P {v[0]:.3g} / D {v[1]:.3g}（P 占 {v[0] / (v[0] + v[1]):.0%}）" for k, v in out["d_node_per_session"].items()) + "\n")
    a("![pd_demand.png](figures/pd_demand.png)\n")

    a("## 2. goodput：基线 vs 动态（切换零代价的理想上界）\n")
    a("| 场景 | 静态全混部 | 静态最优 P:D | 静态最优 P/D/混部 | 动态 P:D 理想 | 动态三态理想 | 动态 P:D 滞后一窗（逻辑切换+池） |")
    a("|---|---:|---:|---:|---:|---:|---:|")
    for k, r in out["scenarios"].items():
        p = r["policies"]
        b = p["static-pd"]
        lag = r["tiers"]["L-pool"]["dyn-pd-lag"]
        a(f"| {k} | {p['static-mixed']:.3f}（{_pct(p['static-mixed'] / b)}） | {b:.3f} | {p['static-best3']:.3f}（{_pct(p['static-best3'] / b)}） | "
          f"{p['dyn-pd-oracle@ideal']:.3f}（{_pct(p['dyn-pd-oracle@ideal'] / b)}） | {p['dyn-3-oracle@ideal']:.3f}（{_pct(p['dyn-3-oracle@ideal'] / b)}） | "
          f"{lag:.3f}（{_pct(lag / b)}） |")
    a("\n括号内为相对静态最优 P:D。静态最优组合（P, D, 混部 节点数）：" + "；".join(
        f"{k} P:D={tuple(r['static_pd_alloc'][:2])}，三态={tuple(r['static_best3_alloc'])}" for k, r in out["scenarios"].items()) + "。\n")
    a("![pd_goodput.png](figures/pd_goodput.png)\n")

    a("## 3. 切换代价分档（动态 P:D）\n")
    a("| 档 | D→P 耗时 (s) | P→D 耗时 (s) | prefill 乘数 | " + " | ".join(f"{k} oracle / 滞后" for k in out["scenarios"]) + " |")
    a("|---|---:|---:|---:|" + "---:|" * len(out["scenarios"]))
    first = next(iter(out["scenarios"].values()))
    for t in TIER_ORDER:
        tr = first["tiers"][t]
        cells = []
        for k, r in out["scenarios"].items():
            x = r["tiers"][t]
            b = r["policies"]["static-pd"]
            cells.append(f"{_pct(x['dyn-pd-oracle'] / b)} / {_pct(x['dyn-pd-lag'] / b)}")
        pm = "按场景" if any(abs(r["tiers"][t]["p_mult_mean"] - 1) > 1e-9 for r in out["scenarios"].values()) else "1"
        a(f"| {tr['label']} | {tr['t_dp']:.1f} | {tr['t_pd']:.1f} | {pm} | " + " | ".join(cells) + " |")
    a("\n滞后控制器在其 goodput 处 24h 内的切换节点次数（逻辑+池档）：" + "；".join(
        f"{k} {r['tiers']['L-pool']['switches_at_lag_goodput']}" for k, r in out["scenarios"].items()) + "。")
    a("\n表中为相对静态最优 P:D 的 goodput 变化。sleep/wake 档的 prefill 乘数来自 host 权重副本挤占 DRAM 池后命中下降（见 §4）。\n")
    a("动态三态（P/D/混部，oracle）各档：\n")
    a("| 档 | " + " | ".join(out["scenarios"]) + " |")
    a("|---|" + "---:|" * len(out["scenarios"]))
    for t in TIER_ORDER:
        a(f"| {first['tiers'][t]['label']} | " + " | ".join(
            _pct(r["tiers"][t]["dyn-3-oracle"] / r["policies"]["static-pd"]) for r in out["scenarios"].values()) + " |")
    a("\n![pd_tiers.png](figures/pd_tiers.png)\n")

    if "s5_steps" in out:
        st = out["s5_steps"]
        r5 = out["scenarios"]["S5"]
        a("## 3a. S5 配比阶跃：每次阶跃后的过载（有池 vs 无池）\n")
        a(f"场景：{r5['label']}。实际 P 占比 {r5['p_share_min']:.2f}–{r5['p_share_max']:.2f}，总量 CV {r5['total_cv']:.1%}。"
          "CC 会话长（中位约 1.8 小时），到达率阶跃后 CC 需求要逐步衰减/累积，P 占比实际约 45–60 分钟完成大部分变化，不是几分钟；"
          "Qwen 到达率按 CC 实际需求补齐（同一 seed 先单跑 CC 求需求），保证总需求≈恒定。\n")
        a(f"统计口径：所有策略在同一负载 α* = 0.95 × 理想动态 P:D goodput = {st['alpha']:.3f} 下比较；"
          "“过载窗口”= 该 5 分钟窗口平均需求超过可用节点；每格为“该阶跃到下一次阶跃之间的过载窗口数 / 首段连续过载分钟”。\n")
        hdr = " | ".join(f"{'↑' if j % 2 == 0 else '↓'}{s:.0f}h" for j, s in enumerate(st["steps_h"]))
        a(f"| 策略 | 全天过载窗口 | 切换节点次数 | {hdr} |")
        a("|---|---:|---:|" + "---:|" * len(st["steps_h"]))
        for k, lab in S5_ROWS:
            v = st["policies"][k]
            cells = " | ".join(f"{r['over_windows']} / {r['first_run_min']:.0f}" for r in v["per_step"])
            a(f"| {lab} | {v['total_over_windows']} | {v['switches']} | {cells} |")
        a(f"| 静态全混部 | {st['policies']['static-mixed']['total_over_windows']} | 0 | " + " | ".join(
            f"{r['over_windows']} / {r['first_run_min']:.0f}" for r in st["policies"]["static-mixed"]["per_step"]) + " |")
        a("\n↑ = P 占比升高（D→P 方向切换，慢的那一侧）；↓ = 回落（P→D）。sleep/wake+池 档的过载里有一部分来自 host 权重副本挤占池导致的 prefill 增量（×1.03–1.13），不全是切换时延。\n")
        a("![pd_s5_timeline.png](figures/pd_s5_timeline.png)\n")
        a("![pd_s5_steps.png](figures/pd_s5_steps.png)\n")

    a("## 4. E4：一次 D→P 切换的代价（P95，满载 D 节点快照）\n")
    a("| trace | 节点 KV (GB) | 在途条数 | 无池·排空 (s) | 无池·点对点迁移 (s) | 无池·杀掉重算 (NPU·s) | KVCM 池·刷尾页 (s) | KVCM 池·接收方重读 (节点·s) | 单请求重读停顿 P95 (ms) |")
    a("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, e in out["e4"].items():
        a(f"| {k} | {e['kv_gb']['p50']:.0f} | {e['seqs']['p50']:.0f} | {e['drain_s']['p95']:.0f} | {e['d2d_s']['p95']:.2f} | "
          f"{e['recompute_npu_s']['p95']:.0f} | {e['pool_flush_s']['p95']:.4f} | {e['pool_reload_node_s']['p95']:.2f} | "
          f"{e['pool_reload_stall_s_p95']['p95'] * 1e3:.1f} |")
    a("\nhost 权重副本（510 GB/节点）从 DRAM 池扣除后的 prefill 乘数（读 PR #90 MRC，KVCM 现实 hook）：")
    for k, s in out["pool_shrink"].items():
        a(f"- {k}：池 1× → {s['f_after']:.2f}×，prefill 占用 {s['npu_busy_f1']:.1f} → {s['npu_busy_after']:.1f} NPU（×{s['mult']:.3f}）")
    a("\n![pd_switch_cost.png](figures/pd_switch_cost.png)\n")

    if out.get("sensitivity"):
        a("## 5. 敏感性（S2 / S4，相对同一变体下的静态最优 P:D）\n")
        a("| 场景 | 变体 | 静态最优 P:D（α） | 静态全混部 | 静态最优 P/D/混部 | 动态·逻辑+池 | 动态·逻辑+排空 | 动态·sleep/wake+池 |")
        a("|---|---|---:|---:|---:|---:|---:|---:|")
        for r in out["sensitivity"]:
            b = r["static-pd"]
            a(f"| {r['scenario']} | {r['variant']} | {b:.3f} | {_pct(r['static-mixed'] / b)} | {_pct(r['static-best3'] / b)} | "
              f"{_pct(r['lag@L-pool'] / b)} | {_pct(r['lag@L-drain'] / b)} | {_pct(r['lag@S-pool'] / b)} |")
        a("\n动态均为滞后一窗控制器。排空耗时随 decode 每步耗时同比缩放；E4 快照未随 HBM 变体重抽。\n")

    a("## 6. 简化与局限\n")
    for s in [
        "流体模型只看窗口平均（5 分钟），不排队，不出 TTFT/TPOT 分位；“过载窗口”代表该窗口平均需求超过可用节点，窗口内的突发与尾延迟没算。",
        "decode 按 HBM 容量约束建模（KV 占用 ≤ 296 GB × 90%），每步耗时 25 ms 是 roofline 假设；混部节点 TPOT = t_step/(1−f)，均为假设，A3 无实测。",
        "命中率取 trace 的无限容量上限 h*，各配置相同（HBM 前缀缓存与 decode KV 争用 HBM 的效应未建模）；只有 sleep/wake + 池档按 PR #90 MRC 计入池收缩。",
        "切换：oracle 已知未来需求，按切换耗时前瞻（组合要承得住切换完成前的需求峰值）；滞后控制器看上一窗需求 × 1.05。切换期间新角色不可用，排空档旧角色保留 50%。",
        "E4 是满载 D 节点的静态快照，不含接收方 HBM 余量与网络争用；点对点迁移和池重读都按 16 NPU 链路带宽线性叠加（乐观）。",
        "无池档的命中率沿用有池口径（实际无池时命中更低，对无池偏乐观）。",
        "Codex trace 时间线是合成的，未纳入；Qwen coder 会话很短，作为 prefill 偏重的一极用于配比注入。",
    ]:
        a(f"- {s}")
    open(path, "w").write("\n".join(L) + "\n")
