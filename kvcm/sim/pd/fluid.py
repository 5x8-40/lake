"""E1：窗口级流体模型。

1. 需求序列：按场景的会话到达率（Poisson，非齐次用 thinning）抽会话，会话内按 trace 原时间回放；
   每个窗口得到 prefill 需求 P(w)（A3 NPU，平均占用）和 decode KV 占用 D(w)（字节，按 D 节点每步耗时）。
2. 容量：节点是整数（14 个，每个 16 NPU）。
   - P 节点：prefill 利用率 ≤ rho_p；
   - D 节点：KV 占用 ≤ hbm_kv_per_node × kv_fill（decode 在我们的参数下受 HBM 容量约束，见 params.md）；
   - 混部节点：prefill 时间占比 f ≤ f_mixed，decode 每步被拉长为 t_step/(1−f)，KV 占用同比放大。
3. 策略：静态最优 P:D、静态全混部、静态最优 P/D/混部组合、动态 P:D（oracle / 滞后一窗）、动态三态（oracle）。
   动态策略的切换损失按 E4 的切换耗时扣除（跨窗口结转）。
4. goodput 代理：把负载整体乘 α，过载窗口占比 ≤ overload_tol 的最大 α。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import product

import numpy as np

from .pd_params import PDParams
from .workload import Sessions

DAY = 86400.0


@dataclass
class Scenario:
    name: str
    label: str
    injected: bool
    pops: list            # [(Sessions, rate_fn(t) -> sessions/s)]
    warmup: float = 8 * 3600
    horizon: float = 32 * 3600
    steps: tuple = ()     # 阶跃时刻（测量段内，秒）


@dataclass
class Demand:
    t: np.ndarray         # 窗口起点（测量段，秒）
    P: np.ndarray         # prefill 平均占用 NPU
    D: np.ndarray         # decode KV 平均占用字节（按 t_step）
    parts: dict = field(default_factory=dict)


def build_demand(sc: Scenario, q: PDParams, seed: int = 1, chunk: int = 4000) -> Demand:
    rng = np.random.default_rng(seed)
    W = q.window
    nsec = int(sc.horizon)
    nwin = int(sc.horizon // W)
    P = np.zeros(nwin)
    dk = np.zeros(nsec + 2)
    parts = {}
    for si, (S, rate) in enumerate(sc.pops):
        grid = np.arange(0, sc.horizon, 60.0)
        rmax = max(rate(grid).max(), 1e-12)
        n = rng.poisson(rmax * sc.horizon)
        st = np.sort(rng.uniform(0, sc.horizon, n))
        st = st[rng.uniform(0, 1, n) < rate(st) / rmax]
        sid = rng.integers(0, S.n, len(st))
        kvb = S.kv_bytes(q.kv_bytes_per_token)
        Pp = np.zeros(nwin)
        dkp = np.zeros(nsec + 2)
        for c in range(0, len(st), chunk):
            s0, ii = st[c:c + chunk], sid[c:c + chunk]
            lens = S.off[ii + 1] - S.off[ii]
            ridx = np.repeat(S.off[ii] - np.cumsum(np.r_[0, lens[:-1]]), lens) + np.arange(lens.sum())
            ta = np.repeat(s0, lens) + S.t[ridx]
            m = ta < sc.horizon
            ridx, ta = ridx[m], ta[m]
            np.add.at(Pp, (ta // W).astype(int), S.p[ridx])
            te = np.minimum(ta + S.out[ridx] * q.t_step, sc.horizon)
            np.add.at(dkp, ta.astype(int), kvb[ridx])
            np.add.at(dkp, te.astype(int), -kvb[ridx])
        P += Pp
        dk += dkp
        parts[S.name] = (Pp, dkp)
    occ = np.cumsum(dk)[:nsec]
    D = occ[: nwin * int(W)].reshape(nwin, int(W)).mean(axis=1)
    w0 = int(sc.warmup // W)
    out = Demand(np.arange(nwin)[w0:] * W - sc.warmup, P[w0:] / W, D[w0:])
    for k, (Pp, dkp) in parts.items():
        o = np.cumsum(dkp)[:nsec][: nwin * int(W)].reshape(nwin, int(W)).mean(axis=1)
        out.parts[k] = (Pp[w0:] / W, o[w0:])
    return out


# ---------------------------------------------------------------- 需求 → 节点
def need(dm: Demand, q: PDParams, alpha: float, p_mult: float = 1.0):
    """各窗口按纯 P / 纯 D 计的节点需求（连续值）。"""
    nP = alpha * p_mult * dm.P / (q.npu_per_node * q.rho_p)
    nD = alpha * dm.D / q.kv_cap_node
    return nP, nD


def feasible(nP, nD, nM, P, D, q: PDParams):
    """给定节点组合（可为向量，按窗口），判断各窗口是否承得住。P 为 NPU 需求，D 为 KV 字节（按 t_step）。

    prefill 先放 P 节点（≤ rho_p），溢出部分给混部节点（≤ f_mixed）；混部节点 decode 每步拉长 1/(1−f)。
    """
    npu = q.npu_per_node
    p_left = np.maximum(0.0, P - nP * npu * q.rho_p)
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(nM > 0, p_left / (np.maximum(nM, 1e-9) * npu), np.where(p_left > 1e-9, np.inf, 0.0))
    ok_p = f <= q.f_mixed + 1e-12
    cap_d = nD * q.kv_cap_node
    d_left = np.maximum(0.0, D - cap_d)
    stretch = 1.0 / np.maximum(1e-9, 1.0 - np.minimum(f, 0.999))
    ok_d = d_left * stretch <= nM * q.kv_cap_node + 1e-6
    return ok_p & ok_d


def combos(n, mixed=True):
    out = []
    for a in range(n + 1):
        for b in range(n + 1 - a):
            c = n - a - b
            if c and not mixed:
                continue
            out.append((a, b, c))
    return out


# ---------------------------------------------------------------- 策略
def static_best(dm, q, alpha, p_mult=1.0, mixed=True, only=None):
    P, D = alpha * p_mult * dm.P, alpha * dm.D
    best = None
    for c in (only or combos(q.n_nodes, mixed)):
        ok = feasible(c[0], c[1], c[2], P, D, q)
        r = 1.0 - ok.mean()
        if best is None or r < best[0]:
            best = (r, c)
    c = best[1]
    return {"overload": best[0], "alloc": c, "switches": 0, "ov": ~feasible(c[0], c[1], c[2], P, D, q)}


@dataclass
class Tier:
    name: str
    label: str
    t_dp: float           # D→P：新角色可用前的耗时
    t_pd: float           # P→D
    old_keep: float       # 切换期间旧角色保留的能力比例（排空 0.5；经池丢弃 0）
    p_mult: float         # prefill 需求乘数（host 权重副本挤占池 → 命中下降）


def _alloc_choices(q, mixed):
    return combos(q.n_nodes, mixed)


def dynamic(dm, q, alpha, tier: Tier, mixed=False, oracle=True, headroom=1.05):
    """逐窗口选组合；切换耗时按节点·秒从后续窗口扣除。

    oracle：已知未来需求，选承得住“本窗口到切换完成”这段峰值、且切换最少的组合；
    lag：只看上一窗口需求 × headroom，当前组合在上一窗口已过载或余量 < 5% 才换。
    """
    W = q.window
    P, D = alpha * tier.p_mult * dm.P, alpha * dm.D
    choices = _alloc_choices(q, mixed)
    C = np.array(choices, dtype=float)
    cur = np.array(static_best(dm, q, alpha, tier.p_mult, mixed)["alloc"], dtype=float)
    pend = []                 # [(role_idx, start_offset_s, dur_s, sign)] 结转到后续窗口的能力增减
    over = 0
    ov = np.zeros(len(P), bool)
    sw = 0
    lost_ns = 0.0
    look = int(math.ceil(max(tier.t_dp, tier.t_pd) / W))
    for w in range(len(P)):
        if oracle:
            # 前瞻：组合要承得住接下来 look 个窗口（切换完成前）的需求峰值
            Pw, Dw = P[w:w + look + 1].max(), D[w:w + look + 1].max()
            ok = feasible(C[:, 0], C[:, 1], C[:, 2], Pw, Dw, q)
            # 先按“扣掉切换损失后仍承得住”挑，再按切换节点数最少
            cand = []
            for j in np.nonzero(ok)[0]:
                moved = np.abs(C[j] - cur).sum() / 2
                cand.append((moved, j))
            tgt = C[min(cand)[1]] if cand else cur
        else:
            if w == 0:
                tgt = cur
            else:
                Pl, Dl = P[w - 1] * headroom, D[w - 1] * headroom
                ok_cur = feasible(cur[0], cur[1], cur[2], Pl, Dl, q)
                if ok_cur:
                    tgt = cur
                else:
                    ok = feasible(C[:, 0], C[:, 1], C[:, 2], Pl, Dl, q)
                    cand = [(np.abs(C[j] - cur).sum() / 2, j) for j in np.nonzero(ok)[0]]
                    tgt = C[min(cand)[1]] if cand else cur
        delta = tgt - cur
        if np.any(delta != 0):
            gain = np.maximum(delta, 0)
            loss = np.maximum(-delta, 0)
            n_mv = gain.sum()
            sw += int(n_mv)
            # 新角色在 t_sw 后才可用；旧角色在切换期间保留 old_keep
            t_sw = tier.t_dp if (gain[0] > 0 or gain[2] > 0) else tier.t_pd
            for r in range(3):
                if gain[r]:
                    pend.append([r, 0.0, t_sw, -gain[r]])
                if loss[r] and tier.old_keep > 0:
                    pend.append([r, 0.0, t_sw, loss[r] * tier.old_keep])
            lost_ns += n_mv * t_sw * (1 - tier.old_keep)
        cur = tgt
        # 本窗口有效节点数 = cur + 结转修正（按时间平均）
        eff = cur.copy()
        nxt = []
        for r, s, d, amt in pend:
            span = min(W, s + d) - s if s < W else 0.0
            if span > 0:
                eff[r] += amt * span / W
            if s + d > W:
                nxt.append([r, max(0.0, s - W), d - max(0.0, W - s), amt])
        pend = nxt
        eff = np.maximum(eff, 0)
        if not feasible(eff[0], eff[1], eff[2], P[w], D[w], q):
            over += 1
            ov[w] = True
    n = len(P)
    return {"overload": over / n, "switches": sw, "lost_node_s": lost_ns, "ov": ov}


def goodput(fn, tol, lo=0.05, hi=4.0, it=26):
    """过载窗口占比 ≤ tol 的最大负载倍数 α（二分）。"""
    if fn(lo)["overload"] > tol:
        return 0.0
    for _ in range(it):
        mid = 0.5 * (lo + hi)
        if fn(mid)["overload"] <= tol:
            lo = mid
        else:
            hi = mid
    return lo


def p_share(dm, q, alpha=1.0):
    nP, nD = need(dm, q, alpha)
    return nP / (nP + nD)


def closed_form_gain(share):
    """总需求不变时，静态最优配比相对理想动态的多余容量 = s_max − s_min。"""
    return float(np.max(share) - np.min(share))


def sinus(base, amp, period=DAY, phase=0.0):
    return lambda t: base * (1.0 + amp * np.sin(2 * math.pi * (np.asarray(t) / period) + phase))


def mix_rates(r_tot, a_fn, d_a, d_b):
    """总节点需求 ≈ r_tot（节点），A 的需求占比 a(t)；d_a/d_b 是两类会话各自的平均节点·秒。"""
    ra = lambda t: r_tot * a_fn(t) / d_a
    rb = lambda t: r_tot * (1.0 - a_fn(t)) / d_b
    return ra, rb


def step_fn(lo, hi, ups, downs, ramp=300.0):
    """分段常数 + 线性斜坡：在 ups 时刻 lo→hi，downs 时刻 hi→lo（绝对时间，秒），斜坡 ramp 秒。"""
    def f(t):
        t = np.asarray(t, float)
        x = np.zeros_like(t)
        for u, d in zip(ups, downs):
            x += np.clip((t - u) / ramp, 0, 1) - np.clip((t - d) / ramp, 0, 1)
        return lo + (hi - lo) * x
    return f


def per_step(ov, t, steps, W):
    """每次阶跃后（到下一次阶跃前）的过载窗口数与首段连续过载时长（分钟）。"""
    rows = []
    bounds = list(steps) + [t[-1] + W]
    for k, s in enumerate(steps):
        m = (t >= s) & (t < bounds[k + 1])
        seg = ov[m]
        run = 0
        for x in seg:
            if not x:
                break
            run += 1
        # 首段：阶跃后第一次出现过载起的连续过载
        first = 0
        if seg.any():
            i0 = int(np.argmax(seg))
            for x in seg[i0:]:
                if not x:
                    break
                first += 1
        rows.append({"step_h": s / 3600, "over_windows": int(seg.sum()), "over_min": float(seg.sum() * W / 60),
                     "first_run_min": float(first * W / 60)})
    return rows
