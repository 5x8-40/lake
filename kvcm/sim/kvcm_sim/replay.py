"""事件驱动的 KV 缓存回放器（纯 CPU，不跑模型）。

拓扑：n_inst 个推理实例，每实例一块 HBM KV 区（L0）；DRAM（L1）两种组织：
- `local_lru`：每实例私有 DRAM（write-through，inclusive），只本实例可见；
- `shared_lru` / `kvcm`：全部节点 DRAM 组成一个共享池（去重，任意实例可取）。

缓存粒度为页（`page_tokens` 个 token，链式前缀键）。命中 = 连续前缀长度（HBM 与
DRAM 的并集）。每个请求的整条前缀都被加载到服务实例的 HBM，并写入 DRAM 层；
触达按“叶先根后”顺序刷新 LRU，祖先页始终不比子孙页旧，所以各层驻留集合沿前缀
闭合，前缀长度可以二分查找。

驱逐分三类：`pinned`（硬保活，不可驱逐）> `lru1`（有活会话引用，后驱逐）>
`lru0`（零引用，先驱逐）。基线策略里所有页都在 `lru0`，退化为纯 LRU。

KVCM 机制（逐项可开关，用于消融）：
- keepalive：会话停顿期把前缀钉在 HBM（短停顿）或共享池（长停顿），带预算；
- prewarm：按 ETA 在返回前把前缀从共享池预放置到目标实例 HBM（D-direct）；
- zeroref：活会话引用的页后驱逐，零引用页先驱逐。
hook 三档决定 ETA：none（无信息）、realistic（只用该流历史间隔预测）、
oracle（真实间隔，且知道会话是否还有下一轮）。
"""

from __future__ import annotations

import heapq
import math
from collections import OrderedDict
from dataclasses import dataclass, field, replace

import numpy as np

from .traces import Workload

ARRIVE, COMPLETE, PREWARM, PIN_EXP_POOL, PIN_EXP_HBM, LIVE_EXP = range(6)


@dataclass
class Config:
    n_inst: int = 7
    hbm_pages: int = 0            # 每实例 HBM KV 页数
    dram_pages: int = 0           # DRAM 总页数（local_lru 时均分到各实例）
    policy: str = "local_lru"     # local_lru | shared_lru | kvcm
    keepalive: bool = False
    prewarm: bool = False
    zeroref: bool = False
    hook: str = "none"            # none | realistic | oracle
    # 路由：SGLang cache-aware 规则——负载失衡时走最短队列，否则走最长前缀命中
    bal_abs: int = 8
    bal_rel: float = 1.5
    # KVCM 参数（假设值，见 params.md）
    hbm_keep_max_gap: float = 30.0
    hbm_pin_frac: float = 0.3
    pool_pin_max_gap: float = 7200.0
    pool_pin_frac: float = 0.5
    slack_mult: float = 2.0
    slack_add: float = 5.0
    prewarm_lead: float = 5.0
    live_mult: float = 4.0
    live_min: float = 300.0
    eta_hist: int = 5
    pin_tc_mult: float = 2.0      # 只钉 pin_tc_lo×Tc < ETA ≤ pin_tc_mult×Tc 的停顿（Tc=该层 LRU 驻留时间）
    pin_tc_lo: float = 0.0        # 更短的停顿 LRU 自己就留得住，不占钉预算
    tc_window: int = 2000         # 每多少个到达刷新一次各层 LRU 驻留时间估计
    zeroref_hbm: bool = False     # 零引用优先是否也作用于各实例 HBM（默认只作用于共享池）
    attribution: bool = False     # 记录“集群内存在但本实例不可见”的命中（损失归因）


class Tier:
    __slots__ = ("cap", "all", "l0", "l1", "pinned", "pin_budget", "ins", "ins_prev", "tc")

    def __init__(self, cap: int, pin_frac: float = 0.0):
        self.cap = max(1, int(cap))
        self.all: dict[int, int] = {}      # key -> 0/1/2
        self.l0: OrderedDict = OrderedDict()
        self.l1: OrderedDict = OrderedDict()
        self.pinned: dict[int, int] = {}   # key -> pin count
        self.pin_budget = int(self.cap * pin_frac)
        self.ins = 0
        self.ins_prev = 0
        self.tc = math.inf          # LRU 驻留时间估计 = 容量 / 新页写入速率

    def size(self) -> int:
        return len(self.all)


