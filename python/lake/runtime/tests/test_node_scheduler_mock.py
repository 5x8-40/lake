"""C0/C1：node_scheduler continuous batching + overlap 冒烟（Fake pool）。"""

from __future__ import annotations

from typing import Dict, List, Tuple

from lake.engine.model_runner import ModelRunner, ModelRunnerOutput
from lake.engine.pool_iface import ReadyHandle, StepStats
from lake.runtime.node_scheduler import NodeScheduler, _BatchResult, build_req_from_generate
from lake.runtime.role import RoleConfig
from lake.runtime.scheduler_output import ForwardMode, SchedulerOutput


class FakePool:
    def __init__(self) -> None:
        self.finished: List[str] = []
        self.prepared_modes: List[ForwardMode] = []

    def prepare_step(self, output: SchedulerOutput, reqs: Dict) -> ReadyHandle:
        self.prepared_modes.append(output.forward_mode)
        stats = {}
        for rid in output.num_scheduled_tokens:
            req = reqs[rid]
            phase = output.req_forward_modes.get(rid, output.forward_mode)
            if phase == ForwardMode.EXTEND:
                nblocks = (len(req.prompt_token_ids) + 7) // 8
                # 只回 StepStats；由 scheduler._apply_ready_stats 写入 Host Req
                st = StepStats(reused_blocks=0, prefill_blocks=nblocks)
            else:
                # 模拟「decode 误报 reused」：不得覆盖冷启动 reused==0
                st = StepStats(reused_blocks=99, prefill_blocks=0)
            stats[rid] = st
        return ReadyHandle(step_id=output.step_id, stats_by_req=stats)

    def done(self, step_id: int) -> None:
        return None

    def on_request_finished(self, req) -> None:
        self.finished.append(req.req_id)

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        return None


def _make_sched(overlap: bool = True, max_running: int = 8) -> Tuple[NodeScheduler, FakePool]:
    pool = FakePool()
    runner = ModelRunner(pool, model_backend="mock")  # type: ignore[arg-type]
    role = RoleConfig(
        model_backend="mock",
        enable_overlap=overlap,
        max_running_reqs=max_running,
    )
    return NodeScheduler(pool, runner, role), pool  # type: ignore[arg-type]


def test_extend_then_decode_finishes() -> None:
    sched, pool = _make_sched()
    req = build_req_from_generate("r1", "mock-llm", list(range(16)), max_new_tokens=3, node_id="n0")
    sched.add_request(req)
    sched.run_until_idle()
    done = sched.get_req("r1")
    assert done.finished
    assert done.num_output_tokens == 3
    assert done.output_token_ids == [1016, 1017, 1018]
    assert pool.finished == ["r1"]
    assert ForwardMode.EXTEND in pool.prepared_modes
    assert ForwardMode.DECODE in pool.prepared_modes
    # 冷启动：EXTEND prefill>0 写入后，decode 步 reused=99 不得覆盖
    assert done.reused_blocks == 0
    assert done.prefill_blocks == 2


def test_build_req_requires_served_model_name() -> None:
    try:
        build_req_from_generate("missing-model", "", [1, 2], 1, "n0")
        raise AssertionError("expected missing served_model_name")
    except ValueError as e:
        assert "served_model_name is required" in str(e)


def test_continuous_batching_two_reqs() -> None:
    sched, pool = _make_sched(max_running=4)
    sched.add_request(build_req_from_generate("a", "m", list(range(8)), 2, "n0"))
    sched.add_request(build_req_from_generate("b", "m", list(range(8, 16)), 2, "n0"))
    sched.run_until_idle()
    assert sched.get_req("a").finished and sched.get_req("b").finished
    assert set(pool.finished) == {"a", "b"}
    # 至少有一步同批（EXTEND 双请求或 MIXED）
    assert any(m in (ForwardMode.EXTEND, ForwardMode.MIXED, ForwardMode.DECODE) for m in pool.prepared_modes)


