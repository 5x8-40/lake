"""池边界门面：把 SchedulerOutput 编成 PreparePlan，转调 StorageAgent（D2）。

生产：agent = PyO3 `lake-storage-agent`；P3：`GrpcSkeletonAgent`。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from engine.agent import StorageAgent
from engine.agents.memory import InMemoryAgent
from engine.pool_types import FinishRequest, PreparePlan, ReadyHandle, StepStats
from runtime.prefix_hint import PrefixHint
from runtime.req import Req
from runtime.scheduler_output import SchedulerOutput

LOG = logging.getLogger("lake.pool_iface")

# 兼容旧 import（chain_block_hashes / mock_kv_bytes 惰性，避免无 grpc 时 import 炸）
__all__ = [
    "PoolIface",
    "ReadyHandle",
    "StepStats",
    "chain_block_hashes",
    "mock_kv_bytes",
]


def __getattr__(name: str):
    if name in ("chain_block_hashes", "mock_kv_bytes"):
        from engine.agents.grpc_skeleton import chain_block_hashes, mock_kv_bytes

        return chain_block_hashes if name == "chain_block_hashes" else mock_kv_bytes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _grpc_agent_type():
    from engine.agents.grpc_skeleton import GrpcSkeletonAgent

    return GrpcSkeletonAgent


class PoolIface:
    def __init__(
        self,
        agent: StorageAgent,
        *,
        pull_budget_ms: int = 0,
        allow_partial_hit: bool = False,
    ) -> None:
        self._agent = agent
        self.pull_budget_ms = pull_budget_ms
        self.allow_partial_hit = allow_partial_hit
        self._last_ready: Optional[ReadyHandle] = None

    @classmethod
    def from_grpc(cls, cp, kv, **kwargs) -> "PoolIface":
        return cls(_grpc_agent_type()(cp, kv), **kwargs)

    def probe_prefix(self, req: Req) -> PrefixHint:
        """方案 Z：只读命中视图 / Lookup；不放置。"""
        if isinstance(self._agent, InMemoryAgent):
            computed, full = self._agent.probe_local(req.req_id, len(req.prompt_token_ids))
            blocks = computed // 8
            # local_hit = 有本机 L0 前缀（部分亦可 → D-direct 残差）；prebuilt 仅整段
            return PrefixHint(
                computed_tokens=computed,
                reused_blocks=blocks,
                local_hit=computed > 0,
                prebuilt=full,
            )
        if hasattr(self._agent, "probe_prefix"):
            return self._agent.probe_prefix(req)  # type: ignore[attr-defined]
        return PrefixHint()

    def prepare_step(self, output: SchedulerOutput, reqs: Dict[str, Req]) -> ReadyHandle:
        plan = PreparePlan(
            step_id=output.step_id,
            forward_mode=output.forward_mode,
            read_set=list(output.read_set),
            write_set=list(output.write_set),
            num_scheduled_tokens=dict(output.num_scheduled_tokens),
            pull_budget_ms=self.pull_budget_ms,
            allow_partial_hit=self.allow_partial_hit,
        )
        bind = getattr(self._agent, "bind_host_reqs", None)
        if callable(bind):
            bind(reqs)
        handle = self._agent.prepare_step(plan)
        self._last_ready = handle
        return handle

    def done(self, step_id: int) -> None:
        self._agent.done(step_id)
        self._last_ready = None

    def on_request_finished(self, req: Req) -> None:
        self._agent.on_request_finished(
            FinishRequest(req_id=req.req_id, node_id=req.node_id, model_id=req.model_id)
        )

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        """回收写槽高水位（verify 未接受 / 实际产出短于预留）。

        D10：InMemory 已用 prepare 代数防 overlap 误 shrink（见 compute-layer D10）；
        生产仍应对齐 vLLM V2 device 侧会计；GrpcSkeleton 为 no-op。
        """
        if isinstance(self._agent, InMemoryAgent):
            self._agent.commit_write_extent(req_id, token_end)
