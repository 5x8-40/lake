"""局部性实验编排（Project 文档《KVCM 池化的近远端、冷热与争用》§4.2 的五组对照）。

    PYTHONPATH=. python3 -m kvcm_sim.locality_run --trace cc256k --jobs 2
    PYTHONPATH=. python3 -m kvcm_sim.locality_run --trace zipf --jobs 2     # 倾斜度扫描（合成）

结果写到 `results/locality/<trace>.json`，图与汇总由 `kvcm_sim.locality_report` 生成。
"""

from __future__ import annotations

import argparse
import dataclasses
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

K = 32
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
}


def experiments(trace: str):
    """(实验, 变体, 池容量倍数, 局部性覆盖, bytes/token 覆盖)。"""
    ex = []
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


def _cfg(P, f: float, pol: str) -> Config:
    return Config(n_inst=P.n_inst,
                  hbm_pages=max(1, int(P.pages(P.hbm_kv_per_inst, PAGE) / K)),
                  dram_pages=max(1, int(P.pages(P.dram_kv_total, PAGE) * f / K)),
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
    P = dataclasses.replace(P0, kv_bytes_per_token=kvb) if kvb else P0
    pol, lov = VARIANTS[var]
    loc = LocCfg(k=K, **{**lov, **lo})
    wl = _wl(trace, alpha)
    t0 = time.time()
    sim = LocSim(_cfg(P, f, pol), loc, wl, rate=rate / K, horizon=HORIZON, warmup=WARMUP, params=P, seed=1)
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
    print(f"[{trace}] {tag} hit={s['hit_rate']:.4f} p95={s['des_ttft_p95']:.3f} exp={s['exposed_share']:.3f} "
          f"loc={s['local_byte_share']:.2f} egmax={s['egress_util_max']:.2f} rep={s['replica_share']:.3f} "
          f"({s['wall_s']}s)", flush=True)
    return s


def _key(r):
    return (r["exp"], r["variant"], r["f"], r["transport"], r["cost"], r["overlap"], r["kv_bytes"], r["alpha"])


def _jkey(j):
    e, _, var, f, lo, kvb, alpha, _ = j
    c = LocCfg(**lo)
    return (e, var, f, c.transport, c.cost, c.overlap, kvb or P0.kv_bytes_per_token, alpha)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--only", default="")
    ap.add_argument("--alphas", default="", help="只跑这些 α（zipf），逗号分隔")
    ap.add_argument("--suffix", default="", help="写到 <trace><suffix>.json，便于并行后合并")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    cal = json.load(open(os.path.join(OUT, "..", "calibration.json")))
    jobs = []
    ex = [e for e in experiments(args.trace) if not args.only or e[0] in args.only.split(",")]
    if args.alphas:
        al = {float(x) for x in args.alphas.split(",")}
        ex = [e for e in ex if e[5] in al]
    zcal_p = os.path.join(OUT, "calibration_zipf.json")
    zcal = json.load(open(zcal_p)) if os.path.exists(zcal_p) else {}
    for e, var, f, lo, kvb, alpha in ex:
        if args.trace == "zipf":
            key = f"{alpha}@k{K}"
            if key not in zcal:
                zcal[key] = calibrate_zipf(alpha)
                json.dump(zcal, open(zcal_p, "w"), indent=2)
                print(f"[zipf] a={alpha} rate={zcal[key] * 3600:.0f}/h", flush=True)
            rate = zcal[key]
        else:
            rate = cal[f"{args.trace}@k{K}"]
        jobs.append((e, args.trace, var, f, lo, kvb, alpha, rate))
    path = os.path.join(OUT, f"{args.trace}{args.suffix}.json")
    res = json.load(open(path)) if os.path.exists(path) else []
    done = {_key(r) for r in res}
    jobs = [j for j in jobs if _jkey(j) not in done]
    with Pool(args.jobs) as pool:
        for s in pool.imap_unordered(run_one, jobs):
            res.append(s)
            json.dump(res, open(path, "w"), indent=1)


if __name__ == "__main__":
    main()
