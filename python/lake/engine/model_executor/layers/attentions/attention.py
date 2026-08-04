"""Attention 后端选择（对照 vLLM AttentionBackend；agent 出表、runner 出其余 metadata 留 D4）。"""

from __future__ import annotations

from typing import List, Protocol

import torch

from lake.kernels.attn_ref import causal_attn, causal_attn_queries
from lake.kernels.attn_triton import causal_attn_triton


class AttentionBackend(Protocol):
    name: str

    def forward(
        self,
        q: List[List[float]],
        k: List[List[float]],
        v: List[List[float]],
    ) -> List[List[float]]: ...

    def forward_queries(
        self,
        q: List[List[float]],
        k: List[List[float]],
        v: List[List[float]],
        q_pos_start: int,
    ) -> List[List[float]]: ...


class RefAttentionBackend:
    name = "ref"

    def forward(
        self,
        q: List[List[float]],
        k: List[List[float]],
        v: List[List[float]],
    ) -> List[List[float]]:
        return causal_attn(q, k, v)

    def forward_queries(
        self,
        q: List[List[float]],
        k: List[List[float]],
        v: List[List[float]],
        q_pos_start: int,
    ) -> List[List[float]]:
        return causal_attn_queries(q, k, v, q_pos_start)


class TritonAttentionBackend:
    name = "triton"

    def forward(
        self,
        q: List[List[float]],
        k: List[List[float]],
        v: List[List[float]],
    ) -> List[List[float]]:
        return causal_attn_triton(q, k, v)

    def forward_queries(
        self,
        q: List[List[float]],
        k: List[List[float]],
        v: List[List[float]],
        q_pos_start: int,
    ) -> List[List[float]]:
        # triton stub 未实现残差路径时回退 ref（保正确性）
        return causal_attn_queries(q, k, v, q_pos_start)


class TorchAttentionBackend:
    """Pure Torch tensor backend for Qwen3/Torch model skeletons.

    Tensor shape follows common decoder attention layout: [B, H, T, D].
    """

    name = "torch"

    def forward_tensors(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        *,
        is_causal: bool = True,
        attn_mask: torch.Tensor | None = None,
        dropout_p: float = 0.0,
        scale: float | None = None,
    ) -> torch.Tensor:
        if q.ndim != 4 or k.ndim != 4 or v.ndim != 4:
            raise ValueError("q/k/v must have shape [B, H, T, D]")
        if k.shape[1] != v.shape[1]:
            raise ValueError("k and v must have the same number of heads")
        if q.shape[1] != k.shape[1]:
            if q.shape[1] % k.shape[1] != 0:
                raise ValueError("q heads must be divisible by kv heads for GQA repeat")
            repeat = q.shape[1] // k.shape[1]
            k = k.repeat_interleave(repeat, dim=1)
            v = v.repeat_interleave(repeat, dim=1)
        causal = is_causal and attn_mask is None
        try:
            return torch.nn.functional.scaled_dot_product_attention(
                q,
                k,
                v,
                attn_mask=attn_mask,
                dropout_p=dropout_p,
                is_causal=causal,
                scale=scale,
            )
        except TypeError:
            if scale is not None:
                q = q * (scale * (q.shape[-1] ** 0.5))
            return torch.nn.functional.scaled_dot_product_attention(
                q,
                k,
                v,
                attn_mask=attn_mask,
                dropout_p=dropout_p,
                is_causal=causal,
            )


def build_attn_backend(name: str = "triton") -> AttentionBackend:
    if name == "ref":
        return RefAttentionBackend()
    if name == "torch":
        return TorchAttentionBackend()
    return TritonAttentionBackend()