def test_overlap_process_lags_execute() -> None:
    """多请求 decode 稳态：execute(N) 先入队，再 process 上批（对齐 event_loop_overlap）。

    单请求 EXTEND 不可重入，会先 drain 再 decode，看不出重叠；用双请求 decode 段验证。
    """
    sched, _ = _make_sched(overlap=True)
    sched.add_request(build_req_from_generate("a", "m", list(range(8)), 3, "n0"))
    sched.add_request(build_req_from_generate("b", "m", list(range(8, 16)), 3, "n0"))
    sched.run_until_idle()
    tl = sched.timeline
    # 存在某次：execute(s) 出现在 process(s) 之前，且中间夹了另一次 execute
    found = False
    for i, (op, step) in enumerate(tl):
        if op != "process":
            continue
        # 同一 step 的 execute 必须更早
        assert ("execute", step) in tl[:i]
        # 在 process(step) 之前，应已启动过更新的 execute（重叠）
        later_exec = [s for o, s in tl[:i] if o == "execute" and s > step]
        if later_exec:
            found = True
            break
    assert found, tl


def test_sync_loop_process_before_next_execute() -> None:
    sched, _ = _make_sched(overlap=False)
    sched.add_request(build_req_from_generate("r1", "m", list(range(8)), 2, "n0"))
    sched.run_until_idle()
    tl = sched.timeline
    # 同步：process(1) 在 execute(2) 之前
    assert tl.index(("process", 1)) < tl.index(("execute", 2)), tl


def test_future_map_holds_last_token() -> None:
    sched, _ = _make_sched(overlap=True)
    sched.add_request(build_req_from_generate("r1", "m", [1, 2, 3, 4, 5, 6, 7, 8], 2, "n0"))
    sched.run_until_idle()
    done = sched.get_req("r1")
    # 结束后 FutureMap 已 clear
    assert sched.future_map.resolve("r1") is None
    assert done.output_token_ids[-1] == 1010  # seed=8 → 1009,1010


def test_schedule_marks_structured_output() -> None:
    sched, _ = _make_sched(overlap=False)
    req = build_req_from_generate("g1", "m", [1, 2, 3, 4], 1, "n0")
    req.num_computed_tokens = len(req.prompt_token_ids)
    req.sampling_params.structured_output = "json"
    sched.add_request(req)

    out = sched.schedule()
    assert out.forward_mode == ForwardMode.DECODE
    assert out.has_structured_output is True
    assert out.grammar_output is not None
    assert out.grammar_output.req_ids == ["g1"]


def test_spec_and_structured_output_disables_overlap() -> None:
    sched, _ = _make_sched(overlap=True)
    req = build_req_from_generate("g2", "m", [1, 2, 3, 4], 3, "n0")
    req.num_computed_tokens = len(req.prompt_token_ids)
    req.sampling_params.structured_output = "json"
    sched.add_request(req)
    sched._running.append(sched._waiting.pop(0))  # noqa: SLF001
    sched._pending_drafts["g2"] = [7, 8]  # noqa: SLF001
    out = SchedulerOutput(
        step_id=1,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"g2": 1},
        total_num_scheduled_tokens=1,
        req_forward_modes={"g2": ForwardMode.DECODE},
        req_num_computed_at_schedule={"g2": len(req.prompt_token_ids)},
    )
    sched._result_queue.append(  # noqa: SLF001
        _BatchResult(
            output=out,
            runner_out=ModelRunnerOutput(step_id=1),
            ready=ReadyHandle(step_id=1),
        )
    )

    assert sched._should_disable_overlap() is True  # noqa: SLF001


