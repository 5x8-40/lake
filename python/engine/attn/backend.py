"""Attention 后端选择（对照 vLLM AttentionBackend；agent 出表、runner 出其余 metadata 留 D4）。"""

from __future__ import annotations

from typing import List, Protocol

from kernels.attn_ref import causal_attn, causal_attn_queries
from kernels.attn_triton import causal_attn_triton


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


def build_attn_backend(name: str = "triton") -> AttentionBackend:
    if name == "ref":
        return RefAttentionBackend()
    return TritonAttentionBackend()
