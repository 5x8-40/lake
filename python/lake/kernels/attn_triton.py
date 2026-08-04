"""Triton attention 入口占位。

C3/C5：数值路径统一走 `attn_ref`（CI 无 GPU）。
真 Triton kernel 落地前，勿把「import 成功」当成「走了 GPU 路径」。
"""

from __future__ import annotations

from typing import List

from lake.kernels.attn_ref import causal_attn


def causal_attn_triton(
    q: List[List[float]],
    k: List[List[float]],
    v: List[List[float]],
) -> List[List[float]]:
    # TODO(C3+): 真 triton kernel；当前始终参考实现
    return causal_attn(q, k, v)
