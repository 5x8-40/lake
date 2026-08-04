"""C13 structured output 采样占位。

生产路径会把 packed bitmask 搬到 device 并在 logits 上原地 apply；纯 Python
骨架用 bool list 表达 allowed token 集合，便于单测固定接口语义。
"""

from __future__ import annotations

from math import inf
from typing import List, Sequence


def apply_token_bitmask(logits: Sequence[float], bitmask: Sequence[bool]) -> List[float]:
    """返回屏蔽后的 logits；False 的 token 置为 -inf。"""

    if len(logits) != len(bitmask):
        raise ValueError(f"bitmask length {len(bitmask)} != logits length {len(logits)}")
    if not any(bitmask):
        raise ValueError("grammar bitmask rejects all tokens")
    return [float(v) if allowed else -inf for v, allowed in zip(logits, bitmask)]
