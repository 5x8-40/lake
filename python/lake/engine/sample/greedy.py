"""贪心采样（C3）。完整 sampling 参数见 docs/research/sampling-params.md（D7）。"""

from __future__ import annotations

from typing import List


def greedy_sample(logits: List[float]) -> int:
    best_i = 0
    best_v = logits[0]
    for i, v in enumerate(logits):
        if v > best_v:
            best_v = v
            best_i = i
    return best_i
