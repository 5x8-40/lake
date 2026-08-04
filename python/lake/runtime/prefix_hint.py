"""前缀命中提示——Router / probe 的输入，供节点组 batch（方案 Z 只读）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PrefixHint:
    computed_tokens: int = 0  # 已可复用 token 数（半开上界语义：前 computed 已算）
    reused_blocks: int = 0
    local_hit: bool = False  # 有本机 L0 前缀（含部分；D-direct 条件，≠ 整段）
    prebuilt: bool = False  # True：整段已在 L0（调度侧将 computed 提到 prompt_len）
