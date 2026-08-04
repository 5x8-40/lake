"""C2：StorageAgent FFI 契约 + D5 交互序（InMemoryAgent）。"""

from __future__ import annotations

from lake.engine.agents.memory import InMemoryAgent
from lake.engine.pool_iface import PoolIface
from lake.engine.pool_types import (
    FinishRequest,
    PoolError,
    PoolErrorCode,
    PreparePlan,
    ReadyHandle,
)
from lake.runtime.req import Req
from lake.runtime.scheduler_output import ForwardMode, ReqIoSet, SamplingParams, SchedulerOutput


def _plan(step: int, *, read=None, write=None, budget=0, partial=False) -> PreparePlan:
    return PreparePlan(
        step_id=step,
        forward_mode=ForwardMode.DECODE,
        read_set=read or [],
        write_set=write or [],
        num_scheduled_tokens={"r1": 1},
        pull_budget_ms=budget,
        allow_partial_hit=partial,
    )


def test_ready_done_mismatch() -> None:
    ag = InMemoryAgent()
    try:
        ag.done(1)
        raise AssertionError("expected PoolError")
    except PoolError as e:
        assert e.code == PoolErrorCode.PROTOCOL_ERROR


def test_prepare_done_roundtrip() -> None:
    ag = InMemoryAgent()
    plan = _plan(1, write=[ReqIoSet(req_id="r1", token_start=0, token_end=8)])
    ready = ag.prepare_step(plan)
    assert ready.step_id == 1
    ag.done(1)
    assert ag.done_calls == 1


def test_pull_budget_timeout() -> None:
    ag = InMemoryAgent()
    ag.force_pull_reqs.add("r1")
    ag.pull_cost_ms = 30
    plan = _plan(
        1,
        read=[ReqIoSet(req_id="r1", token_start=0, token_end=8)],
        budget=10,
        partial=False,
    )
    try:
        ag.prepare_step(plan)
        raise AssertionError("expected TIMEOUT")
    except PoolError as e:
        assert e.code == PoolErrorCode.TIMEOUT


def test_allow_partial_hit_drops_req() -> None:
    ag = InMemoryAgent()
    ag.force_pull_reqs.add("r1")
    ag.pull_cost_ms = 30
    ag.l0_token_end["r2"] = 4
    plan = _plan(
        1,
        read=[
            ReqIoSet(req_id="r1", token_start=0, token_end=8),
            ReqIoSet(req_id="r2", token_start=0, token_end=4),
        ],
        write=[ReqIoSet(req_id="r2", token_start=4, token_end=5)],
        budget=10,
        partial=True,
    )
    ready = ag.prepare_step(plan)
    assert all(io.req_id != "r1" for io in ready.effective_read_set)
    assert any(io.req_id == "r2" for io in ready.effective_write_set)


def test_deferred_finish_until_done() -> None:
    ag = InMemoryAgent()
    plan = _plan(1, write=[ReqIoSet(req_id="r1", token_start=0, token_end=8)])
    ag.prepare_step(plan)
    ag.on_request_finished(FinishRequest(req_id="r1", node_id="n0"))
    assert ag.finished == []
    ag.done(1)
    assert ag.finished == ["r1"]


def test_commit_write_extent_shrinks() -> None:
    ag = InMemoryAgent()
    ag.l0_token_end["r1"] = 20  # verify 预留高水位
    ag.commit_write_extent("r1", 12)
    assert ag.l0_token_end["r1"] == 12


def test_pool_iface_facade() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag, pull_budget_ms=0)
    req = Req(
        req_id="r1",
        served_model_name="model",
        prompt_token_ids=list(range(8)),
        sampling_params=SamplingParams(max_new_tokens=1),
    )
    out = SchedulerOutput(
        step_id=7,
        forward_mode=ForwardMode.EXTEND,
        num_scheduled_tokens={"r1": 8},
        total_num_scheduled_tokens=8,
        write_set=[ReqIoSet(req_id="r1", token_start=0, token_end=8)],
        req_forward_modes={"r1": ForwardMode.EXTEND},
    )
    ready = pool.prepare_step(out, {"r1": req})
    assert ready.step_id == 7
    pool.done(7)
    pool.on_request_finished(req)
    assert "r1" in ag.finished


