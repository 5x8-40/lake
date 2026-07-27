"""C10：Worker 生命周期 + 容量信号上报。"""

from __future__ import annotations

import threading
import time

from engine.model_runner import ModelRunner
from runtime.lifecycle import WorkerLifecycle, WorkerState
from runtime.node_scheduler import build_req_from_generate
from runtime.role import RoleConfig
from runtime.worker_engine import WorkerEngine, _Inbound


class FakePool:
    def prepare_step(self, output, reqs):
        from engine.pool_iface import ReadyHandle

        return ReadyHandle(step_id=output.step_id)

    def done(self, step_id: int) -> None:
        return None

    def on_request_finished(self, req) -> None:
        return None

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        return None


def test_lifecycle_forward_and_drain() -> None:
    life = WorkerLifecycle()
    life.warm()
    assert life.state == WorkerState.WARM
    life.ready()
    life.serve()
    assert life.accepts_new_requests()
    life.drain()
    assert not life.accepts_new_requests()


def test_engine_capacity_signal() -> None:
    pool = FakePool()
    runner = ModelRunner(pool)  # type: ignore[arg-type]
    eng = WorkerEngine(pool, runner, RoleConfig(model_backend="mock"), coalesce_s=0)  # type: ignore[arg-type]
    eng.start()
    try:
        sig = eng.capacity_signal()
        assert sig.state == WorkerState.SERVING
        assert sig.max_running_reqs == 8
        assert sig.remaining_slots == 8
        assert sig.inflight_reqs == 0
        assert sig.role == "hybrid"
    finally:
        eng.stop()
    assert eng.lifecycle.state == WorkerState.TERMINATE


def test_stop_never_started() -> None:
    """自审：从未 start 的 engine 调 stop 不得 join 未启动线程。"""
    pool = FakePool()
    runner = ModelRunner(pool)  # type: ignore[arg-type]
    eng = WorkerEngine(pool, runner, RoleConfig(model_backend="mock"), coalesce_s=0)  # type: ignore[arg-type]
    eng.stop()  # 不得抛 "cannot join thread before it is started"
    assert eng.lifecycle.state == WorkerState.TERMINATE
    # stop 幂等
    eng.stop()
    assert eng.lifecycle.state == WorkerState.TERMINATE


def test_stop_fails_orphaned_inflight() -> None:
    """stop 后必须唤醒仍卡在 done.wait 的 inflight（审出：哨兵抢先会孤儿化）。"""
    pool = FakePool()
    runner = ModelRunner(pool)  # type: ignore[arg-type]
    eng = WorkerEngine(pool, runner, RoleConfig(model_backend="mock"), coalesce_s=0)  # type: ignore[arg-type]
    eng.start()
    req = build_req_from_generate("orphan", "m", list(range(4)), 2, "n0")
    item = _Inbound(req=req, hint=None, done=threading.Event(), error=[], result=[])
    eng._inflight[req.req_id] = item  # noqa: SLF001
    eng.scheduler.add_request(req)
    eng.stop(timeout=2.0)
    assert item.done.is_set()
    assert item.error and "stopped" in str(item.error[0]).lower()
    assert eng.lifecycle.state == WorkerState.TERMINATE


def test_stop_timeout_does_not_clear_inflight() -> None:
    """High：join 超时后调用方不得清 scheduler/inflight（避免与活 step 竞态）。"""
    pool = FakePool()
    runner = ModelRunner(pool)  # type: ignore[arg-type]
    eng = WorkerEngine(pool, runner, RoleConfig(model_backend="mock"), coalesce_s=0)  # type: ignore[arg-type]
    release = threading.Event()
    entered = threading.Event()

    def blocked(before_schedule=None, should_stop=None):  # noqa: ANN001
        entered.set()
        # 模拟卡在长 step：忽略 should_stop，直到测试放行
        while not release.is_set():
            time.sleep(0.01)

    # 先挂 stub，再 start/加 work，避免真实 run_until_idle 抢跑
    eng._sched.run_until_idle = blocked  # type: ignore[method-assign]  # noqa: SLF001
    eng.start()
    req = build_req_from_generate("stuck", "m", list(range(4)), 2, "n0")
    item = _Inbound(req=req, hint=None, done=threading.Event(), error=[], result=[])
    eng._inflight[req.req_id] = item  # noqa: SLF001
    eng.scheduler.add_request(req)
    assert entered.wait(timeout=2.0)
    eng.stop(timeout=0.05)
    assert eng._thread.is_alive()  # noqa: SLF001
    assert req.req_id in eng._inflight  # noqa: SLF001 — 调用方未清表
    assert not item.done.is_set()
    assert eng.lifecycle.state == WorkerState.DRAIN  # 未进 TERMINATE
    release.set()
    eng._thread.join(timeout=2.0)  # noqa: SLF001
    assert item.done.is_set()  # loop 自己 fail
    assert item.error and "stopped" in str(item.error[0]).lower()


def test_submit_after_stop_raises() -> None:
    """Medium：stop 后 submit 必须立刻失败，不得永久 wait。"""
    pool = FakePool()
    runner = ModelRunner(pool)  # type: ignore[arg-type]
    eng = WorkerEngine(pool, runner, RoleConfig(model_backend="mock"), coalesce_s=0)  # type: ignore[arg-type]
    eng.start()
    eng.stop()
    try:
        eng.submit(build_req_from_generate("late", "m", list(range(4)), 1, "n0"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "not running" in str(e).lower()


def test_submit_stop_race_no_hang() -> None:
    """Medium：submit 与 stop 交错时，要么完成要么 raise，不得 hang。"""
    pool = FakePool()
    runner = ModelRunner(pool)  # type: ignore[arg-type]
    eng = WorkerEngine(pool, runner, RoleConfig(model_backend="mock"), coalesce_s=0)  # type: ignore[arg-type]
    eng.start()
    barrier = threading.Barrier(2)
    errors: list = []

    def _submit() -> None:
        barrier.wait()
        try:
            eng.submit(build_req_from_generate("race", "m", list(range(8)), 1, "n0"))
        except RuntimeError as e:
            errors.append(e)

    t = threading.Thread(target=_submit)
    t.start()
    barrier.wait()
    eng.stop(timeout=2.0)
    t.join(timeout=2.0)
    assert not t.is_alive(), "submit hung after stop"
