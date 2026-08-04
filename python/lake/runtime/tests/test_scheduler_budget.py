"""C7：token_budget / chunked extend / max_model_length admission。"""

from __future__ import annotations

from typing import Dict, List, Tuple

from lake.engine.model_runner import ModelRunner
from lake.engine.pool_iface import ReadyHandle, StepStats
from lake.runtime.node_scheduler import NodeScheduler, build_req_from_generate
from lake.runtime.role import RoleConfig
from lake.runtime.scheduler_output import ForwardMode, SchedulerOutput


class FakePool:
    def __init__(self) -> None:
        self.finished: List[str] = []
        self.extend_ns: List[int] = []

    def prepare_step(self, output: SchedulerOutput, reqs: Dict) -> ReadyHandle:
        for rid, n in output.num_scheduled_tokens.items():
            if output.req_forward_modes.get(rid, output.forward_mode) == ForwardMode.EXTEND:
                self.extend_ns.append(n)
        stats = {
            rid: StepStats(reused_blocks=0, prefill_blocks=1 if n else 0)
            for rid, n in output.num_scheduled_tokens.items()
        }
        return ReadyHandle(step_id=output.step_id, stats_by_req=stats)

    def done(self, step_id: int) -> None:
        return None

    def on_request_finished(self, req) -> None:
        self.finished.append(req.req_id)

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        return None


def _make(role: RoleConfig) -> Tuple[NodeScheduler, FakePool]:
    pool = FakePool()
    runner = ModelRunner(pool, model_backend=role.model_backend)  # type: ignore[arg-type]
    return NodeScheduler(pool, runner, role), pool  # type: ignore[arg-type]


def test_chunked_extend_advances_computed() -> None:
    role = RoleConfig(
        model_backend="mock",
        enable_overlap=False,
        long_prefill_token_threshold=4,
        max_num_scheduled_tokens=64,
    )
    sched, pool = _make(role)
    prompt = list(range(10))  # 10 tokens → chunks 4+4+2
    sched.add_request(build_req_from_generate("c1", "m", prompt, 2, "n0"))
    sched.run_until_idle()
    done = sched.get_req("c1")
    assert done.finished
    assert done.num_output_tokens == 2
    assert pool.extend_ns == [4, 4, 2]
    assert pool.finished == ["c1"]


def test_token_budget_caps_batch() -> None:
    """两请求各需 8 token extend；budget=8 → 同步只能调度其一（或合计≤8）。"""
    role = RoleConfig(
        model_backend="mock",
        enable_overlap=False,
        max_num_scheduled_tokens=8,
        long_prefill_token_threshold=0,
        max_running_reqs=4,
    )
    sched, pool = _make(role)
    sched.add_request(build_req_from_generate("a", "m", list(range(8)), 1, "n0"))
    sched.add_request(build_req_from_generate("b", "m", list(range(8, 16)), 1, "n0"))
    out = sched.schedule()
    assert out.total_num_scheduled_tokens <= 8
    assert out.forward_mode == ForwardMode.EXTEND
    # 预算只够一个完整 prompt
    assert len(out.num_scheduled_tokens) == 1
    assert sum(out.num_scheduled_tokens.values()) == 8
    # 跑完仍应两者都完成
    sched._run_batch(out)  # noqa: SLF001
    sched._pop_and_process()  # noqa: SLF001
    sched.run_until_idle()
    assert sched.get_req("a").finished and sched.get_req("b").finished


def test_decode_priority_over_extend() -> None:
    """running 里已有 decode 时，先占预算做 decode，再用剩余预算 chunk extend。"""
    role = RoleConfig(
        model_backend="mock",
        enable_overlap=False,
        max_num_scheduled_tokens=4,
        long_prefill_token_threshold=0,
        max_running_reqs=4,
    )
    sched, _ = _make(role)
    # a 已算完 prompt，进入生成
    ra = build_req_from_generate("a", "m", list(range(4)), 3, "n0")
    ra.num_computed_tokens = 4
    sched.add_request(ra)
    # b 还要 extend 8 tokens
    sched.add_request(build_req_from_generate("b", "m", list(range(8)), 1, "n0"))
    out = sched.schedule()
    assert out.req_forward_modes["a"] == ForwardMode.DECODE
    assert out.num_scheduled_tokens["a"] == 1
    # decode 占 1 后剩 3 → b 被 budget 切成 3（非整段 8）
    assert out.num_scheduled_tokens["b"] == 3
    assert out.req_forward_modes["b"] == ForwardMode.EXTEND
    assert out.forward_mode == ForwardMode.MIXED
    assert out.total_num_scheduled_tokens == 4


def test_decode_read_write_sets_target_query_token() -> None:
    role = RoleConfig(
        model_backend="mock",
        enable_overlap=False,
        max_num_scheduled_tokens=4,
    )
    sched, _ = _make(role)
    req = build_req_from_generate("d", "m", list(range(4)), 1, "n0")
    req.num_computed_tokens = 4
    sched.add_request(req)

    out = sched.schedule()
    assert out.req_forward_modes["d"] == ForwardMode.DECODE
    assert out.num_scheduled_tokens["d"] == 1
    assert [(io.token_start, io.token_end) for io in out.read_set] == [(0, 3)]
    assert [(io.token_start, io.token_end) for io in out.write_set] == [(3, 4)]
    assert out.req_query_start["d"] == 3
    assert out.req_query_end["d"] == 4


def test_decode_query_geometry_includes_inflight_overlap() -> None:
    role = RoleConfig(
        model_backend="mock",
        enable_overlap=True,
        max_num_scheduled_tokens=4,
    )
    sched, _ = _make(role)
    req = build_req_from_generate("o", "m", list(range(4)), 3, "n0")
    req.num_computed_tokens = 4
    sched.add_request(req)
    sched._running.append(sched._waiting.pop(0))  # noqa: SLF001
    sched._inflight_decode["o"] = 1  # noqa: SLF001

    out = sched.schedule()
    assert out.req_forward_modes["o"] == ForwardMode.DECODE
    assert [(io.token_start, io.token_end) for io in out.write_set] == [(4, 5)]
    assert out.req_query_start["o"] == 4
    assert out.req_query_end["o"] == 5


def test_admission_rejects_over_max_model_length() -> None:
    role = RoleConfig(max_model_length=10, model_backend="mock")
    sched, _ = _make(role)
    try:
        sched.add_request(build_req_from_generate("x", "m", list(range(8)), 5, "n0"))
    except ValueError as e:
        assert "max_model_length" in str(e)
    else:
        raise AssertionError("expected ValueError for over-length request")