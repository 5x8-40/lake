"""池局部性扩展的小测试（合成 Zipf workload，秒级）。

    PYTHONPATH=kvcm/sim python3 -m pytest kvcm/sim/tests -q
"""

from kvcm_sim.locality import LocCfg, LocSim
from kvcm_sim.params import DEFAULT as P
from kvcm_sim.replay import Config, Sim
from kvcm_sim.traces import make_zipf


def _cfg():
    return Config(hbm_pages=200, dram_pages=600, policy="shared_lru", n_inst=4)


def _wl():
    return make_zipf(0.8, 60, doc_pages=8, n_tmpl=50)


def test_hash_single_copy_keeps_base_hit_rate():
    wl = _wl()
    base = Sim(_cfg(), wl, rate=2.0, horizon=600.0, warmup=0.0, seed=1).run()
    for cost in ("flat", "topo", "contend"):
        rec = LocSim(_cfg(), LocCfg(cost=cost, k=1), wl, rate=2.0, horizon=600.0, warmup=0.0,
                     params=P, seed=1).run()
        assert rec["hit"].sum() == base["hit"].sum(), cost


def test_flow_counters_drain_and_replicas_cost_capacity():
    wl = _wl()
    cfg = Config(hbm_pages=200, dram_pages=3000, policy="shared_lru", n_inst=4)
    sim = LocSim(cfg, LocCfg(placement="local", repl="passive", k=1), wl, rate=2.0, horizon=600.0,
                 warmup=0.0, params=P, seed=1)
    sim.run()
    assert sum(sim.egress) == sum(sim.ingress) == sum(sim.h2d) == 0
    reps = [k for k in sim.pool.all if isinstance(k, tuple)]
    assert len(sim.pool.all) <= 3000
    assert sim.n_rep > 0 and len(reps) == sim.n_rep
    assert all(kk in sim.pool.all for kk, _ in reps)
