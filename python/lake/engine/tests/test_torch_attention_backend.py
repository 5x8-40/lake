"""Pure Torch attention backend tests."""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch", reason="torch 不在 CI 最小环境,跳过(真机/GPU 环境跑)")

from lake.engine.model_executor.layers.attentions import TorchAttentionBackend
from lake.engine.model_executor.models.qwen.qwen3 import Qwen3PagedAttention


def _manual_causal_attention(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    if q.shape[1] != k.shape[1]:
        repeat = q.shape[1] // k.shape[1]
        k = k.repeat_interleave(repeat, dim=1)
        v = v.repeat_interleave(repeat, dim=1)
    scale = q.shape[-1] ** -0.5
    scores = torch.matmul(q, k.transpose(-2, -1)) * scale
    q_len = q.shape[-2]
    k_len = k.shape[-2]
    mask = torch.ones(q_len, k_len, dtype=torch.bool).tril()
    scores = scores.masked_fill(~mask, float("-inf"))
    probs = torch.softmax(scores, dim=-1)
    return torch.matmul(probs, v)


def test_torch_attention_backend_matches_manual_causal_gqa() -> None:
    q = torch.tensor(
        [[[[1.0, 0.0], [0.0, 1.0]], [[1.0, 1.0], [1.0, -1.0]]]],
        dtype=torch.float32,
    )
    k = torch.tensor([[[[1.0, 0.0], [0.0, 1.0]]]], dtype=torch.float32)
    v = torch.tensor([[[[2.0, 0.0], [0.0, 4.0]]]], dtype=torch.float32)

    out = TorchAttentionBackend().forward_tensors(q, k, v)
    expected = _manual_causal_attention(q, k, v)
    torch.testing.assert_close(out, expected)


def test_qwen3_paged_attention_uses_torch_backend_for_tensors() -> None:
    q = torch.randn(1, 2, 3, 4)
    k = torch.randn(1, 1, 3, 4)
    v = torch.randn(1, 1, 3, 4)
    attn = Qwen3PagedAttention(num_heads=2, head_dim=4, num_kv_heads=1)

    out = attn(q, k, v)
    expected = TorchAttentionBackend().forward_tensors(q, k, v, scale=4**-0.5)
    torch.testing.assert_close(out, expected)
