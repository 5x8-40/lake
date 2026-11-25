"""跑 PD 转换实验（E1 + E4），写 results/pd/*.json、图与 results/pd/summary.md。

    cd kvcm/sim && PYTHONPATH=. python3 -m pd.run [--fig-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time

import numpy as np

from . import fluid as F
from .pd_params import DEFAULT as Q
from .switch import costs
from .workload import load

HERE = os.path.dirname(__file__)
RES = os.path.normpath(os.path.join(HERE, "..", "results", "pd"))
SIM_RES = os.path.normpath(os.path.join(HERE, "..", "results"))
HOURS = 3600.0


# ---------------------------------------------------------------- 池收缩 → prefill 乘数（读 PR #90 的 MRC）
def pool_shrink_mult(trace: str):
    """host 权重副本（510 GB/节点）从 DRAM 池扣掉后，prefill NPU 占用的相对增量。
    取 PR #90 回放 `results/<trace>.json` 中 KVCM（现实 hook）在 f=1 与扣除后 f 的 npu_busy（线性插值）。"""
    from kvcm_sim.params import DEFAULT as P
    runs = [r for r in json.load(open(os.path.join(SIM_RES, f"{trace}.json")))
            if r["policy"] == "kvcm-real" and not r.get("variant")]
    xs = sorted({r["f"] for r in runs})
    ys = [np.mean([r["npu_busy"] for r in runs if r["f"] == x]) for x in xs]
    f_new = 1.0 - Q.host_weight_copy * Q.n_nodes / P.dram_kv_total
    base = float(np.interp(1.0, xs, ys))
    new = float(np.interp(max(f_new, xs[0]), xs, ys))
    return {"f_after": f_new, "npu_busy_f1": base, "npu_busy_after": new, "mult": new / base}