def test_pool_iface_commit_calls_agent() -> None:
    ag = InMemoryAgent()
    ag.l0_token_end["r1"] = 10
    pool = PoolIface(ag)
    pool.commit_write_extent("r1", 6)
    assert ag.l0_token_end["r1"] == 6
    assert pool.stats.commit_calls == 1


def test_pool_iface_finish_idempotent() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    req = Req(
        req_id="r1",
        served_model_name="model",
        prompt_token_ids=[],
        sampling_params=SamplingParams(max_new_tokens=1),
    )
    pool.on_request_finished(req)
    pool.on_request_finished(req)
    assert ag.finished == ["r1"]
    assert pool.stats.finish_calls == 1
    assert pool.stats.duplicate_finish_calls == 1


def test_pool_iface_wraps_unexpected_agent_error() -> None:
    class BadAgent:
        def prepare_step(self, plan):
            raise RuntimeError("boom")

        def done(self, step_id):
            return None

        def on_request_finished(self, finish):
            return None

        def commit_write_extent(self, req_id, token_end):
            return None

    pool = PoolIface(BadAgent())  # type: ignore[arg-type]
    out = SchedulerOutput(step_id=1, forward_mode=ForwardMode.DECODE)
    try:
        pool.prepare_step(out, {})
        raise AssertionError("expected PoolError")
    except PoolError as e:
        assert e.code == PoolErrorCode.DOWNSTREAM
    assert pool.stats.last_error_code == PoolErrorCode.DOWNSTREAM


def test_pool_iface_rejects_shrink_without_partial_hit() -> None:
    class ShrinkAgent:
        def prepare_step(self, plan):
            return ReadyHandle(
                step_id=plan.step_id,
                effective_read_set=[ReqIoSet(req_id="r1", token_start=0, token_end=1)],
                effective_write_set=[],
            )

        def done(self, step_id):
            return None

        def on_request_finished(self, finish):
            return None

        def commit_write_extent(self, req_id, token_end):
            return None

    pool = PoolIface(ShrinkAgent(), allow_partial_hit=False)  # type: ignore[arg-type]
    out = SchedulerOutput(
        step_id=1,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"r1": 1, "r2": 1},
        total_num_scheduled_tokens=2,
    )
    try:
        pool.prepare_step(out, {})
        raise AssertionError("expected PoolError")
    except PoolError as e:
        assert e.code == PoolErrorCode.PROTOCOL_ERROR


def test_timeout_preserves_request_exec_mode() -> None:
    ag = InMemoryAgent()
    ag.force_pull_reqs.add("r1")
    ag.pull_cost_ms = 30
    pool = PoolIface(ag, pull_budget_ms=10, allow_partial_hit=False)
    req = Req(
        req_id="r1",
        served_model_name="model",
        prompt_token_ids=list(range(8)),
        sampling_params=SamplingParams(max_new_tokens=1),
    )
    out = SchedulerOutput(
        step_id=1,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"r1": 1},
        total_num_scheduled_tokens=1,
        read_set=[ReqIoSet(req_id="r1", token_start=0, token_end=8)],
    )
    before = req.exec_mode
    try:
        pool.prepare_step(out, {"r1": req})
        raise AssertionError("expected TIMEOUT")
    except PoolError as e:
        assert e.code == PoolErrorCode.TIMEOUT
    assert req.exec_mode == before


if __name__ == "__main__":
    test_ready_done_mismatch()
    test_prepare_done_roundtrip()
    test_pull_budget_timeout()
    test_allow_partial_hit_drops_req()
    test_deferred_finish_until_done()
    test_commit_write_extent_shrinks()
    test_pool_iface_facade()
    test_pool_iface_commit_calls_agent()
    test_pool_iface_finish_idempotent()
    test_pool_iface_wraps_unexpected_agent_error()
    test_pool_iface_rejects_shrink_without_partial_hit()
    test_timeout_preserves_request_exec_mode()
    print("test_pool_agent OK")
