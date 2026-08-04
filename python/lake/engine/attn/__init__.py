"""Attention 后端边界（D4/C8）：metadata + 后端选择。"""

from lake.engine.model_executor.layers.attentions import (
    AttentionMetadata,
    build_attn_metadata,
)

__all__ = [
    "AttentionBackend",
    "AttentionMetadata",
    "RefAttentionBackend",
    "TorchAttentionBackend",
    "build_attn_backend",
    "build_attn_metadata",
]


def __getattr__(name: str):
    if name in {
        "AttentionBackend",
        "RefAttentionBackend",
        "TorchAttentionBackend",
        "build_attn_backend",
    }:
        from lake.engine.model_executor.layers import attentions

        return getattr(attentions, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
