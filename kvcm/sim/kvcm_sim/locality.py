"""池块位置、近远端读、网卡争用与副本：在共享池回放器上加的局部性层。

对应 Project 文档《KVCM 池化的近远端、冷热与争用》§4：
- 池块有所在节点（home）：`hash` 按键哈希条带化（扁平池的常见放置），`local` 写到写入者
  所在节点（Mooncake `prefer_alloc_in_same_node`）。
- 池→HBM 读：本机走 H2D，跨机走 FabricMem 或 RoCE（D8a §9.1）。`flat` = 一律按跨机带宽、
  无争用（主线仿真的口径）；`topo` = 分本机/跨机；`contend` = 再加每节点共享：
  `B_eff = B / (1 + n_inflight)`（NetKV），跨机流同时占源节点出口与读者节点入口，本机流占本机
  拷贝引擎。一个请求的跨机页可能散在多个源节点上并行读，但总量受读者入口带宽约束。
- 计入 TTFT 的读时间：`max(0, τ − W)`。`overlap=False` 时读在 prefill 拿到算力后才开始
  （W 不掩盖，SGLang HiCache / vLLM connector 的默认形态）；`overlap=True` 时读从到达就开始，
  和排队重叠（Evolving Infra）。
- 每实例 16 个 prefill 槽（1 NPU/请求，时长 = A3 计算时间 + 未掩盖读时间）。
- 副本占池容量、参与 LRU：`passive` 跨机读后读者留一份（Mooncake 论文 / Evolving Infra
  “拉取留副本”）；`hot` 热页按目标副本数复制到出口最闲的节点，读时两选一（TokenLake）；
  `kvcm` 热页只在被路由到的实例留副本（D8a §6.5 copy 到路由目标，`r_target` 门控）。
- 路由：`sglang`（主线规则）、`load`（最短队列）、`ttft`（按预测 TTFT 选点，Mooncake
  Conductor / Evolving Infra：W + 未掩盖读 + 计算）。

切片口径（时间放大）：1/k 切片的到达率是全规模的 1/k，若槽数、带宽不变，排队与争用会
消失。这里把每个请求的计算时长和每节点带宽都放大 k 倍（时长 ×k、带宽 ÷k）：由 Little
定律，在途读流数、槽占用与全规模相同；所有时间量最后除以 k 还原为全规模秒。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

import numpy as np

from .replay import Sim, pref

XSTART, XEND = 16, 17


@dataclass
class LocCfg:
    transport: str = "fabric"     # fabric | roce
    cost: str = "contend"         # flat | topo | contend
    overlap: bool = False
    placement: str = "hash"       # hash | local
    repl: str = "none"            # none | passive | hot | kvcm
    route: str = "sglang"         # sglang | load | ttft
    k: int = 32
    slots: int = 16               # 每实例 prefill 槽（1 NPU/请求）
    rho: float = 0.5              # 副本目标利用率（D8a §6.5 ρ_target）
    heat_halflife: float = 300.0  # 热度 EWMA 半衰期（仿真秒）
    max_rep: int = 14


BW = {  # 全规模 GB/s：(本机, 跨机)，D8a §9.1 A3 910C
    "fabric": (86.0, 78.0),       # FabricMem rH2D（AICPU 展开）下限
    "roce": (47.0, 23.8),         # 本机 H2D / RoCE rH2D
}


class LocSim(Sim):
    def __init__(self, cfg, loc: LocCfg, wl, rate, horizon, warmup, params, seed: int = 1):
        super().__init__(cfg, wl, rate, horizon, warmup, seed)
        assert self.shared, "局部性层只建模共享池"
        self.loc = loc
        self.P = params
        n = cfg.n_inst
        self.n = n
        self.home: dict = {}
        self.reps: dict = {}
        self.n_rep = 0
        bl, br = BW[loc.transport]
        self.B_l = bl * 1e9 / loc.k
        self.B_r = br * 1e9 / loc.k
        self.page_bytes = wl.page_tokens * params.kv_bytes_per_token
        self.egress = [0] * n
        self.ingress = [0] * n
        self.h2d = [0] * n
        self.slot = [[0.0] * loc.slots for _ in range(n)]
        self.heat: dict = {}
        self.lam_k = math.log(2) / loc.heat_halflife
        self.xseq = 1 << 60
        self.lrng = np.random.default_rng(seed + 101)
        self.warm = warmup
        self.m = {"t": [], "ttft": [], "W": [], "exp": [], "comp": [], "tau": [], "pool_b": [], "loc_b": []}
        self.eg_bytes = np.zeros(n)
        self.lo_bytes = np.zeros(n)
        self.copy_bytes = 0.0
        self.rep_samples: list = []

    # ------------------------------------------------------------ pool bookkeeping
    def use(self, t, keys):
        if t is not self.pool:
            return super().use(t, keys)
        a = t.all
        new = [k for k in keys if k not in a]
        super().use(t, keys)
        if new:
            node = self.cur_node
            hashp = self.loc.placement == "hash"
            for k in new:
                if k in a:
                    self.home[k] = (k % self.n) if hashp else node

    def _evict_one(self, t) -> bool:
        if t.l0:
            k, _ = t.l0.popitem(last=False)
        elif t.l1:
            k, _ = t.l1.popitem(last=False)
        else:
            return False
        del t.all[k]
        if t is self.pool:
            self._drop(t, k)
        return True

    def _drop(self, t, k) -> None:
        if type(k) is tuple:
            kk, node = k
            s = self.reps.get(kk)
            if s is not None:
                s.discard(node)
                if not s:
                    del self.reps[kk]
            self.n_rep -= 1
            return
        self.home.pop(k, None)
        s = self.reps.pop(k, None)
        if s:
            for node in s:
                rk = (k, node)
                c = t.all.pop(rk, None)
                if c == 0:
                    t.l0.pop(rk)
                elif c == 1:
                    t.l1.pop(rk)
                self.n_rep -= 1

    def _add_rep(self, k, node) -> bool:
        t = self.pool
        rk = (k, node)
        if rk in t.all or k not in t.all:
            return False
        for q in (t.l0, t.l1):
            if k in q:
                q.move_to_end(k)  # 正在被读的主副本不能成为腾位置的牺牲者
        if len(t.all) >= t.cap and not self._evict_one(t):
            return False
        if k not in t.all:
            return False
        t.all[rk] = 0
        t.l0[rk] = None
        self.reps.setdefault(k, set()).add(node)
        self.n_rep += 1
        return True

    def _holders(self, k):
        h = self.home.get(k, k % self.n)
        s = self.reps.get(k)
        return (h,) if not s else (h, *s)

    # ------------------------------------------------------------ cost model
    def _comp(self, n_in: int, hit_tok: int) -> float:
        """A3 prefill 计算时长（全规模秒）。"""
        P = self.P
        nk = n_in / 1024.0
        hf = min(1.0, hit_tok / max(n_in, 1))
        return (P.fit_c + (1.0 - hf) * P.model_prefill_scale * (P.fit_a * nk + P.fit_b * nk * nk)) * P.a3_per_950

    def _bw(self, src, i) -> float:
        c = self.loc.cost
        if c == "flat":
            return self.B_r
        if src == i:
            return self.B_l / (1 + self.h2d[i]) if c == "contend" else self.B_l
        return self.B_r / (1 + self.egress[src]) if c == "contend" else self.B_r

    def _pick(self, hs, i):
        if self.loc.cost == "flat":
            return -1
        if i in hs:
            return i
        if len(hs) == 1:
            return hs[0]
        if len(hs) == 2:
            a, b = hs
        else:
            a, b = self.lrng.choice(len(hs), 2, replace=False)
            a, b = hs[a], hs[b]
        return a if self.egress[a] <= self.egress[b] else b

    def _push(self, t, typ, pl) -> None:
        self.xseq += 1
        heapq.heappush(self.ev, (t, self.xseq, typ, pl))

    def _flow(self, now, t0, dur, kind, node) -> None:
        """登记一条流，只在争用模型下计数。kind：'l' 本机 H2D，'e' 源节点出口，'i' 读者节点入口。"""
        if self.loc.cost != "contend" or node < 0 or dur <= 0:
            return
        if t0 <= now:
            self._xs(kind, node, 1)
        else:
            self._push(t0, XSTART, (kind, node))
        self._push(t0 + dur, XEND, (kind, node))

    def _xs(self, kind, node, d) -> None:
        if kind == "l":
            self.h2d[node] += d
        elif kind == "e":
            self.egress[node] += d
        else:
            self.ingress[node] += d

    def _on_event(self, typ, pl, now) -> None:
        if typ == XSTART:
            self._xs(*pl, 1)
        elif typ == XEND:
            self._xs(*pl, -1)
        else:
            raise ValueError(typ)

    def _tau(self, by: dict, i: int):
        """按来源分组的页数 → (本机读时长, 跨机读时长)。跨机受每个源的出口与本机入口共同约束。"""
        pb = self.page_bytes
        if self.loc.cost == "flat":
            return 0.0, sum(by.values()) * pb / self.B_r
        t_l = by.get(i, 0) * pb / self._bw(i, i)
        rem = 0
        t_r = 0.0
        for s, c in by.items():
            if s != i:
                rem += c
                t_r = max(t_r, c * pb / self._bw(s, i))
        if rem:
            b_in = self.B_r / (1 + self.ingress[i]) if self.loc.cost == "contend" else self.B_r
            t_r = max(t_r, rem * pb / b_in)
        return t_l, t_r

    # ------------------------------------------------------------ routing
    def _route(self, keys, r=None):
        rt = self.loc.route
        if rt == "sglang" or r is None:
            return super()._route(keys, r)
        load = self.load
        if rt == "load":
            lo = min(load)
            cand = [i for i in range(self.n) if load[i] == lo]
            return max(cand, key=lambda i: pref(self.hbm[i].all, keys))
        # ttft：W_i + 未掩盖读 + 计算；池前缀对所有实例相同，近远与争用因实例而异
        now = self.now
        P = self.wl.page_tokens
        hp = pref(self.pool.all, keys)
        hold = [self._holders(k) for k in keys[:hp]]
        best, bi = math.inf, 0
        k = self.loc.k
        for i in range(self.n):
            hh = pref(self.hbm[i].all, keys)
            hit = max(hh, hp)
            by: dict = {}
            for hs in hold[hh:hp]:
                s = i if i in hs else hs[0]
                by[s] = by.get(s, 0) + 1
            tau = max(self._tau(by, i))
            W = max(0.0, self.slot[i][0] - now)
            exp = max(0.0, tau - W) if self.loc.overlap else tau
            T = W + exp + self._comp(r.n_in, hit * P) * k
            if T < best - 1e-9 or (abs(T - best) <= 1e-9 and load[i] < load[bi]):
                best, bi = T, i
        return bi

    # ------------------------------------------------------------ per request
    def _on_serve(self, now, i, keys, h_hbm, hit, r) -> None:
        loc = self.loc
        k = loc.k
        P = self.wl.page_tokens
        comp = self._comp(r.n_in, hit * P) * k
        sl = self.slot[i]
        start = max(now, sl[0])
        W = start - now
        t0 = now if loc.overlap else start
        pool_pages = keys[h_hbm:hit] if hit > h_hbm else []
        by: dict = {}
        remote_pages = []
        for kk in pool_pages:
            hs = self._holders(kk)
            s = self._pick(hs, i)
            by[s] = by.get(s, 0) + 1
            if s != i:
                remote_pages.append((kk, s, len(hs)))
            elif len(hs) > 1 and (kk, i) in self.pool.l0:
                self.pool.l0.move_to_end((kk, i))
        t_l, t_r = self._tau(by, i)
        tau = max(t_l, t_r)
        durs = {}
        for s in by:
            durs[s] = t_l if s == i else t_r
            self._flow(now, t0, durs[s], "l" if s == i else "e", s)
        if t_r > 0:
            self._flow(now, t0, t_r, "i", i)  # 一个请求的多源跨机读在读者入口只算一条流
        exp = max(0.0, t0 + tau - start)
        heapq.heapreplace(sl, start + exp + comp)
        rec = now >= self.warm
        if rec:
            m = self.m
            m["t"].append(now)
            m["ttft"].append((W + exp + comp) / k)
            m["W"].append(W / k)
            m["exp"].append(exp / k)
            m["comp"].append(comp / k)
            m["tau"].append(tau / k)
            nb = len(pool_pages) * self.page_bytes
            m["pool_b"].append(nb)
            m["loc_b"].append(by.get(i, 0) * self.page_bytes)
            for s, c in by.items():
                if s == i:
                    self.lo_bytes[i] += c * self.page_bytes
                elif s >= 0:
                    self.eg_bytes[s] += c * self.page_bytes
            if self.n_arr % 50 == 0:
                self.rep_samples.append(self.n_rep / max(1, len(self.pool.all)))
        self._replicate(now, i, pool_pages, remote_pages, durs, t0)

    def _replicate(self, now, i, pool_pages, remote_pages, durs, t0) -> None:
        rp = self.loc.repl
        if rp == "none" or self.loc.cost == "flat":
            return
        if rp == "passive":
            for kk, s, _ in remote_pages:
                self._add_rep(kk, i)
            return
        # 热度：池读的 EWMA 速率 λ_p；目标副本数 r = ceil(λ_p × τ_req / ρ)（D8a §6.5，Little 定律）
        heat = self.heat
        lk = self.lam_k
        copy_by: dict = {}
        for kk in pool_pages:
            v, tl = heat.get(kk, (0.0, now))
            v = v * math.exp(-lk * (now - tl)) + 1.0
            heat[kk] = (v, now)
        for kk, s, nh in remote_pages:
            lam = heat[kk][0] * lk
            tau_req = durs.get(s, 0.0)
            r_t = min(self.loc.max_rep, math.ceil(lam * tau_req / self.loc.rho))
            if nh >= r_t:
                continue
            if rp == "kvcm":
                self._add_rep(kk, i)  # 读者刚拉到的字节就地留存，不额外搬运
            else:
                hs = self._holders(kk)
                cand = [x for x in range(self.n) if x not in hs]
                if not cand:
                    continue
                tgt = min(cand, key=lambda x: self.egress[x])
                if self._add_rep(kk, tgt):
                    copy_by[(s, tgt)] = copy_by.get((s, tgt), 0) + 1
        for (s, tgt), c in copy_by.items():
            d = c * self.page_bytes / self.B_r
            self.copy_bytes += c * self.page_bytes
            self._flow(now, now, d, "e", s)
            self._flow(now, now, d, "i", tgt)
