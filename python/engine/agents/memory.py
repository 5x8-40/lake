"""InMemoryAgent：无 gRPC 的 StorageAgent，供单测与本地骨架。

模拟：L0 放置集合、补拉延迟、ready/done、overlap 下延迟 on_request_finished。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

from engine.pool_types import (
    FinishRequest,
    PoolError,
    PoolErrorCode,
    PreparePlan,
    ReadyHandle,
    StepStats,
)

LOG = logging.getLogger("lake.agent.memory")


class InMemoryAgent:
    def __init__(self) -> None:
        # req_id → 已在「L0」的 token 上界（半开）
        self.l0_token_end: Dict[str, int] = {}
        self._ready_step: Optional[int] = None
        self._frozen_reqs: Set[str] = set()
        self._deferred_finish: List[FinishRequest] = []
        self.finished: List[str] = []
        # 测试注入：prepare 时这些 req 的 read 视为 miss，需「补拉」
        self.force_pull_reqs: Set[str] = set()
        # 测试注入：补拉耗时（逻辑 ms）；与 plan.pull_budget_ms 比较
        self.pull_cost_ms: int = 0
        self.prepare_calls = 0
        self.done_calls = 0
        # D10：自上次 commit 以来的 prepare 次数；>1 说明有更新的 prepare，禁止 shrink
        self._prepares_since_commit: Dict[str, int] = {}

    def seed_local_prefix(self, req_id: str, token_end: int) -> None:
        """测试/预放置：把 [0, token_end) 标为已在本机 L0（D-direct）。"""
        self.l0_token_end[req_id] = max(self.l0_token_end.get(req_id, 0), token_end)

    def probe_local(self, req_id: str, prompt_len: int) -> tuple[int, bool]:
        """返回 (computed_tokens, full_local)。

        computed>0 ⇒ 有本机前缀（D-direct）；full_local ⇒ 整段在 L0（computed=prompt_len）。
        """
        have = self.l0_token_end.get(req_id, 0)
        computed = min(have, prompt_len)
        full = prompt_len > 0 and have >= prompt_len
        return computed, full

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        """将 L0 写高水位收到实际 token_end（回收未接受的 verify 预留槽）。

        D10：对齐 vLLM V2 device 侧接受长度会计——host ``min`` 不得压后续 prepare
        已抬高的预留。若自上次 commit 后又发生过更新的 prepare，则跳过 shrink
        （残留高水位最多保留到 ``on_request_finished``）。
        """
        end = max(0, token_end)
        n = self._prepares_since_commit.get(req_id, 0)
        if n > 1:
            self._prepares_since_commit[req_id] = n - 1
            return
        if req_id in self.l0_token_end:
            self.l0_token_end[req_id] = min(self.l0_token_end[req_id], end)
        self._prepares_since_commit[req_id] = 0
    def prepare_step(self, plan: PreparePlan) -> ReadyHandle:
        self.prepare_calls += 1
        if self._ready_step is not None:
            raise PoolError(PoolErrorCode.PROTOCOL_ERROR, f"prepare while ready={self._ready_step}")

        # overlap：先冲刷已无冻结引用的延迟结束
        self._flush_deferred_finish()

        stats: Dict[str, StepStats] = {}
        eff_read = list(plan.read_set)
        eff_write = list(plan.write_set)

        # 1) read_set：本地命中 or 补拉
        pull_ms = 0
        for io in plan.read_set:
            have = self.l0_token_end.get(io.req_id, 0)
            need_pull = io.req_id in self.force_pull_reqs or have < io.token_end
            if need_pull:
                pull_ms += self.pull_cost_ms
                if plan.pull_budget_ms > 0 and pull_ms > plan.pull_budget_ms:
                    if plan.allow_partial_hit:
                        eff_read = [x for x in eff_read if x.req_id != io.req_id]
                        eff_write = [x for x in eff_write if x.req_id != io.req_id]
                        LOG.info("partial drop req=%s budget exceeded", io.req_id)
                        continue
                    raise PoolError(
                        PoolErrorCode.TIMEOUT,
                        f"pull budget {plan.pull_budget_ms}ms exceeded for {io.req_id}",
                    )
                self.l0_token_end[io.req_id] = max(have, io.token_end)
                st = stats.setdefault(io.req_id, StepStats())
                st.pulled_blocks += 1
            self._frozen_reqs.add(io.req_id)
            stats.setdefault(io.req_id, StepStats())

        # 2) write_set：分配「slot」（扩展 l0 上界）
        for io in plan.write_set:
            if io.req_id not in {x.req_id for x in eff_write}:
                continue
            self.l0_token_end[io.req_id] = max(self.l0_token_end.get(io.req_id, 0), io.token_end)
            self._prepares_since_commit[io.req_id] = self._prepares_since_commit.get(io.req_id, 0) + 1
            self._frozen_reqs.add(io.req_id)
            st = stats.setdefault(io.req_id, StepStats())
            if io.token_start == 0 and io.token_end > 0:
                st.prefill_blocks = max(st.prefill_blocks, 1)

        self._ready_step = plan.step_id
        return ReadyHandle(
            step_id=plan.step_id,
            stats_by_req=stats,
            effective_read_set=eff_read,
            effective_write_set=eff_write,
        )

    def done(self, step_id: int) -> None:
        self.done_calls += 1
        if self._ready_step is None or self._ready_step != step_id:
            raise PoolError(PoolErrorCode.PROTOCOL_ERROR, f"done step={step_id} ready={self._ready_step}")
        self._ready_step = None
        # step 结束解冻（简化：清空本步冻结；生产按 block ref）
        self._frozen_reqs.clear()
        self._flush_deferred_finish()
    def on_request_finished(self, finish: FinishRequest) -> None:
        if finish.req_id in self._frozen_reqs or self._ready_step is not None:
            # overlap：本步还在用 / 下一 ready 未完成 → 延迟归还
            self._deferred_finish.append(finish)
            LOG.debug("defer finish req=%s", finish.req_id)
            return
        self._apply_finish(finish)

    def _flush_deferred_finish(self) -> None:
        if self._ready_step is not None or self._frozen_reqs:
            return
        pending = self._deferred_finish
        self._deferred_finish = []
        for fin in pending:
            self._apply_finish(fin)

    def _apply_finish(self, finish: FinishRequest) -> None:
        self.l0_token_end.pop(finish.req_id, None)
        self._prepares_since_commit.pop(finish.req_id, None)
        self.finished.append(finish.req_id)
