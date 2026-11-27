"""回放器核心语义的小测试（合成 workload，秒级）。

    PYTHONPATH=kvcm/sim python3 -m pytest kvcm/sim/tests -q
"""

import math

import numpy as np

from kvcm_sim.replay import Config, Sim, Tier, pref
from kvcm_sim.traces import Req, Session, Workload, _fill_next


def _wl(sessions):
    for s in sessions:
        _fill_next(s)
    return Workload("synthetic", 512, sessions)


def _chain_session(sid, n_turns, pages_per_turn, gap, svc=1.0):
    s = Session(sid)
    base = np.arange(1, 1 + n_turns * pages_per_turn, dtype=np.int64) * 1000 + sid
    for i in range(n_turns):
        npg = (i + 1) * pages_per_turn
        s.reqs.append(Req(t=i * (gap + svc), stream=0, n_in=npg * 512, pages=base[:npg].copy(), svc=svc))
    return s


def test_pref_binary_search_matches_linear():
    d = {k: 0 for k in range(10)}
    assert pref(d, list(range(20))) == 10
    assert pref(d, [99] + list(range(5))) == 0
    assert pref(d, list(range(5))) == 5
    assert pref(d, []) == 0


def test_lru_reverse_touch_keeps_prefix_closed():
    sim = Sim(Config(hbm_pages=8, dram_pages=8, policy="local_lru", n_inst=1), _wl([]), 1.0, 1.0, 0.0)
    t = Tier(4)
    sim.use(t, [1, 2, 3])
    sim.use(t, [1, 2, 3, 4, 5])  # 容量 4：最旧的是上一轮叶子之前插入的页，前缀 1,2 不能先丢
    assert 1 in t.all and 2 in t.all
    assert pref(t.all, [1, 2, 3, 4, 5]) >= 2


def test_single_stream_infinite_capacity_hits_ideal():
    wl = _wl([_chain_session(0, 6, 4, gap=5.0)])
    sim = Sim(Config(hbm_pages=10_000, dram_pages=10_000, policy="shared_lru", n_inst=2), wl,
              rate=0.01, horizon=2000.0, warmup=0.0, seed=0)
    rec = sim.run()
    assert len(rec["t"]) > 0
    assert (rec["hit"] == rec["ideal"]).all()


def test_oracle_keepalive_beats_lru_under_pressure():
    # 两条长会话交替，中间插入大量一次性请求冲刷缓存：LRU 丢掉会话前缀，oracle 保活能留住
    sessions = [_chain_session(i, 8, 2, gap=30.0) for i in range(2)]
    for j in range(2, 40):
        s = Session(j)
        s.reqs.append(Req(t=0.0, stream=0, n_in=6 * 512,
                          pages=np.arange(1, 7, dtype=np.int64) * 7919 + j * 10**6, svc=1.0))
        sessions.append(s)
    wl = _wl(sessions)
    common = dict(hbm_pages=8, dram_pages=24, n_inst=1, pool_pin_frac=0.5, pin_tc_mult=math.inf)
    res = {}
    hbm = {}
    for name, kw in {"lru": dict(policy="shared_lru"),
                     "oracle": dict(policy="kvcm", hook="oracle", keepalive=True, zeroref=True)}.items():
        sim = Sim(Config(**common, **kw), wl, rate=0.2, horizon=2000.0, warmup=0.0, seed=3)
        sim.pool.tc = 1.0  # 跳过在线估计：直接允许钉
        rec = sim.run()
        res[name] = rec["hit"].sum() / rec["n_in"].sum()
        hbm[name] = sim.hbm[0]
    assert res["oracle"] > res["lru"], res
    # KVCM 只管池：HBM 里没有钉/引用类页，全部是引擎 LRU
    assert not hbm["oracle"].pinned and not hbm["oracle"].l1
    assert all(c == 0 for c in hbm["oracle"].all.values())


def test_six_node_params_scale_hbm_pool_and_npu():
    from kvcm_sim.params import Params

    p = Params(n_nodes=6)
    assert p.n_npu == 96 and p.n_inst == 6
    assert math.isclose(p.hbm_kv_per_inst / 1e9, 296.1, rel_tol=1e-3)
    assert math.isclose(p.hbm_kv_per_inst * p.n_inst / 1e12, 1.7766, rel_tol=1e-3)
    assert math.isclose(p.dram_kv_total / 1e12, 6.0)
    # 池容量按 TB 给出时折算成标称倍数：0.18 TB / 6 TB = 0.03
    assert math.isclose(0.18e12 / p.dram_kv_total, 0.03)


def test_closure_ceiling_thresholds_and_top():
    from kvcm_sim.report_n6 import ceiling, closure

    def rec(pol, pool, seed, hit, ideal):
        return {"policy": pol, "pool_tb": pool, "seed": seed, "k": 32, "hit_rate": hit, "ideal_hit_rate": ideal}

    rows = []
    for seed, ideal in ((1, 0.96), (2, 0.94)):
        # 1 TB：LRU 缺口 6 pt，理想 hook 补回一半，现实 hook 反而低 1 pt
        rows += [rec("shared", 1.0, seed, ideal - 0.06, ideal), rec("kvcm-oracle", 1.0, seed, ideal - 0.03, ideal),
                 rec("kvcm-real", 1.0, seed, ideal - 0.07, ideal)]
        # 6 TB：缺口 0.3 pt → 只作参考；12 TB：三者都在上限 → 触顶，比值不给
        rows += [rec("shared", 6.0, seed, ideal - 0.003, ideal), rec("kvcm-oracle", 6.0, seed, ideal - 0.001, ideal),
                 rec("kvcm-real", 6.0, seed, ideal - 0.003, ideal)]
        rows += [rec(p, 12.0, seed, ideal, ideal) for p in ("shared", "kvcm-oracle", "kvcm-real")]
    c = ceiling(rows)
    assert math.isclose(c[0], 0.95) and math.isclose(c[1], 0.94) and math.isclose(c[2], 0.96)
    out = {r["pool_tb"]: r for r in closure(rows)}
    r1 = out[1.0]
    assert r1["status"] == "ok" and r1["n_seeds"] == 2
    assert math.isclose(r1["gap"]["shared"], 0.06) and math.isclose(r1["gap"]["kvcm-oracle"], 0.03)
    assert math.isclose(r1["closure_ideal"][0], 0.5) and math.isclose(r1["closure_real"][0], -1 / 6)
    assert out[6.0]["status"] == "ref" and math.isclose(out[6.0]["closure_ideal"][0], 2 / 3)
    assert out[12.0]["status"] == "top" and out[12.0]["closure_ideal"] is None and out[12.0]["closure_real"] is None
