"""实验编排：负载校准 → 命中率-容量曲线（MRC）→ 消融 → TTFT / 等效 NPU。

    PYTHONPATH=kvcm/sim python3 -m kvcm_sim.run --trace cc256k --jobs 2   # k 默认 32

结果写到 `kvcm/sim/results/<trace>.json`（每个 run 一条汇总），图由 `kvcm_sim.report` 生成。
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

from .params import DEFAULT as P
from .replay import Config, Sim
from .traces import load_cc, load_codex, load_qwen

TRACE_PATHS = {
    "cc256k": os.environ.get("CC256K", "/tmp/traces/cc256k.jsonl"),
    "codex": os.environ.get("CODEX", "/tmp/traces/codex.json"),
    "qwen_coder": os.environ.get("QWEN_CODER", "/tmp/traces/qwen_coder.jsonl"),
}
PAGE = 512
BAL_ABS = 11
HORIZON = 36000.0
WARMUP = 10800.0
GAP_EDGES = [0, 10, 60, 600, 3600, math.inf]
GAP_LABELS = ["<10s", "10-60s", "1-10min", "10-60min", ">1h"]

# KVCM 主线 = 只作用于 DRAM 池的保活 + 零引用优先；HBM 归引擎（纯 LRU），各策略相同
FULL = dict(keepalive=True, zeroref=True)
# realistic 的池钉预算取小（大预算会挤掉 LRU 本来留得住的内容）；oracle 用默认预算
REAL_BUDGET = dict(pool_pin_frac=0.1)

POLICIES = {
    "local": dict(policy="local_lru"),
    "shared": dict(policy="shared_lru"),
    "kvcm-none": dict(policy="kvcm", hook="none", **FULL),
    "kvcm-real": dict(policy="kvcm", hook="realistic", **FULL, **REAL_BUDGET),
    "kvcm-oracle": dict(policy="kvcm", hook="oracle", **FULL),
    # 消融链：shared → +池保活 → +零引用优先（= KVCM）
    "real+ka": dict(policy="kvcm", hook="realistic", keepalive=True, **REAL_BUDGET),
    "oracle+ka": dict(policy="kvcm", hook="oracle", keepalive=True),
}

# x 轴只扫 DRAM 池容量（标称 14 TB 的倍数）；HBM 固定为真实大小
# 收益工作区间 = 池 ≈ 1–2× 集群 HBM；两端各加一个点
F_HBM1 = round(P.hbm_kv_per_inst * P.n_inst / P.dram_kv_total, 4)
MRC_F = sorted([1 / 32, 1 / 16, 1 / 8, 1 / 4, F_HBM1, 1 / 2, 2 * F_HBM1, 1, 2])
MRC_POLICIES = ["local", "shared", "kvcm-real", "kvcm-oracle"]
ABL_F = [1 / 4, 1]
ABL_POLICIES = ["kvcm-none", "real+ka", "oracle+ka"]
# 下界对照：下一轮剥掉思维链，decode KV 不被命中（同一负载）
STRIP_F = [1 / 4, 1]
STRIP_POLICIES = ["local", "shared", "kvcm-real", "kvcm-oracle"]
# 敏感性：HBM 预算不扣激活（每实例 360 GB）
HBM360_F = [1 / 4, 1]
HBM360_POLICIES = ["local", "shared", "kvcm-real", "kvcm-oracle"]
P360 = dataclasses.replace(P, act_in_budget=False)


def load(trace: str):
    if trace.startswith("cc"):
        return load_cc(TRACE_PATHS[trace], PAGE, tag=trace)
    if trace == "codex":
        return load_codex(TRACE_PATHS[trace], PAGE)
    if trace == "qwen_coder":
        return load_qwen(TRACE_PATHS[trace], PAGE)
    raise ValueError(trace)


def base_cfg(f: float, k: int, hbm_bytes: float | None = None) -> dict:
    return dict(
        n_inst=P.n_inst,
        hbm_pages=max(1, int(P.pages(hbm_bytes or P.hbm_kv_per_inst, PAGE) / k)),
        dram_pages=max(1, int(P.pages(P.dram_kv_total, PAGE) * f / k)),
        # 失衡阈值不随切片缩小：CC 的 subagent 扇出让单会话瞬时并发与 k 无关；
        # 取 k=8 时的 11，k=32 下 per-instance / shared 命中与 k=8 相差 < 0.2 个点
        bal_abs=BAL_ABS,
    )


def summarize(rec: dict, k: int, sim: Sim) -> dict:
    m = rec["t"] >= WARMUP
    n = rec["n_in"][m].astype(float)
    hit = rec["hit"][m].astype(float)
    hbm = rec["hbm"][m].astype(float)
    ideal = rec["ideal"][m].astype(float)
    N = n.sum()
    win = HORIZON - WARMUP
    hf = hit / np.maximum(n, 1)
    ns = P.npu_seconds(n, hf)
    ns_ideal = P.npu_seconds(n, ideal / np.maximum(n, 1))
    pool_bytes = (hit - hbm) * P.kv_bytes_per_token
    shared = sim.shared
    # 共享池页按“远端”计（14 节点里本实例只占 2 个）；per-instance DRAM 是本机
    ttft = P.ttft(n, hf, load_bytes_local=0 if shared else pool_bytes, load_bytes_remote=pool_bytes if shared else 0)
    ttft_roce = P.ttft(n, hf, load_bytes_local=0 if shared else pool_bytes,
                       load_bytes_remote=pool_bytes if shared else 0, remote_bw=P.bw_roce)
    gap = rec["gap"][m]
    buckets = {}
    first = np.isnan(gap)
    buckets["first-turn"] = [float(ideal[first].sum()), float(hit[first].sum()), int(first.sum())]
    for lo, hi, lab in zip(GAP_EDGES[:-1], GAP_EDGES[1:], GAP_LABELS):
        sel = (~first) & (gap >= lo) & (gap < hi)
        buckets[lab] = [float(ideal[sel].sum()), float(hit[sel].sum()), int(sel.sum())]
    out = {
        "requests": int(m.sum()),
        "input_tokens": float(N),
        "hit_rate": float(hit.sum() / N),
        "hbm_hit_rate": float(hbm.sum() / N),
        "ideal_hit_rate": float(ideal.sum() / N),
        "lost_reuse": float(1 - hit.sum() / ideal.sum()),
        "miss_tokens_per_s": float((n - hit).sum() / win * k),
        "npu_util": float(ns.sum() / win / (P.n_npu / k)),
        "npu_util_ideal": float(ns_ideal.sum() / win / (P.n_npu / k)),
        "npu_busy": float(ns.sum() / win * k),          # 全规模等效：prefill 平均占用 NPU 数
        "ttft_mean": float(ttft.mean()),
        "ttft_p50": float(np.percentile(ttft, 50)),
        "ttft_p95": float(np.percentile(ttft, 95)),
        "ttft_p99": float(np.percentile(ttft, 99)),
        "ttft_roce_mean": float(ttft_roce.mean()),
        "ttft_roce_p95": float(np.percentile(ttft_roce, 95)),
        "gap_buckets": buckets,
        "sessions": sim.n_sessions,
    }
    if "any" in rec:
        anyh = rec["any"][m].astype(float)
        out["any_hit_rate"] = float(anyh.sum() / N)
    return out


def run_one(args):
    trace, pol, f, k, rate, *rest = args
    seed = rest[0] if rest else 1
    wl = load(trace)
    base = pol.removeprefix("strip:").removeprefix("hbm360:")
    hbm = P360.hbm_kv_per_inst if pol.startswith("hbm360:") else None
    cfg = Config(**base_cfg(f, k, hbm), **POLICIES[base], attribution=(base == "local"),
                 decode_cache=not pol.startswith("strip:"))
    t0 = time.time()
    sim = Sim(cfg, wl, rate=rate / k, horizon=HORIZON, warmup=WARMUP, seed=seed)
    rec = sim.run()
    s = summarize(rec, k, sim)
    s.update(trace=trace, policy=pol, f=f, k=k, rate_full=rate, wall_s=round(time.time() - t0, 1),
             seed=seed, n_nodes=P.n_nodes, pool_tb=round(f * P.dram_kv_total / 1e12, 4))
    print(f"[{trace}] {pol:19s} f={f:<8.4g} s={seed} hit={s['hit_rate']:.4f} hbm={s['hbm_hit_rate']:.4f} "
          f"ideal={s['ideal_hit_rate']:.4f} util={s['npu_util']:.3f} ({s['wall_s']}s)", flush=True)
    return s


def calibrate(trace: str, k: int, target: float = P.prefill_util_target, anchor: str = "ideal") -> float:
    """prefill 占集群 target 时的会话到达率（全规模，会话/s）。

    anchor="ideal"：理想命中（无限容量、全局共享）下的占用，线性外推两轮；
    anchor="lru"：标称池（f=1）共享池 LRU 的实际占用。命中率随到达率变化，外推到相对误差 < 0.5%（最多 8 轮）。
    """
    wl = load(trace)
    mean_dur = np.mean([max(r.t for r in s.reqs) for s in wl.sessions])
    rate = len(wl.sessions) / max(HORIZON, mean_dur)  # 初值：任意
    for it in range(2 if anchor == "ideal" else 8):
        cfg = (Config(**base_cfg(1.0, k), policy="shared_lru", ideal_only=True) if anchor == "ideal"
               else Config(**base_cfg(1.0, k), **POLICIES["shared"]))
        sim = Sim(cfg, wl, rate=rate / k, horizon=HORIZON, warmup=WARMUP, seed=1)
        s = summarize(sim.run(), k, sim)
        u = s["npu_util_ideal"] if anchor == "ideal" else s["npu_util"]
        print(f"[{trace}] calibrate {anchor}: rate={rate * 3600:.0f}/h util={u:.4f}", flush=True)
        if anchor == "lru" and it >= 2 and abs(u / target - 1) < 0.005:
            break
        rate *= target / u
    return rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--k", type=int, default=32)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "results"))
    ap.add_argument("--only", default="", help="mrc|abl|strip|hbm360，留空=全部")
    ap.add_argument("--util", type=float, default=P.prefill_util_target, help="标定锚点：理想命中下 prefill 占 NPU 比例")
    ap.add_argument("--pool-tb", default="", help="池容量扫描点（TB，逗号分隔）；给出时只跑 MRC")
    ap.add_argument("--policies", default="", help="MRC 策略，逗号分隔")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--anchor", default="ideal", choices=("ideal", "lru"),
                    help="ideal：理想命中下占用；lru：标称池共享池 LRU 实际占用")
    ap.add_argument("--rate-k", type=int, default=0, help="复用该 k 的标定到达率（切片对照，全规模到达率与 k 无关）")
    args = ap.parse_args()
    seeds = [int(x) for x in args.seeds.split(",")]
    os.makedirs(args.out, exist_ok=True)
    cal_path = os.path.join(args.out, "calibration.json")
    cal = json.load(open(cal_path)) if os.path.exists(cal_path) else {}
    kc = args.rate_k or args.k
    key = (f"{args.trace}@k{kc}" + (f"@u{args.util:g}" if args.util != P.prefill_util_target else "")
           + ("@lru" if args.anchor == "lru" else ""))
    if key not in cal:
        cal[key] = calibrate(args.trace, kc, args.util, args.anchor)
        # 多条 trace 并行校准时，写回前合并其他进程已写入的键
        if os.path.exists(cal_path):
            cal = {**json.load(open(cal_path)), **cal}
        json.dump(cal, open(cal_path, "w"), indent=2)
    rate = cal[key]
    print(f"[{args.trace}] full-scale session rate = {rate:.4f}/s ({rate * 3600:.0f}/h)", flush=True)

    jobs = []
    if args.pool_tb:
        pols = args.policies.split(",") if args.policies else MRC_POLICIES
        for tb in (float(x) for x in args.pool_tb.split(",")):
            jobs += [(args.trace, p, tb * 1e12 / P.dram_kv_total, args.k, rate, sd) for p in pols for sd in seeds]
    elif args.only in ("", "mrc"):
        jobs += [(args.trace, p, f, args.k, rate) for f in MRC_F for p in MRC_POLICIES]
    if not args.pool_tb and args.only in ("", "abl"):
        jobs += [(args.trace, p, f, args.k, rate) for f in ABL_F for p in ABL_POLICIES]
    if not args.pool_tb and args.only in ("", "strip"):
        jobs += [(args.trace, "strip:" + p, f, args.k, rate) for f in STRIP_F for p in STRIP_POLICIES]
    if not args.pool_tb and args.only in ("", "hbm360"):
        jobs += [(args.trace, "hbm360:" + p, f, args.k, rate) for f in HBM360_F for p in HBM360_POLICIES]
    out_path = os.path.join(args.out, f"{args.trace}.json")
    done = json.load(open(out_path)) if os.path.exists(out_path) else []
    have = {(d["policy"], round(d["f"], 6), d["k"], d.get("seed", 1)) for d in done}
    jobs = [j for j in jobs if (j[1], round(j[2], 6), j[3], j[5] if len(j) > 5 else 1) not in have]
    # 慢的（大容量、kvcm）先跑
    jobs.sort(key=lambda j: (-j[2], "local" in j[1]))
    with Pool(args.jobs) as pool:
        for s in pool.imap_unordered(run_one, jobs):
            done.append(s)
            json.dump(done, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main()
