"""Attention 后端边界（D4/C8）：metadata + 后端选择。"""

from engine.attn.backend import AttentionBackend, RefAttentionBackend, build_attn_backend
from engine.attn.metadata import AttentionMetadata, build_attn_metadata

__all__ = [
    "AttentionBackend",
    "AttentionMetadata",
    "RefAttentionBackend",
    "build_attn_backend",
    "build_attn_metadata",
]