# ---------------------------------------------------------------- 场景
def scenarios(cc, qw):
    d_node = {}
    for S in (cc, qw):
        kv = S.kv_bytes(Q.kv_bytes_per_token)
        dP = S.p.sum() / S.n / (Q.npu_per_node * Q.rho_p)
        dD = (kv * S.out * Q.t_step).sum() / S.n / Q.kv_cap_node
        d_node[S.name] = (dP, dD)
    tot = 12.0                       # 参考负载：平均约 12 个节点的需求；goodput 以它的倍数 α 报告
    dcc, dqw = sum(d_node["cc256k"]), sum(d_node["qwen_coder"])
    r_cc = tot / dcc
    sc = [
        F.Scenario("S0", "平稳：CC 会话 Poisson 到达（PR #90 方式，未注入）", False,
                   [(cc, lambda t: np.full_like(np.asarray(t, float), r_cc))]),
        F.Scenario("S1", "【注入】总量变化：CC 到达率按 24h 正弦，峰谷 2:1，配比不变", True,
                   [(cc, F.sinus(r_cc, 1 / 3))]),
    ]
    for nm, amp, lab in (("S2", 0.40, "【注入】配比变化（强）：总需求≈恒定，CC 需求占比 50%±40% 按 24h 正弦，其余为 Qwen coder"),
                         ("S2m", 0.15, "【注入】配比变化（弱）：同上，CC 需求占比 50%±15%")):
        a = lambda t, amp=amp: 0.5 + amp * np.sin(2 * np.pi * np.asarray(t, float) / F.DAY)
        ra, rb = F.mix_rates(tot, a, dcc, dqw)
        sc.append(F.Scenario(nm, lab, True, [(cc, ra), (qw, rb)]))
    a = lambda t: 0.5 + 0.40 * np.sin(2 * np.pi * np.asarray(t, float) / (2 * HOURS))
    ra, rb = F.mix_rates(tot, a, dcc, dqw)
    sc.append(F.Scenario("S4", "【注入】配比快速变化：同 S2 幅度，周期 2h", True, [(cc, ra), (qw, rb)]))
    # S5：P 占比阶跃。CC 需求占比 a 与 P 占比 s 近似线性：s ≈ a·s_cc + (1−a)·s_qw
    s_cc = d_node["cc256k"][0] / dcc
    s_qw = d_node["qwen_coder"][0] / dqw
    a_of = lambda s_: (s_qw - s_) / (s_qw - s_cc)
    wu = 8 * HOURS
    steps = (2 * HOURS, 6 * HOURS, 10 * HOURS, 14 * HOURS, 18 * HOURS, 22 * HOURS)
    a = F.step_fn(a_of(0.5), a_of(0.8), [wu + x for x in steps[0::2]], [wu + x for x in steps[1::2]])
    ra = lambda t, a=a: tot * a(t) / dcc
    # CC 会话长、需求滞后于到达率；按 CC 实际需求补 Qwen（会话短、需求跟随到达率），使总需求≈恒定
    probe = F.Scenario("S5cc", "", True, [(cc, ra)], warmup=0.0)
    dm_c = F.build_demand(probe, Q)
    nPc, nDc = F.need(dm_c, Q, 1.0)
    tc = dm_c.t + Q.window / 2
    fill = np.maximum(0.0, tot - (nPc + nDc))
    rb = lambda t, tc=tc, fill=fill: np.interp(np.asarray(t, float), tc, fill) / dqw
    sc.append(F.Scenario("S5", "【注入】配比阶跃：CC 到达率阶跃（P 占比目标 0.5→0.8，5 分钟斜坡，持续 4h 再跳回，24h 内 3 次），"
                         "Qwen coder 按 CC 实际需求补齐，总需求≈恒定",
                         True, [(cc, ra), (qw, rb)], steps=steps))
    a = lambda t: 0.5 + 0.40 * np.sin(2 * np.pi * np.asarray(t, float) / F.DAY)
    env = lambda t: 1.0 + (1 / 3) * np.sin(2 * np.pi * np.asarray(t, float) / F.DAY + np.pi / 2)
    ra, rb = F.mix_rates(tot, a, dcc, dqw)
    sc.append(F.Scenario("S3", "【注入】总量 + 配比同时变化（S1 包络错相 6h 叠加 S2）", True,
                         [(cc, lambda t: ra(t) * env(t)), (qw, lambda t: rb(t) * env(t))]))
    return sc, d_node, r_cc


def sensitivity(scs, e4, shrink):
    """S2 / S4 上扫 HBM KV 预算、decode 每步耗时、sleep/wake 权重回填耗时。"""
    rows = []
    variants = [("基准", {}), ("HBM KV 360 GB/节点", {"hbm_kv_per_node": 360e9}),
                ("decode 每步 12.5 ms", {"t_step": 0.0125}), ("decode 每步 37.5 ms", {"t_step": 0.0375}),
                ("sleep/wake 权重 10 s", {"t_weights_sleep": 10.0}), ("sleep/wake 权重 120 s", {"t_weights_sleep": 120.0}),
                ("sleep/wake 权重 600 s", {"t_weights_sleep": 600.0})]
    tol = Q.overload_tol
    for sc in scs:
        if sc.name not in ("S2", "S4"):
            continue
        cache = {}
        for lab, kw in variants:
            q = Q.with_(**kw)
            key = (q.t_step,)
            if key not in cache:
                cache[key] = F.build_demand(sc, q)
            dm = cache[key]
            tr = {t.name: t for t in tiers(e4, shrink, dm, q)}
            b = F.goodput(lambda a: F.static_best(dm, q, a, mixed=False), tol)
            r = {"scenario": sc.name, "variant": lab, "static-pd": b,
                 "static-mixed": F.goodput(lambda a: F.static_best(dm, q, a, only=[(0, 0, q.n_nodes)]), tol),
                 "static-best3": F.goodput(lambda a: F.static_best(dm, q, a, mixed=True), tol)}
            for t in ("L-pool", "L-drain", "S-pool"):
                r[f"lag@{t}"] = F.goodput(lambda a: F.dynamic(dm, q, a, tr[t], oracle=False), tol)
            rows.append(r)
            print("sens", json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}, ensure_ascii=False))
    return rows