def test_respect_effective_sets_drops_req() -> None:
    """P2-1：prepare 缩批后 scheduler 不得再 execute 被丢的 req。"""
    from lake.engine.agents.memory import InMemoryAgent
    from lake.engine.pool_iface import PoolIface
    from lake.runtime.role import RoleConfig
    from lake.runtime.scheduler_output import ForwardMode, ReqIoSet, SchedulerOutput

    ag = InMemoryAgent()
    ag.force_pull_reqs.add("drop")
    ag.pull_cost_ms = 50
    ag.l0_token_end["keep"] = 4  # keep 本地已齐，无需补拉
    pool = PoolIface(ag, pull_budget_ms=10, allow_partial_hit=True)
    role = RoleConfig(model_backend="mock", enable_overlap=False)
    runner = ModelRunner(pool, model_backend="mock")
    sched = NodeScheduler(pool, runner, role)

    out = SchedulerOutput(
        step_id=1,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"drop": 1, "keep": 1},
        total_num_scheduled_tokens=2,
        read_set=[
            ReqIoSet(req_id="drop", token_start=0, token_end=8),
            ReqIoSet(req_id="keep", token_start=0, token_end=4),
        ],
        write_set=[ReqIoSet(req_id="keep", token_start=4, token_end=5)],
        req_forward_modes={"drop": ForwardMode.DECODE, "keep": ForwardMode.DECODE},
    )
    ready = pool.prepare_step(out, {})
    filtered = sched._respect_effective_sets(out, ready)  # noqa: SLF001
    assert "drop" not in filtered.num_scheduled_tokens
    assert "keep" in filtered.num_scheduled_tokens
    pool.done(out.step_id)


def test_respect_effective_sets_drops_all() -> None:
    """P1：allow_partial_hit 把全批丢掉时，effective_*=[] 不得当成「未缩批」。

    旧实现 ``not eff_read and not eff_write`` 把全空视为未缩批 → 缺块 req 仍进算。
    现 None=未缩批、[]=缩批至空 → 应降为 IDLE。
    """
    from lake.engine.agents.memory import InMemoryAgent
    from lake.engine.pool_iface import PoolIface
    from lake.runtime.role import RoleConfig
    from lake.runtime.scheduler_output import ForwardMode, ReqIoSet, SchedulerOutput

    ag = InMemoryAgent()
    ag.force_pull_reqs.update("a", "b")  # 两 req 都需补拉
    ag.pull_cost_ms = 50
    pool = PoolIface(ag, pull_budget_ms=10, allow_partial_hit=True)
    role = RoleConfig(model_backend="mock", enable_overlap=False)
    runner = ModelRunner(pool, model_backend="mock")
    sched = NodeScheduler(pool, runner, role)

    out = SchedulerOutput(
        step_id=2,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"a": 1, "b": 1},
        total_num_scheduled_tokens=2,
        read_set=[
            ReqIoSet(req_id="a", token_start=0, token_end=8),
            ReqIoSet(req_id="b", token_start=0, token_end=8),
        ],
        write_set=[
            ReqIoSet(req_id="a", token_start=8, token_end=9),
            ReqIoSet(req_id="b", token_start=8, token_end=9),
        ],
        req_forward_modes={"a": ForwardMode.DECODE, "b": ForwardMode.DECODE},
    )
    ready = pool.prepare_step(out, {})
    # agent 把两 req 都因预算超限缩掉 → effective_*=[]（非 None）
    assert ready.effective_read_set == []
    assert ready.effective_write_set == []

    filtered = sched._respect_effective_sets(out, ready)  # noqa: SLF001
    assert filtered.forward_mode == ForwardMode.IDLE
    assert filtered.num_scheduled_tokens == {}
    assert filtered.total_num_scheduled_tokens == 0
    # 被丢 req 的 inflight 须回滚（下步重试）
    assert sched._inflight_decode.get("a", 0) == 0  # noqa: SLF001
    assert sched._inflight_decode.get("b", 0) == 0  # noqa: SLF001
    pool.done(out.step_id)


if __name__ == "__main__":
    test_extend_then_decode_finishes()
    test_continuous_batching_two_reqs()
    test_overlap_process_lags_execute()
    test_sync_loop_process_before_next_execute()
    test_future_map_holds_last_token()
    test_schedule_marks_structured_output()
    test_spec_and_structured_output_disables_overlap()
    test_respect_effective_sets_drops_req()
    test_respect_effective_sets_drops_all()
    print("test_node_scheduler_mock OK")
