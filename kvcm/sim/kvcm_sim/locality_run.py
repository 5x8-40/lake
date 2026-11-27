"""局部性实验编排（Project 文档《KVCM 池化的近远端、冷热与争用》§4.2 的五组对照）。

    PYTHONPATH=. python3 -m kvcm_sim.locality_run --trace cc256k --jobs 2
    PYTHONPATH=. python3 -m kvcm_sim.locality_run --trace zipf --jobs 2     # 倾斜度扫描（合成）

结果写到 `results/locality/<trace>.json`，图与汇总由 `kvcm_sim.locality_report` 生成。
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import math
import os
import time
from multiprocessing import Pool

import numpy as np

from .locality import BW, LocCfg, LocSim
from .params import DEFAULT as P0
from .replay import Config, Sim
from .run import BAL_ABS, FULL, HORIZON, PAGE, POLICIES, REAL_BUDGET, WARMUP, load, summarize
from .traces import make_zipf
from . import run as _run

if os.environ.get("LOC_H"):  # "horizon,warmup"（秒）：截止时间约束下缩短回放窗口，报告注明
    HORIZON, WARMUP = (float(x) for x in os.environ["LOC_H"].split(","))
    _run.HORIZON, _run.WARMUP = HORIZON, WARMUP

K = int(os.environ.get("LOC_K", "32"))   # 压力扫描与增益网格用 64（截止时间约束，报告注明）
OUT = os.path.join(os.path.dirname(__file__), "..", "results", "locality")
ZIPF_ALPHAS = [0.6, 0.8, 1.0, 1.2, 1.4]
ZIPF_DOCS_FULL = 80000            # 全规模文档数（≈2× 集群 HBM、0.6× 池）；切片按 1/k 缩小

# 池侧策略（基于主线 POLICIES）+ 局部性设定
VARIANTS = {
    # 扁平共享池：键哈希条带化、单副本、主线路由
    "flat": ("shared", dict()),
    "local": ("shared", dict(placement="local")),
    "local+passive": ("shared", dict(placement="local", repl="passive")),
    "hash+hot": ("shared", dict(repl="hot")),
    "kvcm": ("kvcm-real", dict()),
    "kvcm+loc": ("kvcm-real", dict(placement="local", repl="kvcm", route="ttft")),
    "kvcm-oracle+loc": ("kvcm-oracle", dict(placement="local", repl="kvcm", route="ttft")),
    "local+load": ("shared", dict(placement="local", route="load")),
    "local+ttft": ("shared", dict(placement="local", route="ttft")),
    # 消融：同 kvcm+loc，但预测 TTFT 路由不计数据位置（只看排队 + 命中）
    "kvcm+noloc": ("kvcm-real", dict(placement="local", repl="kvcm", route="ttft_noloc")),
    "kvcm-oracle": ("kvcm-oracle", dict()),
}

# 压力敏感性（全部【注入】，RoCE + 拓扑争用，标称 14 TB 池）。
# _hbm：每实例 HBM KV 倍数；_kvx：KV bytes/token 倍数（HBM/池按 token 容量不变，只放大传输字节）
STRESS = {
    "base": {},
    "hbm1/2": dict(_hbm=0.5), "hbm1/4": dict(_hbm=0.25),
    "bw1/4": dict(xbw=0.25), "bw1/10": dict(xbw=0.1),
    "bg30": dict(bg=0.3), "bg50": dict(bg=0.5), "bg70": dict(bg=0.7),
    "kv4x": dict(_kvx=4.0), "kv8x": dict(_kvx=8.0),
    "C1": dict(_hbm=0.25, xbw=0.1, bg=0.5),
    "C2": dict(_kvx=8.0, xbw=0.25, bg=0.5),
    "C3": dict(_hbm=0.25, _kvx=8.0, xbw=0.1, bg=0.7),
}
STRESS_VARS = ("flat", "local", "local+passive", "hash+hot", "kvcm", "kvcm+loc", "kvcm+noloc")

# KVCM 增益区间网格（全部【注入】，RoCE + 链路排队）：负载倍数 × {池容量, 每实例 HBM, KV 大小}。
# _load：会话到达率倍数（等价于复制会话，会话内时序与复用不变）；
# _kvc：KV bytes/token 倍数，HBM/池按字节不变（token 容量 ÷ 倍数，传输字节 × 倍数）
GRID_LOADS = (1.0, 1.5, 2.0)
GRID_AXES = (("pool", 0.25), ("pool", 0.5), ("pool", 1.0), ("hbm", 0.5), ("hbm", 0.25))
GRID_VARS = ("flat", "kvcm", "kvcm-oracle", "kvcm+loc", "kvcm-oracle+loc", "local+ttft")


def grid_points():
    out = []
    for ax, v in GRID_AXES:
        for m in GRID_LOADS:
            lo = {"pool": {}, "hbm": dict(_hbm=v), "kv": dict(_kvc=v)}[ax]
            f = v if ax == "pool" else 1.0
            out.append((f"{ax}={v:g}@{m:g}x", f, dict(_load=m, **lo)))
    return out
ZSTRESS = ("bw1/10", "C3")
ZSTRESS_ALPHAS = (0.6,)
ZSTRESS_VARS = ("flat", "local+passive", "hash+hot", "kvcm+loc")


def experiments(trace: str, stress: bool = False, grid: bool = False):
    """(实验, 变体, 池容量倍数, 局部性覆盖, bytes/token 覆盖)。"""
    ex = []
    if grid:
        for pt, f, lo in grid_points():
            for v in GRID_VARS:
                ex.append(("G", v, f, dict(transport="roce", cost="queue", _pt=pt, **lo), None, None))
        return ex
    if stress:
        pts, vs, als = (ZSTRESS, ZSTRESS_VARS, ZSTRESS_ALPHAS) if trace == "zipf" else (STRESS, STRESS_VARS, (None,))
        for pt in pts:
            for a in als:
                for v in vs:
                    ex.append(("S", v, 1.0, dict(transport="roce", cost="queue", _pt=pt, **STRESS[pt]), None, a))
        return ex
    if trace == "zipf":
        for a in ZIPF_ALPHAS:
            for tr in BW:
                for v in ("flat", "hash+hot", "kvcm+loc"):
                    ex.append(("E3", v, 1.0, dict(transport=tr), None, a))
        return ex
    # E1 成本模型：扁平 vs 拓扑 vs 拓扑+争用 × 是否与排队重叠 × 传输路径
    for tr in BW:
        for cost in ("flat", "topo", "contend"):
            for ov in (False, True):
                ex.append(("E1", "flat", 1.0, dict(transport=tr, cost=cost, overlap=ov), None, None))
    # E2 放置与副本（拓扑+争用，读不与排队重叠）
    for tr, fs in (("fabric", (1.0,)), ("roce", (1.0, 0.25))):
        for f in fs:
            for v in ("flat", "local", "local+passive", "hash+hot", "kvcm", "kvcm+loc", "kvcm-oracle+loc"):
                ex.append(("E2", v, f, dict(transport=tr), None, None))
    # E4 路由（本地放置）：主线规则 / 最短队列 / 预测 TTFT
    for tr in BW:
        for v in ("local", "local+load", "local+ttft"):
            ex.append(("E4", v, 1.0, dict(transport=tr), None, None))
    # E5 跨机代价敏感度：bytes/token（BF16 3,200 vs 官方 FP4 890），拓扑无争用
    for tr in BW:
        for kvb in (3200.0, 890.0):
            ex.append(("E5", "flat", 1.0, dict(transport=tr, cost="topo"), kvb, None))
    return ex


def _wl(trace: str, alpha):
    if trace == "zipf":
        return make_zipf(alpha, max(1, ZIPF_DOCS_FULL // K))
    return load(trace)


def _cfg(P, f: float, pol: str, hbm_mult: float = 1.0, cap_mult: float = 1.0) -> Config:
    return Config(n_inst=P.n_inst,
                  hbm_pages=max(1, int(P.pages(P.hbm_kv_per_inst * hbm_mult * cap_mult, PAGE) / K)),
                  dram_pages=max(1, int(P.pages(P.dram_kv_total * cap_mult, PAGE) * f / K)),
                  bal_abs=BAL_ABS, **POLICIES[pol])


def calibrate_zipf(alpha: float) -> float:
    wl = _wl("zipf", alpha)
    rate = 1.0
    for _ in range(2):
        sim = Sim(dataclasses.replace(_cfg(P0, 1.0, "shared"), ideal_only=True), wl, rate=rate / K,
                  horizon=HORIZON, warmup=WARMUP, seed=1)
        s = summarize(sim.run(), K, sim)
        rate *= P0.prefill_util_target / s["npu_util_ideal"]
    return rate


def run_one(args):
    exp, trace, var, f, lo, kvb, alpha, rate = args
    lo = dict(lo)
    pt = lo.pop("_pt", None)
    hbm_m = lo.pop("_hbm", 1.0)
    kvx = lo.pop("_kvx", 1.0)
    kvc = lo.pop("_kvc", 1.0)
    load_m = lo.pop("_load", 1.0)
    rate = rate * load_m
    if kvx != 1.0:
        kvb = P0.kv_bytes_per_token * kvx
    if kvc != 1.0:
        kvb = P0.kv_bytes_per_token * kvc
    P = dataclasses.replace(P0, kv_bytes_per_token=kvb) if kvb else P0
    pol, lov = VARIANTS[var]
    loc = LocCfg(k=K, **{**lov, **lo})
    wl = _wl(trace, alpha)
    t0 = time.time()
    sim = LocSim(_cfg(P, f, pol, hbm_m, kvx), loc, wl, rate=rate / K, horizon=HORIZON, warmup=WARMUP, params=P, seed=1)
    rec = sim.run()
    s = summarize(rec, K, sim)
    m = {k: np.asarray(v) for k, v in sim.m.items()}
    ttft, exp_t = m["ttft"], m["exp"]
    pb = m["pool_b"]
    win = HORIZON - WARMUP
    eg = sim.eg_bytes
    util = eg / (win * sim.B_r)                       # 每节点出口平均利用率（切片口径 = 全规模）
    has = pb > 0
    s.update(
        exp=exp, trace=trace, variant=var, f=f, kv_bytes=P.kv_bytes_per_token, alpha=alpha, rate_full=rate,
        transport=loc.transport, cost=loc.cost, overlap=loc.overlap, placement=loc.placement,
        repl=loc.repl, route=loc.route,
        des_ttft_mean=float(ttft.mean()), des_ttft_p50=float(np.percentile(ttft, 50)),
        des_ttft_p95=float(np.percentile(ttft, 95)), des_ttft_p99=float(np.percentile(ttft, 99)),
        des_ttft_pool_p95=float(np.percentile(ttft[has], 95)) if has.any() else 0.0,
        exposed_mean=float(exp_t.mean()), exposed_share=float(exp_t.sum() / max(ttft.sum(), 1e-12)),
        exposed_frac=float((exp_t > 1e-9).mean()),
        exposed_pool_p95=float(np.percentile(exp_t[has], 95)) if has.any() else 0.0,
        wait_p95=float(np.percentile(m["W"], 95)),
        tau_pool_p95=float(np.percentile(m["tau"][has], 95)) if has.any() else 0.0,
        pool_req_frac=float(has.mean()),
        local_byte_share=float(m["loc_b"].sum() / max(pb.sum(), 1)),
        egress_util_mean=float(util.mean()), egress_util_max=float(util.max()),
        egress_util_tot_max=float((util + sim.bgn).max()),
        k=K, horizon=HORIZON, point=pt, hbm_mult=hbm_m, kv_mult=kvx, kv_cap_mult=kvc, load=load_m, xbw=loc.xbw, bg=loc.bg,
        egress_max_over_mean=float(eg.max() / max(eg.mean(), 1)),
        egress_cv=float(eg.std() / max(eg.mean(), 1)),
        replica_share=float(np.mean(sim.rep_samples)) if sim.rep_samples else 0.0,
        copy_gb_per_s=float(sim.copy_bytes / win * K / 1e9),
        wall_s=round(time.time() - t0, 1),
    )
    # E5：按上下文长度分桶的“跨机相对本机”额外 TTFT（全规模秒）
    if exp == "E5":
        bl, br = BW[loc.transport]
        rem = (pb - m["loc_b"]) / 1e9
        extra = rem * (1 / br - 1 / bl)
        n_in = rec["n_in"][rec["t"] >= WARMUP][: len(extra)]
        edges = [0, 32768, 65536, 131072, 262144, math.inf]
        s["extra_by_ctx"] = []
        for a, b in zip(edges[:-1], edges[1:]):
            sel = (n_in >= a) & (n_in < b)
            if sel.any():
                s["extra_by_ctx"].append([a, b, int(sel.sum()), float(extra[sel].mean()),
                                          float(np.percentile(extra[sel], 95)), float(ttft[sel].mean())])
    tag = f"{exp} {var:16s} {loc.transport:6s} {loc.cost:7s} ov={int(loc.overlap)} f={f:<5g}"
    if alpha is not None:
        tag += f" a={alpha}"
    if kvb:
        tag += f" kvb={kvb:.0f}"
    if pt:
        tag += f" [{pt}]"
    print(f"[{trace}] {tag} hit={s['hit_rate']:.4f} p95={s['des_ttft_p95']:.3f} exp={s['exposed_share']:.3f} "
          f"loc={s['local_byte_share']:.2f} egmax={s['egress_util_max']:.2f} rep={s['replica_share']:.3f} "
          f"({s['wall_s']}s)", flush=True)
    return s


def _key(r):
    return (r["exp"], r["variant"], r["f"], r["transport"], r["cost"], r["overlap"], r["kv_bytes"], r["alpha"],
            r.get("point"))


def _jkey(j):
    e, _, var, f, lo, kvb, alpha, _ = j
    lo = dict(lo)
    pt = lo.pop("_pt", None)
    lo.pop("_hbm", None)
    lo.pop("_load", None)
    kvx = lo.pop("_kvx", 1.0) * lo.pop("_kvc", 1.0)
    c = LocCfg(**lo)
    return (e, var, f, c.transport, c.cost, c.overlap, kvb or P0.kv_bytes_per_token * kvx, alpha, pt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--only", default="")
    ap.add_argument("--alphas", default="", help="只跑这些 α（zipf），逗号分隔")
    ap.add_argument("--stress", action="store_true", help="压力敏感性扫描（写到 <trace>_stress<suffix>.json）")
    ap.add_argument("--grid", action="store_true", help="KVCM 增益区间网格（写到 <trace>_grid<suffix>.json）")
    ap.add_argument("--points", default="", help="只跑这些压力点，逗号分隔")
    ap.add_argument("--suffix", default="", help="写到 <trace><suffix>.json，便于并行后合并")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    cal = json.load(open(os.path.join(OUT, "..", "calibration.json")))
    jobs = []
    ex = [e for e in experiments(args.trace, args.stress, args.grid) if not args.only or e[0] in args.only.split(",")]
    if args.points:
        ps = set(args.points.split(","))
        ex = [e for e in ex if e[3].get("_pt") in ps]
    if args.alphas:
        al = {float(x) for x in args.alphas.split(",")}
        ex = [e for e in ex if e[5] in al]
    zcal_p = os.path.join(OUT, "calibration_zipf.json")
    zcal = json.load(open(zcal_p)) if os.path.exists(zcal_p) else {}
    for e, var, f, lo, kvb, alpha in ex:
        if args.trace == "zipf":
            key = f"{alpha}@k{K}" if f"{alpha}@k{K}" in zcal or f"{alpha}@k32" not in zcal else f"{alpha}@k32"
            if key not in zcal:
                zcal[key] = calibrate_zipf(alpha)
                json.dump(zcal, open(zcal_p, "w"), indent=2)
                print(f"[zipf] a={alpha} rate={zcal[key] * 3600:.0f}/h", flush=True)
            rate = zcal[key]
        else:
            rate = cal.get(f"{args.trace}@k{K}", cal[f"{args.trace}@k32"])  # 全规模到达率，与 k 无关
        jobs.append((e, args.trace, var, f, lo, kvb, alpha, rate))
    tag = "_grid" if args.grid else ("_stress" if args.stress else "")
    path = os.path.join(OUT, f"{args.trace}{tag}{args.suffix}.json")
    res = json.load(open(path)) if os.path.exists(path) else []
    done = set()
    for q in glob.glob(os.path.join(OUT, f"{args.trace}{tag}*.json")):
        done |= {_key(r) for r in json.load(open(q))}
    jobs = [j for j in jobs if _jkey(j) not in done]
    with Pool(args.jobs) as pool:
        for s in pool.imap_unordered(run_one, jobs):
            res.append(s)
            json.dump(res, open(path, "w"), indent=1)


if __name__ == "__main__":
    main()
