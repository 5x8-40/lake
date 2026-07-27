"""TinyLM：零强制依赖的最小因果 LM（C3）。

纯 Python + `kernels.attn_*`；无 torch/triton 亦可跑。
生产将换 HuggingFace/自研权重 + 真 Triton；本模型验证
ModelRunner ← attn/sample 链路与确定性采样。

参考形态：vLLM model_executor 前向 + SGLang 薄 runner 消费 batch。
关键差异：KV 仍应归池；C3 用整段重算（无本地 KV 权威）规避未就绪的 arena。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

from engine.attn.backend import AttentionBackend, build_attn_backend


def _lcg(seed: int) -> int:
    return (1103515245 * seed + 12345) & 0x7FFFFFFF


def _rand_matrix(rows: int, cols: int, seed: int) -> List[List[float]]:
    s = seed
    out: List[List[float]] = []
    for _ in range(rows):
        row = []
        for _ in range(cols):
            s = _lcg(s)
            row.append((s / 0x7FFFFFFF) * 0.2 - 0.1)
        out.append(row)
    return out


def _matvec(m: List[List[float]], v: List[float]) -> List[float]:
    return [sum(m[i][j] * v[j] for j in range(len(v))) for i in range(len(m))]


def _add(a: List[float], b: List[float]) -> List[float]:
    return [x + y for x, y in zip(a, b)]


def _layernorm(x: List[float], eps: float = 1e-5) -> List[float]:
    n = len(x)
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / n
    inv = 1.0 / math.sqrt(var + eps)
    return [(v - mean) * inv for v in x]


class TinyLM:
    def __init__(
        self,
        vocab_size: int = 256,
        d_model: int = 32,
        n_heads: int = 4,
        seed: int = 7,
        attn_backend: str = "triton",
        attn: Optional[AttentionBackend] = None,
    ) -> None:
        assert d_model % n_heads == 0
        self.vocab_size = vocab_size
        self.d_model = d_model
        self.n_heads = n_heads
        self.head_dim = d_model // n_heads
        self._attn: AttentionBackend = attn or build_attn_backend(attn_backend)
        self.embed = _rand_matrix(vocab_size, d_model, seed)
        self.wq = _rand_matrix(d_model, d_model, seed + 1)
        self.wk = _rand_matrix(d_model, d_model, seed + 2)
        self.wv = _rand_matrix(d_model, d_model, seed + 3)
        self.wo = _rand_matrix(d_model, d_model, seed + 4)
        self.w_out = _rand_matrix(vocab_size, d_model, seed + 5)

    def _embed_tokens(self, token_ids: Sequence[int]) -> List[List[float]]:
        rows = []
        for t in token_ids:
            tid = int(t) % self.vocab_size
            rows.append(list(self.embed[tid]))
        return rows

    def forward_query_logits(
        self, token_ids: Sequence[int], q_start: int, q_end: int
    ) -> List[List[float]]:
        """返回 query 区间 `[q_start, q_end)` 各位置的 vocab logits（C8 残差路径）。

        无本地 KV cache：每次物化 K/V 前缀 `[0, q_end)`（生产由池 L0 + block table 承接）。
        Q 只算残差段，对齐「只算 `[computed, computed+n)`」。
        """
        if not token_ids or q_end <= q_start:
            return []
        q_start = max(0, q_start)
        q_end = min(len(token_ids), q_end)
        if q_end <= q_start:
            return []
        prefix = token_ids[:q_end]
        x = self._embed_tokens(prefix)
        k = [_matvec(self.wk, h) for h in x]
        v = [_matvec(self.wv, h) for h in x]
        q_rows = [_matvec(self.wq, x[i]) for i in range(q_start, q_end)]
        attn_out = self._attn.forward_queries(q_rows, k, v, q_start)
        logits_list: List[List[float]] = []
        for off, i in enumerate(range(q_start, q_end)):
            h = _layernorm(_add(x[i], _matvec(self.wo, attn_out[off])))
            logits_list.append(
                [sum(row[j] * h[j] for j in range(self.d_model)) for row in self.w_out]
            )
        return logits_list

    def forward_logits(self, token_ids: Sequence[int]) -> List[float]:
        """返回最后位置的 vocab logits。"""
        if not token_ids:
            return [0.0] * self.vocab_size
        rows = self.forward_query_logits(token_ids, len(token_ids) - 1, len(token_ids))
        return rows[-1] if rows else [0.0] * self.vocab_size

    def greedy_token(self, token_ids: Sequence[int]) -> int:
        logits = self.forward_logits(token_ids)
        best_i = 0
        best_v = logits[0]
        for i, v in enumerate(logits):
            if v > best_v:
                best_v = v
                best_i = i
        return best_i
