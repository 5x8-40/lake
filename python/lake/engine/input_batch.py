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


@dataclass
class InputBuffers:
    """C11 静态 buffer 镜像。

    生产会替换为固定地址 device tensor；当前用 list 保持纯 Python 单测。
    内容按 query-only 展开，对齐 vLLM `InputBuffers` 的职责边界。
    """

    max_num_reqs: int
    max_num_tokens: int
    input_ids: List[int] = field(init=False)
    positions: List[int] = field(init=False)
    query_start_loc: List[int] = field(init=False)
    seq_lens: List[int] = field(init=False)
    is_padding: List[bool] = field(init=False)
    slot_mapping: List[int] = field(init=False)
    req_ids: List[str] = field(default_factory=list)
    num_reqs: int = 0
    num_tokens: int = 0

    def __post_init__(self) -> None:
        if self.max_num_reqs <= 0:
            raise ValueError("max_num_reqs must be > 0")
        if self.max_num_tokens <= 0:
            raise ValueError("max_num_tokens must be > 0")
        self.input_ids = [0] * self.max_num_tokens
        self.positions = [0] * self.max_num_tokens
        self.query_start_loc = [0] * (self.max_num_reqs + 1)
        self.seq_lens = [0] * self.max_num_reqs
        self.is_padding = [True] * self.max_num_tokens
        self.slot_mapping = [-1] * self.max_num_tokens

    def clear(self) -> None:
        self.req_ids = []
        self.num_reqs = 0
        self.num_tokens = 0
        for i in range(self.max_num_reqs + 1):
            self.query_start_loc[i] = 0
        for i in range(self.max_num_reqs):
            self.seq_lens[i] = 0
        for i in range(self.max_num_tokens):
            self.input_ids[i] = 0
            self.positions[i] = 0
            self.is_padding[i] = True
            self.slot_mapping[i] = -1

    def materialize(
        self,
        batch: InputBatch,
        *,
        slot_mapping_by_req: Dict[str, List[int]] | None = None,
    ) -> "InputBuffers":
        """把 `InputBatch` 写入静态 buffer。

        `slot_mapping_by_req` 由 agent 提供。缺省时用 token 绝对位置做逻辑镜像，
        只服务单测；生产必须由 agent 写真实 slot。
        """

        if len(batch.req_ids) > self.max_num_reqs:
            raise ValueError(
                f"num_reqs={len(batch.req_ids)} exceeds max_num_reqs={self.max_num_reqs}"
            )

        self.clear()
        self.req_ids = list(batch.req_ids)
        self.num_reqs = len(batch.req_ids)
        cursor = 0
        slots = slot_mapping_by_req or {}
        for row, req_id in enumerate(batch.req_ids):
            qs = batch.query_start[req_id]
            qe = batch.query_end[req_id]
            q_len = max(0, qe - qs)
            if cursor + q_len > self.max_num_tokens:
                raise ValueError(
                    f"num_tokens={cursor + q_len} exceeds max_num_tokens={self.max_num_tokens}"
                )
            self.query_start_loc[row] = cursor
            self.seq_lens[row] = qe
            tokens = batch.token_ids[req_id][qs:qe]
            req_slots = slots.get(req_id)
            if req_slots is not None and len(req_slots) != q_len:
                raise ValueError(
                    f"slot_mapping len mismatch req={req_id}: {len(req_slots)} != {q_len}"
                )
            for j, token in enumerate(tokens):
                idx = cursor + j
                self.input_ids[idx] = int(token)
                self.positions[idx] = qs + j
                self.is_padding[idx] = False
                self.slot_mapping[idx] = (
                    int(req_slots[j]) if req_slots is not None else qs + j
                )
            cursor += q_len

        self.query_start_loc[self.num_reqs] = cursor
        self.num_tokens = cursor
        for row in range(self.num_reqs + 1, self.max_num_reqs + 1):
            self.query_start_loc[row] = cursor
        return self
