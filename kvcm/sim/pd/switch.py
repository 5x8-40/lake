"""E4（轻量版）：一次 D→P 切换的代价，有 KVCM 池 vs 无池。

不跑 DES，只对“切换时刻 D 节点上的在途 decode”做快照抽样：
- 在途请求按 decode 时长加权抽（长请求更可能在途），已生成 token 数在 [0, out) 均匀；
- 抽到节点 KV 达到 hbm_kv_per_node × kv_fill 为止。

四种处理在途请求的方式：
1. 无池 · 排空：不接新请求，等在途全部跑完 → max(剩余 token) × t_step；
2. 无池 · 点对点迁移（需引擎支持，类 Llumnix）：在途 KV 字节 / (16 × RoCE)；
3. 无池 · 杀掉重算：在途请求回到 prefill，按 hit=0 重算整段上下文的 NPU·s；
4. KVCM 池：KV 已按页写穿，只需刷入未写满的尾页 → Σ尾页 / (16 × D2rH)；
   接收方从池重读整段上下文，每个请求多一次 ctx/78 GB/s 的停顿。

P→D 方向只需排空在途 prefill（t_p_drain），各方式相同。
权重：同布局逻辑切换 0；sleep/wake + host H2D 加 t_weights_sleep，并且 host 副本常驻挤占池。
"""

from __future__ import annotations

import numpy as np

from kvcm_sim.params import DEFAULT as BASE
from .pd_params import PDParams
from .workload import Sessions


def snapshots(S: Sessions, q: PDParams, n: int = 400, seed: int = 7):
    rng = np.random.default_rng(seed)
    out = S.out
    w = out / out.sum()
    bpt = q.kv_bytes_per_token
    cap = q.kv_cap_node
    res = []
    m = int(cap / (bpt * max(1.0, float(np.percentile(S.n_in, 5))))) + 16
    for _ in range(n):
        j = rng.choice(len(out), size=m, p=w)
        age = rng.uniform(0, 1, m) * out[j]
        c = S.n_in[j] + age
        k = int(np.searchsorted(np.cumsum(c * bpt), cap)) + 1
        res.append(((out[j] - age)[:k], c[:k]))
    return res


def costs(S: Sessions, q: PDParams, n: int = 400):
    bpt = q.kv_bytes_per_token
    npn = q.npu_per_node
    rows = []
    for rem, ctx in snapshots(S, q, n):
        kv = ctx * bpt
        tail = (ctx % q.page_tokens) * bpt
        rows.append(dict(
            seqs=len(ctx),
            kv_gb=kv.sum() / 1e9,
            drain_s=rem.max() * q.t_step,
            d2d_s=kv.sum() / (npn * q.bw_d2d_per_npu),
            recompute_npu_s=float(BASE.npu_seconds(ctx, 0.0).sum()),
            pool_flush_s=tail.sum() / (npn * q.bw_flush_per_npu),
            pool_reload_stall_s_p95=float(np.percentile(kv / q.bw_pool_read_per_npu, 95)),
            pool_reload_node_s=kv.sum() / (npn * q.bw_pool_read_per_npu),
        ))
    keys = rows[0].keys()
    pct = lambda k, p: float(np.percentile([r[k] for r in rows], p))
    return {k: {"p50": pct(k, 50), "p95": pct(k, 95), "max": pct(k, 100)} for k in keys}
