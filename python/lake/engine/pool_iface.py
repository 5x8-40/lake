"""池边界门面：把 SchedulerOutput 编成 PreparePlan，转调 StorageAgent（D2）。

生产：agent = PyO3 `lake-storage-agent`；P3：`GrpcSkeletonAgent`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from lake.engine.agent import StorageAgent
from lake.engine.agents.memory import InMemoryAgent
from lake.engine.pool_types import (
    FinishRequest,
    PoolError,
    PoolErrorCode,
    PreparePlan,
    ReadyHandle,
    StepStats,
)
from lake.runtime.prefix_hint import PrefixHint
from lake.runtime.req import Req
from lake.runtime.scheduler_output import SchedulerOutput

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
        from lake.engine.agents.grpc_skeleton import chain_block_hashes, mock_kv_bytes

        return chain_block_hashes if name == "chain_block_hashes" else mock_kv_bytes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _grpc_agent_type():
    from lake.engine.agents.grpc_skeleton import GrpcSkeletonAgent

    return GrpcSkeletonAgent


@dataclass
class PoolIfaceStats:
    prepare_calls: int = 0
    done_calls: int = 0
    commit_calls: int = 0
    finish_calls: int = 0
    duplicate_finish_calls: int = 0
    error_calls: int = 0
    last_error_code: Optional[PoolErrorCode] = None


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
        self._finished_req_ids: set[str] = set()
        self._stats = PoolIfaceStats()

    @property
    def stats(self) -> PoolIfaceStats:
        return self._stats

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
        self._stats.prepare_calls += 1
        handle = self._call_agent("prepare_step", lambda: self._agent.prepare_step(plan))
        self._validate_ready(output, handle)
        self._last_ready = handle
        return handle

    def done(self, step_id: int) -> None:
        self._stats.done_calls += 1
        self._call_agent("done", lambda: self._agent.done(step_id))
        self._last_ready = None

    def on_request_finished(self, req: Req) -> None:
        if req.req_id in self._finished_req_ids:
            self._stats.duplicate_finish_calls += 1
            LOG.debug("skip duplicate finish req=%s", req.req_id)
            return
        self._stats.finish_calls += 1
        self._call_agent(
            "on_request_finished",
            lambda: self._agent.on_request_finished(
                FinishRequest(
                    req_id=req.req_id,
                    node_id=req.node_id,
                    model_id=req.served_model_name,
                )
            ),
        )
        self._finished_req_ids.add(req.req_id)

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        """回收写槽高水位（verify 未接受 / 实际产出短于预留）。

        D10：InMemory 已用 prepare 代数防 overlap 误 shrink（见 compute-layer D10）；
        生产 agent 须实现同名方法；老 mock 无该方法时按 no-op 兼容。
        """
        if token_end < 0:
            raise PoolError(PoolErrorCode.INVALID_ARG, f"token_end must be >=0: {token_end}")
        commit = getattr(self._agent, "commit_write_extent", None)
        if not callable(commit):
            LOG.debug("agent has no commit_write_extent; no-op req=%s", req_id)
            return
        self._stats.commit_calls += 1
        self._call_agent("commit_write_extent", lambda: commit(req_id, token_end))

    def _validate_ready(self, output: SchedulerOutput, handle: ReadyHandle) -> None:
        if handle.step_id != output.step_id:
            raise PoolError(
                PoolErrorCode.PROTOCOL_ERROR,
                f"ready step={handle.step_id} != output step={output.step_id}",
            )

        planned = set(output.num_scheduled_tokens)
        effective_sets = [handle.effective_read_set, handle.effective_write_set]
        seen_effective = set()
        has_effective = False
        for ios in effective_sets:
            if ios is None:
                continue
            has_effective = True
            for io in ios:
                if io.token_end < io.token_start:
                    raise PoolError(
                        PoolErrorCode.PROTOCOL_ERROR,
                        f"invalid io range req={io.req_id}: {io.token_start}>{io.token_end}",
                    )
                if io.req_id not in planned:
                    raise PoolError(
                        PoolErrorCode.PROTOCOL_ERROR,
                        f"effective set contains unscheduled req={io.req_id}",
                    )
                seen_effective.add(io.req_id)

        if has_effective and not self.allow_partial_hit and seen_effective != planned:
            missing = sorted(planned - seen_effective)
            raise PoolError(
                PoolErrorCode.PROTOCOL_ERROR,
                f"agent shrank batch while allow_partial_hit=false missing={missing}",
            )

        for req_id in handle.stats_by_req:
            if req_id not in planned:
                raise PoolError(
                    PoolErrorCode.PROTOCOL_ERROR,
                    f"stats contain unscheduled req={req_id}",
                )

    def _call_agent(self, op: str, fn):
        try:
            return fn()
        except PoolError as e:
            self._stats.error_calls += 1
            self._stats.last_error_code = e.code
            raise
        except Exception as e:  # noqa: BLE001
            self._stats.error_calls += 1
            self._stats.last_error_code = PoolErrorCode.DOWNSTREAM
            raise PoolError(PoolErrorCode.DOWNSTREAM, f"{op}: {e}") from e
