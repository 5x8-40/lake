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
    common = dict(hbm_pages=8, dram_pages=24, n_inst=1, hbm_keep_max_gap=-1.0,
                  pool_pin_frac=0.5, pin_tc_mult=math.inf)
    res = {}
    for name, kw in {"lru": dict(policy="shared_lru"),
                     "oracle": dict(policy="kvcm", hook="oracle", keepalive=True)}.items():
        sim = Sim(Config(**common, **kw), wl, rate=0.2, horizon=2000.0, warmup=0.0, seed=3)
        sim.pool.tc = 1.0  # 跳过在线估计：直接允许钉
        rec = sim.run()
        res[name] = rec["hit"].sum() / rec["n_in"].sum()
    assert res["oracle"] > res["lru"], res
