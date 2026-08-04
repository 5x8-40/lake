"""参考实现：因果 attention（纯 Python）。

生产路径由 Triton kernel 替换（`kernels/attn_triton.py`，可选依赖）。
对照 vLLM `AttentionBackend` / SGLang flashinfer 路径——此处仅验证接口与数值形状。
"""

from __future__ import annotations

import math
from typing import List


def causal_attn(
    q: List[List[float]],
    k: List[List[float]],
    v: List[List[float]],
) -> List[List[float]]:
    """q/k/v: [T, D] → out [T, D]，因果 mask。"""
    return causal_attn_queries(q, k, v, q_pos_start=0)


def causal_attn_queries(
    q: List[List[float]],
    k: List[List[float]],
    v: List[List[float]],
    q_pos_start: int,
) -> List[List[float]]:
    """残差 query：q 为 `[q_pos_start, q_pos_start+len(q))`，k/v 为全前缀 `[0, T)`。

    C8：只对新区算 Q，仍读完整 K/V 前缀（生产 K/V 来自池 L0）。
    """
    t_kv = len(k)
    d = len(q[0]) if q else (len(k[0]) if k else 0)
    scale = 1.0 / math.sqrt(d) if d else 1.0
    out: List[List[float]] = []
    for qi, qrow in enumerate(q):
        pos = q_pos_start + qi
        scores = []
        for j in range(t_kv):
            if j > pos:
                scores.append(float("-inf"))
            else:
                s = sum(qrow[u] * k[j][u] for u in range(d)) * scale
                scores.append(s)
        finite = [x for x in scores if x != float("-inf")]
        m = max(finite) if finite else 0.0
        exps = [math.exp(s - m) if s != float("-inf") else 0.0 for s in scores]
        z = sum(exps) or 1.0
        weights = [e / z for e in exps]
        row = [0.0] * d
        for j in range(t_kv):
            w = weights[j]
            for u in range(d):
                row[u] += w * v[j][u]
        out.append(row)
    return out
