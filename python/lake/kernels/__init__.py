"""Triton / 参考 kernel 集。

C3：`attn_ref` 为默认；`attn_triton` 在无 triton 时回退 ref。
"""

from lake.kernels.attn_ref import causal_attn
from lake.kernels.attn_triton import causal_attn_triton

__all__ = ["causal_attn", "causal_attn_triton"]