def step_analysis(sc, dm, pol, e4, shrink):
    """S5：在共同负载 α* = 0.95 × 理想动态 P:D goodput 下，逐次阶跃统计过载。"""
    a = 0.95 * pol["dyn-pd-oracle@ideal"]
    W = Q.window
    rows = {}
    rows["static-pd"] = F.static_best(dm, Q, a, mixed=False)
    rows["static-best3"] = F.static_best(dm, Q, a, mixed=True)
    rows["static-mixed"] = F.static_best(dm, Q, a, only=[(0, 0, Q.n_nodes)])
    for tr in tiers(e4, shrink, dm):
        rows[f"{tr.name}|oracle"] = F.dynamic(dm, Q, a, tr)
        rows[f"{tr.name}|lag"] = F.dynamic(dm, Q, a, tr, oracle=False)
    out = {"alpha": a, "steps_h": [x / HOURS for x in sc.steps], "policies": {}}
    for k, r in rows.items():
        ps = F.per_step(r["ov"], dm.t, sc.steps, W)
        out["policies"][k] = {"total_over_windows": int(r["ov"].sum()), "switches": r.get("switches", 0),
                              "alloc": list(r["alloc"]) if "alloc" in r else None, "per_step": ps,
                              "ov": r["ov"].astype(int).tolist()}
    return out


