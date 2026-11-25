import numpy as np

from pd import fluid as F
from pd.pd_params import DEFAULT as Q


def _dm(P_nodes, D_nodes):
    P = np.asarray(P_nodes, float) * Q.npu_per_node * Q.rho_p
    D = np.asarray(D_nodes, float) * Q.kv_cap_node
    return F.Demand(np.arange(len(P)) * Q.window, P, D)


def test_feasible_pure_roles():
    assert F.feasible(6, 8, 0, 6 * 16 * Q.rho_p, 8 * Q.kv_cap_node, Q)
    assert not F.feasible(5, 9, 0, 6 * 16 * Q.rho_p, 8 * Q.kv_cap_node, Q)


def test_mixed_node_prefill_cap():
    p = Q.f_mixed * 16
    assert F.feasible(0, 0, 1, p, 0.0, Q)
    assert not F.feasible(0, 0, 1, p * 1.01, 0.0, Q)


def test_stationary_no_dynamic_gain():
    dm = _dm([5] * 50, [7] * 50)
    zero = F.Tier("z", "", 0, 0, 0, 1.0)
    s = F.goodput(lambda a: F.static_best(dm, Q, a, mixed=False), 0.0)
    d = F.goodput(lambda a: F.dynamic(dm, Q, a, zero), 0.0)
    assert abs(s - d) < 1e-3


def test_mix_drift_dynamic_beats_static():
    t = np.arange(96)
    share = 0.5 + 0.2 * np.sin(2 * np.pi * t / 96)
    dm = _dm(12 * share, 12 * (1 - share))
    zero = F.Tier("z", "", 0, 0, 0, 1.0)
    s = F.goodput(lambda a: F.static_best(dm, Q, a, mixed=False), 0.0)
    d = F.goodput(lambda a: F.dynamic(dm, Q, a, zero), 0.0)
    assert d > s * 1.15
    assert abs(F.closed_form_gain(share) - 0.4) < 0.01


def test_switch_cost_never_helps():
    t = np.arange(96)
    share = 0.5 + 0.2 * np.sin(2 * np.pi * t / 24)
    dm = _dm(12 * share, 12 * (1 - share))
    cheap = F.Tier("c", "", 2, 2, 0, 1.0)
    slow = F.Tier("s", "", 1500, 2, 0.5, 1.0)
    g_c = F.goodput(lambda a: F.dynamic(dm, Q, a, cheap), 0.0)
    g_s = F.goodput(lambda a: F.dynamic(dm, Q, a, slow), 0.0)
    assert g_c >= g_s - 1e-6


def _slices(dm, k=5):
    return F.Demand(np.arange(len(dm.P) * k) * (Q.window / k), np.repeat(dm.P, k), np.repeat(dm.D, k))


def test_planner_stationary_never_switches():
    rng = np.random.default_rng(0)
    dm = _dm(5 + 0.2 * rng.standard_normal(96), 7 + 0.2 * rng.standard_normal(96))
    cheap = F.Tier("c", "", 2, 2, 0, 1.0)
    for t_win in (300.0, 600.0):
        assert F.planner(dm, _slices(dm), Q, 1.0, cheap, t_win=t_win)["switches"] == 0


def test_planner_follows_step_and_recompute_costs():
    nP = np.r_[[4.0] * 48, [8.0] * 48]
    dm = _dm(nP, 11.0 - nP)
    cheap = F.Tier("c", "", 2, 2, 0, 1.0)
    kill = F.Tier("k", "", 2, 2, 0, 1.0, recompute=6000.0)
    r = F.planner(dm, _slices(dm), Q, 1.0, cheap, t_win=600.0)
    assert r["switches"] > 0 and r["recompute_npu_s"] == 0
    rk = F.planner(dm, _slices(dm), Q, 1.0, kill, t_win=600.0)
    assert rk["recompute_npu_s"] > 0
    g = F.goodput(lambda a: F.dynamic(dm, Q, a, cheap, oracle=False), 0.0)
    gk = F.goodput(lambda a: F.dynamic(dm, Q, a, kill, oracle=False), 0.0)
    assert gk <= g + 1e-6
