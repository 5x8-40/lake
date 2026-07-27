"""本步静态 / 批 buffer（非跨步请求权威）。

对齐 vLLM V2 `InputBatch` 子集：本步 token 几何 + 供 attn 的上下文切片。
Host `Req` 权威仍在 `node_scheduler`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class InputBatch:
    req_ids: List[str] = field(default_factory=list)
    num_scheduled_tokens: Dict[str, int] = field(default_factory=dict)
    num_computed_tokens: Dict[str, int] = field(default_factory=dict)
    # 每请求：本步前向可见的 token 前缀（长度 = query_end）
    token_ids: Dict[str, List[int]] = field(default_factory=dict)
    query_start: Dict[str, int] = field(default_factory=dict)
    query_end: Dict[str, int] = field(default_factory=dict)
    # prompt 相 vs 生成相（供 sample 跳过 extend）
    is_prompt_phase: Dict[str, bool] = field(default_factory=dict)

    def clear(self) -> None:
        self.req_ids.clear()
        self.num_scheduled_tokens.clear()
        self.num_computed_tokens.clear()
        self.token_ids.clear()
        self.query_start.clear()
        self.query_end.clear()
        self.is_prompt_phase.clear()
