"""模式选择纯函数骨架（生产权威在 Go Router；此处供节点自测 / P3 回填）。

对齐 docs/architecture/execution-modes.md / features.md：
- **本地命中**（前缀已在本机 L0，含部分）→ D-direct，零/极小传输
- **整段**本地命中 → `full_local_hit`：把 `num_computed` 提到 prompt_len，本步按 vLLM
  几何直接进入生成（prepare 保证 read；无 SGLang PREBUILT 分相）
- 无本地命中 + 专用 Prefill/Decode 角色 → PD；否则混部
失败不设 mode-to-mode fallback。
"""

from __future__ import annotations

from lake.runtime.exec_mode import ExecMode
from lake.runtime.prefix_hint import PrefixHint
from lake.runtime.role import WorkerRole


def select_exec_mode(
    hint: PrefixHint,
    *,
    prompt_len: int,
    role: WorkerRole = WorkerRole.HYBRID,
) -> ExecMode:
    # D-direct = 前缀 KV 已在执行节点 HBM（部分命中仍算；残差用 token 几何 schedule）
    if prompt_len > 0 and hint.local_hit and hint.computed_tokens > 0:
        return ExecMode.D_DIRECT
    if role in (WorkerRole.PREFILL, WorkerRole.DECODE):
        return ExecMode.PD_DISAGG
    return ExecMode.COLOCATED


def full_local_hit(hint: PrefixHint, prompt_len: int) -> bool:
    """整段前缀已在 L0 → 可将 num_computed 置为 prompt_len（无独立 PREBUILT 态）。"""
    return bool(
        prompt_len > 0
        and (hint.prebuilt or (hint.local_hit and hint.computed_tokens >= prompt_len))
    )


# 兼容旧名
should_prebuilt = full_local_hit