def tiers(e4, shrink, dm, q=Q):
    P = dm.P
    pm = np.ones_like(P)
    if dm.parts:
        num = sum(shrink[k]["mult"] * dm.parts[k][0] for k in dm.parts)
        pm = np.where(P > 0, num / np.maximum(P, 1e-12), 1.0)
    cc = e4["cc256k"]
    ctrl = q.t_p_drain
    pool_dp = ctrl + cc["pool_flush_s"]["p95"] + cc["pool_reload_node_s"]["p95"]
    d2d_dp = ctrl + cc["d2d_s"]["p95"]
    drain_dp = cc["drain_s"]["p95"] * q.t_step / Q.t_step
    w = q.t_weights_sleep
    return [
        F.Tier("L-pool", "同布局逻辑切换 + KVCM 池（丢弃后从池重读）", pool_dp, ctrl, 0.0, 1.0),
        F.Tier("L-d2d", "同布局逻辑切换 + 无池点对点迁移（需引擎支持）", d2d_dp, ctrl, 0.0, 1.0),
        F.Tier("L-drain", "同布局逻辑切换 + 无池排空（等在途跑完）", drain_dp, ctrl, 0.5, 1.0),
        F.Tier("S-pool", "PR #89 sleep/wake + host H2D + KVCM 池（host 副本挤占池）", pool_dp + w, ctrl + w, 0.0, pm),
        F.Tier("S-drain", "PR #89 sleep/wake + host H2D + 无池排空", drain_dp + w, ctrl + w, 0.5, 1.0),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fig-dir", default=os.path.join(RES, "figures"))
    ap.add_argument("--copy-to", default="", help="图另拷一份到该目录")
    ap.add_argument("--e4-n", type=int, default=400)
    args = ap.parse_args()
    os.makedirs(RES, exist_ok=True)
    t0 = time.time()
    cc, qw = load("cc256k"), load("qwen_coder")

    e4 = {S.name: costs(S, Q, args.e4_n) for S in (cc, qw)}
    shrink = {k: pool_shrink_mult(k) for k in ("cc256k", "qwen_coder")}
    print("E4", json.dumps({k: {m: v[m]["p95"] for m in v} for k, v in e4.items()}, ensure_ascii=False))
    print("shrink", shrink)

    scs, d_node, r_cc = scenarios(cc, qw)
    out = {"params": {k: getattr(Q, k) for k in Q.__dataclass_fields__}, "f_mixed": Q.f_mixed,
           "d_node_per_session": d_node, "r_cc_ref_per_s": r_cc, "pool_shrink": shrink, "e4": e4,
           "scenarios": {}}
    series = {}
    for sc in scs:
        dm = F.build_demand(sc, Q)
        nP, nD = F.need(dm, Q, 1.0)
        share = nP / (nP + nD)
        tol = Q.overload_tol
        res = {"label": sc.label, "injected": sc.injected,
               "mean_nodes": float((nP + nD).mean()), "peak_nodes": float((nP + nD).max()),
               "p_share_min": float(share.min()), "p_share_max": float(share.max()),
               "closed_form_overprov": F.closed_form_gain(share),
               "total_cv": float((nP + nD).std() / (nP + nD).mean()), "policies": {}}
        pol = res["policies"]
        pol["static-pd"] = F.goodput(lambda a: F.static_best(dm, Q, a, mixed=False), tol)
        pol["static-mixed"] = F.goodput(lambda a: F.static_best(dm, Q, a, only=[(0, 0, Q.n_nodes)]), tol)
        pol["static-best3"] = F.goodput(lambda a: F.static_best(dm, Q, a, mixed=True), tol)
        zero = F.Tier("ideal", "理想（切换零代价）", 0.0, 0.0, 0.0, 1.0)
        pol["dyn-pd-oracle@ideal"] = F.goodput(lambda a: F.dynamic(dm, Q, a, zero), tol)
        pol["dyn-3-oracle@ideal"] = F.goodput(lambda a: F.dynamic(dm, Q, a, zero, mixed=True), tol)
        res["tiers"] = {}
        for tr in tiers(e4, shrink, dm):
            r = {}
            r["dyn-pd-oracle"] = F.goodput(lambda a: F.dynamic(dm, Q, a, tr), tol)
            r["dyn-pd-lag"] = F.goodput(lambda a: F.dynamic(dm, Q, a, tr, oracle=False), tol)
            r["dyn-3-oracle"] = F.goodput(lambda a: F.dynamic(dm, Q, a, tr, mixed=True), tol)
            g = r["dyn-pd-lag"]
            r["switches_at_lag_goodput"] = F.dynamic(dm, Q, g, tr, oracle=False)["switches"] if g > 0 else 0
            r["t_dp"], r["t_pd"] = tr.t_dp, tr.t_pd
            r["p_mult_mean"] = float(np.mean(tr.p_mult))
            res["tiers"][tr.name] = {"label": tr.label, **r}
        # 静态 P:D 在 α=1 下的分配、动态 oracle 的逐窗分配（画图用）
        res["static_pd_alloc"] = F.static_best(dm, Q, pol["static-pd"], mixed=False)["alloc"]
        res["static_best3_alloc"] = F.static_best(dm, Q, pol["static-best3"], mixed=True)["alloc"]
        out["scenarios"][sc.name] = res
        series[sc.name] = (dm, nP, nD, share)
        if sc.steps:
            out["s5_steps"] = step_analysis(sc, dm, pol, e4, shrink)
        print(f"[{sc.name}] {time.time() - t0:.0f}s", json.dumps({k: round(v, 3) for k, v in pol.items()}),
              {k: round(v["dyn-pd-lag"], 3) for k, v in res["tiers"].items()})
    out["sensitivity"] = sensitivity(scs, e4, shrink)
    json.dump(out, open(os.path.join(RES, "pd_results.json"), "w"), ensure_ascii=False, indent=1)

    from .report import figures, summary
    figs = figures(out, series, args.fig_dir)
    if args.copy_to:
        os.makedirs(args.copy_to, exist_ok=True)
        for f in figs:
            shutil.copy(f, args.copy_to)
    summary(out, os.path.join(RES, "summary.md"))
    print("done", time.time() - t0)


if __name__ == "__main__":
    main()
