"""Host Req —— 请求权威只在 node_scheduler（对齐 SGLang，非 vLLM RequestState）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from lake.runtime.exec_mode import ExecMode
from lake.runtime.prefix_hint import PrefixHint
from lake.runtime.scheduler_output import SamplingParams


@dataclass
class Req:
    req_id: str
    served_model_name: str
    prompt_token_ids: List[int]
    sampling_params: SamplingParams
    node_id: str = "worker-0"
    # 调度/复用状态（权威在此，不进 ModelRunner）
    num_computed_tokens: int = 0
    output_token_ids: List[int] = field(default_factory=list)
    reused_blocks: int = 0
    prefill_blocks: int = 0
    finished: bool = False
    finish_reason: Optional[str] = None
    exec_mode: ExecMode = ExecMode.COLOCATED

    @property
    def num_output_tokens(self) -> int:
        return len(self.output_token_ids)

    @property
    def all_token_ids(self) -> List[int]:
        return list(self.prompt_token_ids) + list(self.output_token_ids)

    def apply_prefix_hint(self, hint: PrefixHint) -> None:
        self.num_computed_tokens = min(hint.computed_tokens, len(self.prompt_token_ids))
        self.reused_blocks = hint.reused_blocks
