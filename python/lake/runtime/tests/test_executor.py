"""C15 runtime executor 草案测试。"""

from __future__ import annotations

from typing import Dict, List

from lake.engine.agents.memory import InMemoryAgent
from lake.engine.model_runner import ModelRunner, ModelRunnerOutput
from lake.engine.pool_iface import PoolIface, ReadyHandle, StepStats
from lake.runtime.executor import ExecutorInput, SingleProcessExecutor
from lake.runtime.node_scheduler import NodeScheduler, build_req_from_generate
from lake.runtime.role import RoleConfig
from lake.runtime.scheduler_output import ForwardMode, ReqIoSet, SchedulerOutput
from lake.runtime.worker_engine import WorkerEngine


class FakePool:
    def __init__(self) -> None:
        self.finished: List[str] = []
        self.done_steps: List[int] = []

    def prepare_step(self, output: SchedulerOutput, reqs: Dict) -> ReadyHandle:
        stats = {}
        for rid in output.num_scheduled_tokens:
            req = reqs[rid]
            mode = output.req_forward_modes.get(rid, output.forward_mode)
            if mode == ForwardMode.EXTEND:
                stats[rid] = StepStats(reused_blocks=0, prefill_blocks=1)
            else:
                stats[rid] = StepStats()
        return ReadyHandle(step_id=output.step_id, stats_by_req=stats)

    def done(self, step_id: int) -> None:
        self.done_steps.append(step_id)

    def on_request_finished(self, req) -> None:
        self.finished.append(req.req_id)

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        return None


class RecordingExecutor:
    def __init__(self) -> None:
        self.inputs: List[ExecutorInput] = []

    def execute_model(self, inp: ExecutorInput) -> ModelRunnerOutput:
        self.inputs.append(inp)
        return ModelRunnerOutput(step_id=inp.output.step_id)


class FailingExecutor:
    def execute_model(self, inp: ExecutorInput) -> ModelRunnerOutput:
        raise RuntimeError(f"boom step={inp.output.step_id}")


def test_single_process_executor_calls_runner() -> None:
    class FakeRunner:
        def __init__(self) -> None:
            self.calls: List[ExecutorInput] = []

        def execute_model(self, output, ready, host_reqs):
            self.calls.append(ExecutorInput(output=output, ready=ready, host_reqs=host_reqs))
            return ModelRunnerOutput(step_id=output.step_id, model_backend="fake")

    runner = FakeRunner()
    executor = SingleProcessExecutor(runner)  # type: ignore[arg-type]
    output = SchedulerOutput(step_id=3, forward_mode=ForwardMode.DECODE)
    ready = ReadyHandle(step_id=3)
    out = executor.execute_model(ExecutorInput(output=output, ready=ready, host_reqs={}))
    assert out.step_id == 3
    assert runner.calls[0].output is output


def test_node_scheduler_uses_executor() -> None:
    pool = FakePool()
    runner = ModelRunner(pool, model_backend="mock")  # type: ignore[arg-type]
    executor = RecordingExecutor()
    role = RoleConfig(model_backend="mock", enable_overlap=False)
    sched = NodeScheduler(pool, runner, role, executor=executor)  # type: ignore[arg-type]
    sched.add_request(build_req_from_generate("r1", "m", list(range(4)), 1, "n0"))

    output = sched.schedule()
    sched._run_batch(output)  # noqa: SLF001

    assert len(executor.inputs) == 1
    assert executor.inputs[0].output.step_id == output.step_id
    assert "r1" in executor.inputs[0].host_reqs
    assert pool.done_steps == [output.step_id]


def test_node_scheduler_done_on_executor_exception() -> None:
    pool = FakePool()
    runner = ModelRunner(pool, model_backend="mock")  # type: ignore[arg-type]
    role = RoleConfig(model_backend="mock", enable_overlap=False)
    sched = NodeScheduler(pool, runner, role, executor=FailingExecutor())  # type: ignore[arg-type]
    sched.add_request(build_req_from_generate("r-boom", "m", list(range(4)), 1, "n0"))
    output = sched.schedule()

    try:
        sched._run_batch(output)  # noqa: SLF001
        raise AssertionError("expected executor failure")
    except RuntimeError as e:
        assert "boom" in str(e)
    assert pool.done_steps == [output.step_id]
    assert not sched._result_queue  # noqa: SLF001


def test_node_scheduler_done_when_effective_set_filter_fails() -> None:
    class BadFilterScheduler(NodeScheduler):
        def _respect_effective_sets(self, output, ready):  # type: ignore[no-untyped-def]
            raise RuntimeError("bad effective sets")

    ag = InMemoryAgent()
    pool = PoolIface(ag)
    runner = ModelRunner(pool, model_backend="mock")
    role = RoleConfig(model_backend="mock", enable_overlap=False)
    sched = BadFilterScheduler(pool, runner, role)
    req = build_req_from_generate("r-filter", "m", list(range(4)), 1, "n0")
    sched.add_request(req)
    output = SchedulerOutput(
        step_id=11,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={req.req_id: 1},
        total_num_scheduled_tokens=1,
        write_set=[ReqIoSet(req_id=req.req_id, token_start=3, token_end=4)],
        req_forward_modes={req.req_id: ForwardMode.DECODE},
        req_num_computed_at_schedule={req.req_id: 4},
        req_query_start={req.req_id: 3},
        req_query_end={req.req_id: 4},
    )

    try:
        sched._run_batch(output)  # noqa: SLF001
        raise AssertionError("expected effective set failure")
    except RuntimeError as e:
        assert "bad effective sets" in str(e)
    assert ag.done_calls == 1
    assert ag._ready_step is None  # noqa: SLF001
    assert not sched._result_queue  # noqa: SLF001


def test_worker_engine_wires_executor() -> None:
    pool = FakePool()
    runner = ModelRunner(pool, model_backend="mock")  # type: ignore[arg-type]
    executor = RecordingExecutor()
    role = RoleConfig(model_backend="mock", enable_overlap=False)
    eng = WorkerEngine(pool, runner, role, coalesce_s=0, executor=executor)  # type: ignore[arg-type]
    eng.start()
    try:
        done = eng.submit(build_req_from_generate("r2", "m", list(range(4)), 1, "n0"))
        assert done.finished
        assert executor.inputs
    finally:
        eng.stop()


if __name__ == "__main__":
    test_single_process_executor_calls_runner()
    test_node_scheduler_uses_executor()
    test_node_scheduler_done_on_executor_exception()
    test_worker_engine_wires_executor()
    print("test_executor OK")
