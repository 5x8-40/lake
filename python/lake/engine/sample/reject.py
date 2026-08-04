"""链式拒绝采样（Leviathan / SGLang chain_speculative_sampling 简化版）。

参考:SGLang `speculative/reject_sampling.py::chain_speculative_sampling_triton`；
vLLM RejectionSampler。C4：贪心 target 与 draft 逐位比对，遇分歧停并补 bonus。
"""

from __future__ import annotations

from typing import Callable, List, Sequence


def chain_reject_sample(
    context: Sequence[int],
    draft_tokens: Sequence[int],
    target_greedy: Callable[[Sequence[int]], int],
) -> List[int]:
    """返回本步接受的 token 列表（含分歧处的 target bonus，长度 ∈ [1, len(draft)+1]）。

    约定：对每个 draft[i]，用 target 在 context+accepted 上的 greedy 与之比较；
    全中则再追加 1 个 bonus greedy token。
    """
    accepted: List[int] = []
    ctx = list(context)
    for d in draft_tokens:
        t = target_greedy(ctx)
        if t != int(d):
            accepted.append(t)
            return accepted
        accepted.append(int(d))
        ctx.append(int(d))
    # 全部命中 → bonus
    accepted.append(target_greedy(ctx))
    return accepted
