"""热点复制增益场景【注入】：少数节点持有被反复读的共享长文档，其余节点网卡有余量。

合成负载（不改原有 trace）：每个会话从 n_docs 篇共享长文档里均匀选一篇（256 页 = 128K token），
之后 1–4 轮问答，每轮追加 2 页私有问题与 1 页输出。热集合按集群 HBM 的倍数取，热文档在 HBM 里
互相挤出，读真正落到池上（之前的 Zipf 实验热文档被 HBM 吸收，热点复制从不触发）。

热点集中：写本机放置下，热文档页的主副本固定在 n_hot 个节点（等价于这几台节点上的加载器写入）；
扁平哈希池按键条带化，不受影响。到达率按“全网池读需求 ≈ 全网出口容量的 DEMAND”标定，所以
n_hot 小时热节点出口需求 > 容量，而全网总需求 < 总容量。

    LOC_H=10800,3600 PYTHONPATH=. python3 -m kvcm_sim.hotspot --jobs 4
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
from .locality_run import K, OUT, VARIANTS, _cfg
from .params import DEFAULT as P0
from .run import summarize
from .traces import Req, Session, Workload, _fill_next, _fill_out
from . import locality_run as _lr

DOC_PAGES = 256
HBM_PAGES_CLUSTER = P0.n_inst * P0.pages(P0.hbm_kv_per_inst, 512) / K
DEMAND = 0.30                       # 全网池读需求 / 全网 RoCE 出口容量（标定目标）
HOT_NODES = (1, 2, 3, 4, 7, 14)
SET_X = (0.5, 2.0)                  # 热集合 = 集群 HBM 的倍数
HS_VARS = ("flat", "local", "local+passive", "local+hot", "local+ttft", "kvcm+route", "kvcm+loc")
EXTRA = {
    "local+hot": ("shared", dict(placement="local", repl="hot")),
    "kvcm+route": ("kvcm-real", dict(placement="local", route="ttft")),
}
NAMES = {
    "flat": "扁平哈希池",
    "local": "写本机",
    "local+passive": "写本机 + 被动副本（Mooncake）",
    "local+hot": "写本机 + 热点复制（TokenLake）",
    "local+ttft": "只路由（共享池 + TTFT 路由，不复制）",
    "kvcm+route": "只路由（KVCM 保留 + TTFT 路由，不复制）",
    "kvcm+loc": "KVCM（热页定向副本 + TTFT 路由）",
}


def make_hotspot(n_docs: int, n_tmpl: int = 4000, page_tokens: int = 512, seed: int = 5) -> Workload:
    rng = np.random.default_rng(seed)
    docs = rng.integers(0, n_docs, size=n_tmpl)
    svc = 2.0 + 512 / 40.0
    sessions = []
    for sid in range(n_tmpl):
        d = int(docs[sid])
        doc = np.array([hash((17, d, j)) for j in range(DOC_PAGES)], dtype=np.int64)
        priv: list[int] = []
        prev = hash((19, sid))
        t = 0.0
        reqs = []
        for _ in range(int(rng.integers(1, 5))):
            for _ in range(2):
                prev = hash((prev, len(priv)))
                priv.append(prev)
            pages = np.concatenate([doc, np.array(priv, dtype=np.int64)])
            sh = np.zeros(len(pages), dtype=bool)
            sh[:DOC_PAGES] = True
            reqs.append(Req(t=t, stream=0, n_in=len(pages) * page_tokens + 100, pages=pages, svc=svc,
                            shared=sh, n_out=512))
            prev = hash((prev, 13))
            priv.append(prev)
            t += svc + float(rng.lognormal(math.log(20.0), 1.0))
        s = Session(sid, reqs)
        _fill_next(s)
        _fill_out(s, page_tokens)
        sessions.append(s)
    return Workload(f"hotspot{n_docs}", page_tokens, sessions, meta={"n_docs": n_docs, "doc_pages": DOC_PAGES})


class HotSim(LocSim):
    def __init__(self, *a, n_docs: int, n_hot: int, **kw):
        super().__init__(*a, **kw)
        self.pin = {hash((17, d, j)): d % n_hot for d in range(n_docs) for j in range(DOC_PAGES)}

    def use(self, t, keys):
        super().use(t, keys)
        if t is self.pool and self.loc.placement == "local":
            a, pin, home = t.all, self.pin, self.home
            for k in keys:
                p = pin.get(k)
                if p is not None and k in a:
                    home[k] = p


def _variant(v):
    return EXTRA.get(v) or VARIANTS[v]


def run_one(job):
    var, n_hot, sx, rate = job
    n_docs = max(1, int(round(sx * HBM_PAGES_CLUSTER / DOC_PAGES)))
    pol, lov = _variant(var)
    loc = LocCfg(k=K, transport="roce", cost="queue", **lov)
    wl = make_hotspot(n_docs)
    t0 = time.time()
    sim = HotSim(_cfg(P0, 1.0, pol), loc, wl, rate=rate / K, horizon=_lr.HORIZON, warmup=_lr.WARMUP,
                 params=P0, seed=1, n_docs=n_docs, n_hot=n_hot)
    rec = sim.run()
    s = summarize(rec, K, sim)
    m = {k: np.asarray(v) for k, v in sim.m.items()}
    ttft, pb = m["ttft"], m["pool_b"]
    win = _lr.HORIZON - _lr.WARMUP
    util = sim.eg_bytes / (win * sim.B_r)
    copy_u = sim.copy_bytes / (win * sim.B_r)
    hot = list(range(n_hot))
    s.update(
        variant=var, n_hot=n_hot, set_x=sx, n_docs=n_docs, rate_full=rate, k=K, horizon=_lr.HORIZON,
        ttft_mean=float(ttft.mean()), ttft_p95=float(np.percentile(ttft, 95)),
        exposed_mean_ms=float(m["exp"].mean() * 1e3), pool_req_frac=float((pb > 0).mean()),
        local_byte_share=float(m["loc_b"].sum() / max(pb.sum(), 1)),
        hot_egress_util=float(util[hot].max()), egress_util_max=float(util.max()),
        cold_egress_util=float(util[n_hot:].mean()) if n_hot < sim.n else float("nan"),
        net_demand=float((sim.eg_bytes.sum() + sim.copy_bytes) / (sim.n * win * sim.B_r)),
        pool_read_gbps=float((sim.eg_bytes.sum() + sim.lo_bytes.sum()) / win * K / 1e9),
        copy_gb_per_s=float(sim.copy_bytes / win * K / 1e9), copy_util=float(copy_u),
        replica_share=float(np.mean(sim.rep_samples)) if sim.rep_samples else 0.0,
        wall_s=round(time.time() - t0, 1),
    )
    print(f"[hot] {var:14s} h={n_hot:<2d} x={sx:g} hit={s['hit_rate']:.4f} mean={s['ttft_mean']:.3f} "
          f"p95={s['ttft_p95']:.3f} hotEg={s['hot_egress_util']:.2f} net={s['net_demand']:.2f} "
          f"pool={s['pool_req_frac']:.2f} rep={s['replica_share']:.3f} cp={s['copy_gb_per_s']:.1f} "
          f"npu={s['npu_busy']:.1f} ({s['wall_s']}s)", flush=True)
    return s


def calibrate(sx: float) -> float:
    """按扁平哈希池上的池读字节率标定会话到达率，使全网池读需求 ≈ DEMAND × 全网出口容量。"""
    rate = 30.0
    for _ in range(2):
        s = run_one(("flat", 14, sx, rate))
        cap = P0.n_inst * BW["roce"][1]
        rate *= DEMAND * cap / max(s["pool_read_gbps"], 1e-6)
    return rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--sets", default="")
    ap.add_argument("--hot", default="")
    args = ap.parse_args()
    sets = [float(x) for x in args.sets.split(",")] if args.sets else list(SET_X)
    hots = [int(x) for x in args.hot.split(",")] if args.hot else list(HOT_NODES)
    path = os.path.join(OUT, "hotspot.json")
    cal_p = os.path.join(OUT, "calibration_hotspot.json")
    cal = json.load(open(cal_p)) if os.path.exists(cal_p) else {}
    res = json.load(open(path)) if os.path.exists(path) else []
    done = {(r["variant"], r["n_hot"], r["set_x"]) for r in res}
    jobs = []
    for sx in sets:
        key = f"{sx:g}@k{K}@h{int(_lr.HORIZON)}"
        if key not in cal:
            cal[key] = calibrate(sx)
            json.dump(cal, open(cal_p, "w"), indent=2)
            print(f"[hot] x={sx:g} rate={cal[key] * 3600:.0f} 会话/h", flush=True)
        for h in hots:
            for v in HS_VARS:
                if (v, h, sx) not in done and not (v == "flat" and h != hots[0]):
                    jobs.append((v, h, sx, cal[key]))
    with Pool(args.jobs) as pool:
        for s in pool.imap_unordered(run_one, jobs):
            res.append(s)
            json.dump(res, open(path, "w"), indent=1)


if __name__ == "__main__":
    main()