def pref(d: dict, keys: list) -> int:
    n = len(keys)
    if n == 0 or keys[0] not in d:
        return 0
    if keys[-1] in d:
        return n
    lo, hi = 0, n - 1
    while hi - lo > 1:
        m = (lo + hi) >> 1
        if keys[m] in d:
            lo = m
        else:
            hi = m
    return lo + 1


class Sim:
    def __init__(self, cfg: Config, wl: Workload, rate: float, horizon: float, warmup: float, seed: int = 1):
        self.cfg = cfg
        self.wl = wl
        self.rate = rate
        self.horizon = horizon
        self.warmup = warmup
        self.rng = np.random.default_rng(seed)
        n = cfg.n_inst
        shared = cfg.policy != "local_lru"
        hpf = cfg.hbm_pin_frac if (cfg.policy == "kvcm" and cfg.keepalive) else 0.0
        ppf = cfg.pool_pin_frac if (cfg.policy == "kvcm" and cfg.keepalive) else 0.0
        self.hbm = [Tier(cfg.hbm_pages, hpf) for _ in range(n)]
        if shared:
            pool = Tier(cfg.dram_pages, ppf)
            self.dram = [pool] * n
            self.tiers = self.hbm + [pool]
        else:
            self.dram = [Tier(cfg.dram_pages / n) for _ in range(n)]
            self.tiers = self.hbm + self.dram
        self.pool = self.dram[0] if shared else None
        self.shared = shared
        self.kv = cfg.policy == "kvcm"
        self.use_ref = self.kv and cfg.zeroref and cfg.hook != "none"
        self.refs: dict[int, int] = {}
        self.sess: dict = {}
        self.ref_tiers = self.tiers if cfg.zeroref_hbm else ([self.pool] if shared else [])
        self.load = [0] * n
        self.ideal: set = set()
        self.streams: dict = {}
        self.prewarm_pages = 0
        self.tc_t = 0.0
        self.g_prior = 5.0
        self.ghist: list = []
        self.n_arr = 0

    # ------------------------------------------------------------ tier ops
    def _evict_one(self, t: Tier) -> bool:
        if t.l0:
            k, _ = t.l0.popitem(last=False)
        elif t.l1:
            k, _ = t.l1.popitem(last=False)
        else:
            return False
        del t.all[k]
        return True

    def use(self, t: Tier, keys: list) -> None:
        a = t.all
        l0 = t.l0
        l1 = t.l1
        refs = self.refs if (self.use_ref and t in self.ref_tiers) else None
        cap = t.cap
        for k in reversed(keys):
            c = a.get(k)
            if c == 0:
                l0.move_to_end(k)
            elif c == 1:
                l1.move_to_end(k)
            elif c is None:
                if len(a) >= cap and not self._evict_one(t):
                    continue
                t.ins += 1
                if refs is not None and refs.get(k):
                    a[k] = 1
                    l1[k] = None
                else:
                    a[k] = 0
                    l0[k] = None

    def _pin(self, t: Tier, keys: list) -> None:
        a = t.all
        for k in keys:
            c = a.get(k)
            if c is None:
                continue
            if c == 2:
                t.pinned[k] += 1
            else:
                (t.l0 if c == 0 else t.l1).pop(k)
                a[k] = 2
                t.pinned[k] = 1

    def _unpin(self, t: Tier, keys: list) -> None:
        a = t.all
        refs = self.refs
        for k in reversed(keys):
            c = t.pinned.get(k)
            if c is None:
                continue
            if c > 1:
                t.pinned[k] = c - 1
            else:
                del t.pinned[k]
                if self.use_ref and t in self.ref_tiers and refs.get(k):
                    a[k] = 1
                    t.l1[k] = None
                else:
                    a[k] = 0
                    t.l0[k] = None

    def _ref_change(self, keys: list, delta: int) -> None:
        refs = self.refs
        tiers = self.ref_tiers
        for k in keys:
            old = refs.get(k, 0)
            new = old + delta
            if new:
                refs[k] = new
            else:
                refs.pop(k, None)
            if (old > 0) == (new > 0):
                continue
            for t in tiers:
                c = t.all.get(k)
                if c == 0 and new > 0:
                    t.l0.pop(k)
                    t.l1[k] = None
                    t.all[k] = 1
                elif c == 1 and new == 0:
                    t.l1.pop(k)
                    t.l0[k] = None
                    t.all[k] = 0

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _diff(old: list, new: list):
        """返回 (old 中要释放的, new 中要新增的)。前缀链：只比较公共前缀之后的部分。"""
        n = min(len(old), len(new))
        i = 0
        # 快路径：大多数情况下 new 以 old 为前缀
        if n and old[n - 1] == new[n - 1]:
            i = n
        else:
            while i < n and old[i] == new[i]:
                i += 1
        return old[i:], new[i:]

    def _eta(self, st: dict, gap_true: float):
        """返回 (点估计, 上界估计)。realistic 只用该流历史间隔（冷启动用全局历史中位数）。"""
        if self.cfg.hook == "oracle":
            return gap_true, gap_true
        hist = st["hist"][-self.cfg.eta_hist:]
        if not hist:
            g = self.g_prior
            return g, g * 4.0
        lg = np.log(np.asarray(hist) + 0.1)
        mu = float(lg.mean())
        sd = float(lg.std()) if len(lg) > 1 else 1.0
        return math.exp(mu), math.exp(mu + sd)

    def _tc_update(self, now: float) -> None:
        dt = now - self.tc_t
        if dt <= 0:
            return
        for t in self.tiers:
            rate = (t.ins - t.ins_prev) / dt
            t.ins_prev = t.ins
            t.tc = (len(t.all) / rate) if (rate > 0 and len(t.all) >= 0.95 * t.cap) else math.inf
        self.tc_t = now
        if self.ghist:
            self.g_prior = float(np.median(self.ghist[-20000:]))

    def _route(self, keys: list):
        cfg = self.cfg
        load = self.load
        lo = min(load)
        hi = max(load)
        if hi - lo > cfg.bal_abs and hi > cfg.bal_rel * lo:
            i = load.index(lo)
            return i
        best = -1
        bi = 0
        for i in range(cfg.n_inst):
            h = pref(self.hbm[i].all, keys)
            if not self.shared:
                h = max(h, pref(self.dram[i].all, keys))
            if h > best or (h == best and load[i] < load[bi]):
                best = h
                bi = i
        return bi

    # ------------------------------------------------------------ main loop
    def run(self):
        cfg = self.cfg
        wl = self.wl
        P = wl.page_tokens
        ev: list = []
        seq = 0
        # 会话以 Poisson 过程到达；每个到达实例随机挑一个模板会话、加盐隔离
        t = 0.0
        n_sess = 0
        S = len(wl.sessions)
        while True:
            t += float(self.rng.exponential(1.0 / self.rate))
            if t >= self.horizon:
                break
            tmpl = wl.sessions[int(self.rng.integers(S))]
            salt = int(self.rng.integers(1, 2**62))
            kept = [r for r in tmpl.reqs if t + r.t < self.horizon]
            for ri, r in enumerate(kept):
                heapq.heappush(ev, (t + r.t, seq, ARRIVE, (n_sess, salt, r, ri == len(kept) - 1)))
                seq += 1
            n_sess += 1
        self.n_sessions = n_sess

        rec_t, rec_n, rec_hbm, rec_hit, rec_ideal, rec_gap, rec_any, rec_pw = ([] for _ in range(8))
        streams = self.streams
        while ev:
            now, _, typ, pl = heapq.heappop(ev)
            if typ == ARRIVE:
                sid, salt, r, is_last = pl
                pages = r.pages
                if r.shared is None:
                    keys = (pages ^ salt).tolist()
                else:
                    keys = np.where(r.shared, pages, pages ^ salt).tolist()
                skey = (sid, r.stream)
                st = streams.get(skey)
                if st is None:
                    st = streams[skey] = {"hist": [], "last_end": None, "inst": None, "pool_pin": [],
                                          "hbm_pin": [], "hbm_pin_inst": None, "ref": [], "tok": 0,
                                          "pw": None}
                gap = now - st["last_end"] if st["last_end"] is not None else math.nan
                if st["last_end"] is not None:
                    st["hist"].append(max(0.0, gap))
                    self.ghist.append(max(0.0, gap))
                self.n_arr += 1
                if self.n_arr % cfg.tc_window == 0:
                    self._tc_update(now)
                ideal = pref(self.ideal, keys)
                self.ideal.update(keys)
                i = self._route(keys)
                h_hbm = pref(self.hbm[i].all, keys)
                h_d = pref(self.dram[i].all, keys)
                hit = max(h_hbm, h_d)
                if cfg.attribution:
                    anyh = hit
                    for tt in self.tiers:
                        if anyh >= len(keys):
                            break
                        anyh = max(anyh, pref(tt.all, keys))
                    rec_any.append(anyh)
                pw = st["pw"]
                rec_pw.append(1 if (pw is not None and pw == i and h_hbm > 0) else 0)
                st["pw"] = None
                # HBM 钉在别的实例上：请求换了实例，旧钉子作废
                if st["hbm_pin"] and st["hbm_pin_inst"] != i:
                    self._unpin(self.hbm[st["hbm_pin_inst"]], st["hbm_pin"])
                    st["hbm_pin"] = []
                if self.use_ref:
                    # 引用归会话所有：同一会话的 subagent 之间共享前缀（CC trace 里约 40% 的
                    # 已结束 subagent 页会被同会话后续流复用），按流计引用会错杀。
                    # 在途请求同样持有引用（同 vLLM ref_cnt），新算出的页不进“先驱逐”队列。
                    ss = self.sess.get(sid)
                    if ss is None:
                        ss = self.sess[sid] = {"ref": set(), "tok": 0}
                    sref = ss["ref"]
                    new = [k for k in keys if k not in sref]
                    if new:
                        sref.update(new)
                        self._ref_change(new, +1)
                    ss["tok"] += 1
                self.use(self.dram[i], keys)
                self.use(self.hbm[i], keys)
                self.load[i] += 1
                st["inst"] = i
                st["tok"] += 1
                heapq.heappush(ev, (now + r.svc, seq, COMPLETE, (skey, i, keys, r, now, is_last)))
                seq += 1
                rec_t.append(now)
                rec_n.append(r.n_in)
                rec_hbm.append(h_hbm)
                rec_hit.append(hit)
                rec_ideal.append(ideal)
                rec_gap.append(gap)
            elif typ == COMPLETE:
                skey, i, keys, r, t_arr, is_last = pl
                self.load[i] -= 1
                if not self.kv or cfg.hook == "none":
                    st = streams[skey]
                    st["last_end"] = now
                    continue
                st = streams[skey]
                st["last_end"] = now
                tok = st["tok"]
                has_next = r.next_t < math.inf
                gap_true = (t_arr - r.t + r.next_t) - now if has_next else math.inf
                eta, eta_hi = self._eta(st, gap_true)
                oracle = cfg.hook == "oracle"
                if self.use_ref:
                    sid = skey[0]
                    ss = self.sess[sid]
                    if oracle:
                        if is_last:
                            self._release_sess(sid)
                    else:
                        live = max(cfg.live_mult * eta_hi, cfg.live_min)
                        heapq.heappush(ev, (now + live, seq, LIVE_EXP, (sid, ss["tok"])))
                        seq += 1
                if oracle and not has_next:
                    self._release(st)
                    continue
                if cfg.keepalive:
                    # 共享池保活
                    if cfg.pin_tc_lo * self.pool.tc < eta <= min(cfg.pool_pin_max_gap, cfg.pin_tc_mult * self.pool.tc):
                        exp = now + (eta + 1.0 if oracle else eta_hi * cfg.slack_mult + cfg.slack_add)
                        self._repin(self.pool, st, "pool_pin", keys)
                        if st["pool_pin"]:
                            heapq.heappush(ev, (exp, seq, PIN_EXP_POOL, (skey, tok)))
                            seq += 1
                    elif st["pool_pin"]:
                        self._unpin(self.pool, st["pool_pin"])
                        st["pool_pin"] = []
                    # HBM 保活（短停顿）
                    if cfg.pin_tc_lo * self.hbm[i].tc < eta <= min(cfg.hbm_keep_max_gap, cfg.pin_tc_mult * self.hbm[i].tc):
                        exp = now + (eta + 1.0 if oracle else eta_hi * cfg.slack_mult + cfg.slack_add)
                        if st["hbm_pin"] and st["hbm_pin_inst"] != i:
                            self._unpin(self.hbm[st["hbm_pin_inst"]], st["hbm_pin"])
                            st["hbm_pin"] = []
                        st["hbm_pin_inst"] = i
                        self._repin(self.hbm[i], st, "hbm_pin", keys)
                        if st["hbm_pin"]:
                            heapq.heappush(ev, (exp, seq, PIN_EXP_HBM, (skey, tok)))
                            seq += 1
                    elif st["hbm_pin"]:
                        self._unpin(self.hbm[st["hbm_pin_inst"]], st["hbm_pin"])
                        st["hbm_pin"] = []
                if cfg.prewarm and max(cfg.hbm_keep_max_gap, 0.5 * self.hbm[i].tc) < eta <= cfg.pool_pin_max_gap:
                    tp = now + max(0.0, eta - cfg.prewarm_lead)
                    heapq.heappush(ev, (tp, seq, PREWARM, (skey, tok, keys, eta)))
                    seq += 1
            elif typ == PIN_EXP_POOL:
                skey, tok = pl
                st = streams[skey]
                if st["tok"] == tok and st["pool_pin"]:
                    self._unpin(self.pool, st["pool_pin"])
                    st["pool_pin"] = []
            elif typ == PIN_EXP_HBM:
                skey, tok = pl
                st = streams[skey]
                if st["tok"] == tok and st["hbm_pin"]:
                    self._unpin(self.hbm[st["hbm_pin_inst"]], st["hbm_pin"])
                    st["hbm_pin"] = []
            elif typ == LIVE_EXP:
                sid, tok = pl
                if self.sess[sid]["tok"] == tok:
                    self._release_sess(sid)
            elif typ == PREWARM:
                skey, tok, keys, eta = pl
                st = streams[skey]
                if st["tok"] != tok:
                    continue  # 请求已经先到了
                p = pref(self.pool.all, keys)
                if p == 0:
                    continue
                last = st["inst"]
                lo = min(self.load)
                tgt = last if self.load[last] <= max(cfg.bal_rel * lo, lo + cfg.bal_abs) else self.load.index(lo)
                sub = keys[:p]
                before = pref(self.hbm[tgt].all, sub)
                self.use(self.hbm[tgt], sub)
                self.prewarm_pages += p - before
                st["pw"] = tgt
                if cfg.keepalive:
                    if st["hbm_pin"] and st["hbm_pin_inst"] != tgt:
                        self._unpin(self.hbm[st["hbm_pin_inst"]], st["hbm_pin"])
                        st["hbm_pin"] = []
                    st["hbm_pin_inst"] = tgt
                    self._repin(self.hbm[tgt], st, "hbm_pin", sub)
                    if st["hbm_pin"]:
                        slack = 1.0 if cfg.hook == "oracle" else cfg.prewarm_lead + eta * (cfg.slack_mult - 1) + cfg.slack_add
                        heapq.heappush(ev, (now + cfg.prewarm_lead + slack, seq, PIN_EXP_HBM, (skey, tok)))
                        seq += 1

        self.rec = {
            "t": np.array(rec_t), "n_in": np.array(rec_n, dtype=np.int64),
            "hbm": np.array(rec_hbm, dtype=np.int64) * P, "hit": np.array(rec_hit, dtype=np.int64) * P,
            "ideal": np.array(rec_ideal, dtype=np.int64) * P, "gap": np.array(rec_gap),
            "prewarmed": np.array(rec_pw, dtype=np.int8),
        }
        if cfg.attribution:
            self.rec["any"] = np.array(rec_any, dtype=np.int64) * P
        for k in ("hbm", "hit", "ideal", "any"):
            if k in self.rec:
                self.rec[k] = np.minimum(self.rec[k], self.rec["n_in"])
        return self.rec

    def _repin(self, tier: Tier, st: dict, slot: str, keys: list) -> None:
        old = st[slot]
        rel, add = self._diff(old, keys)
        # 预算：只算新增的不同页
        new_pages = sum(1 for k in add if k in tier.all and k not in tier.pinned)
        if len(tier.pinned) - len(rel) + new_pages > tier.pin_budget:
            if old:
                self._unpin(tier, old)
            st[slot] = []
            return
        if add:
            self._pin(tier, add)
        if rel:
            self._unpin(tier, rel)
        st[slot] = keys

    def _release(self, st: dict) -> None:
        if st["pool_pin"]:
            self._unpin(self.pool, st["pool_pin"])
            st["pool_pin"] = []
        if st["hbm_pin"]:
            self._unpin(self.hbm[st["hbm_pin_inst"]], st["hbm_pin"])
            st["hbm_pin"] = []

    def _release_sess(self, sid) -> None:
        ss = self.sess.get(sid)
        if ss and ss["ref"]:
            self._ref_change(list(ss["ref"]), -1)
            ss["ref"] = set()
