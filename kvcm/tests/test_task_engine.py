"""TaskEngine 单元检查：并发上限、READY 保活接线、track 收编、退出收口。

fake KvOps（离线可跑）：脚本化 exists/copy/query_task/abandon。
"""

from __future__ import annotations

import threading
import time

import pytest

from kvcm.flows.prefetch import PrefetchError
from kvcm.ops.base import (
    KvOps,
    OpKind,
    QueryError,
    TaskHandle,
    TaskStatus,
    VerifyError,
)
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, PinKind, Prefix
from kvcm.task_engine import TaskEngine

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")
_H = TaskHandle(op=OpKind.COPY, task_ids=((1, 101),))


class FakeOps(KvOps):
    def __init__(self, *, exists=True, seq=(), query_gate: threading.Event | None = None):
        self._exists = exists
        self._seq = list(seq)
        self._gate = query_gate  # 设置后 query_task 阻塞在此事件上（测并发/在途）
        self.abandoned: list[TaskHandle] = []
        self.copy_count = 0
        self._lock = threading.Lock()

    def capability(self) -> BackendCapability:
        raise NotImplementedError

    def exists(self, prefix) -> bool:
        return self._exists

    def copy(self, prefix, target) -> TaskHandle:
        with self._lock:
            self.copy_count += 1
        return _H

    def query_task(self, handle) -> TaskStatus:
        if handle in self.abandoned:
            raise KeyError("unknown task handle")  # 后端 abandon 后即遗忘
        if self._gate is not None:
            self._gate.wait(timeout=5)
        nxt = self._seq.pop(0) if self._seq else TaskStatus.READY
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def abandon(self, handle) -> None:
        self.abandoned.append(handle)

    def move(self, prefix, target, source=None):
        raise NotImplementedError

    def pin(self, prefix, kind: PinKind = PinKind.SOFT):
        raise NotImplementedError

    def demote(self, prefix):
        raise NotImplementedError

    def promote(self, prefix, target):
        raise NotImplementedError

    def retain(self, prefix):
        raise NotImplementedError

    def remove(self, prefix):
        raise NotImplementedError


class FakeKeepalive:
    def __init__(self) -> None:
        self.registered: list[Prefix] = []

    def register(self, prefix: Prefix) -> None:
        self.registered.append(prefix)


# ---------------------------------------------------------------------------
# submit / READY 接线
# ---------------------------------------------------------------------------


def test_submit_sync_ready_registers_keepalive() -> None:
    ka = FakeKeepalive()
    eng = TaskEngine(FakeOps(seq=[TaskStatus.READY]), keepalive=ka)
    eng.submit_sync(_P, _T, poll_s=0.01)
    assert ka.registered == [_P]  # READY 接线：保活注册
    eng.shutdown()


def test_failure_does_not_register_and_propagates() -> None:
    ka = FakeKeepalive()
    eng = TaskEngine(FakeOps(seq=[TaskStatus.FAILED]), keepalive=ka)
    with pytest.raises(PrefetchError):
        eng.submit_sync(_P, _T, poll_s=0.01)
    assert ka.registered == []
    eng.shutdown()


def test_concurrency_limit_and_queue() -> None:
    """并发上限 = 2：三个 intent 同发，任一时刻 copy 执行不超过 2。"""
    gate = threading.Event()
    ops = FakeOps(seq=[TaskStatus.READY] * 9, query_gate=gate)
    eng = TaskEngine(ops, max_concurrent=2)
    futs = [eng.submit(_P, _T, poll_s=0.01) for _ in range(3)]
    deadline = time.monotonic() + 2
    while ops.copy_count < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ops.copy_count == 2  # 第三个在队列里
    gate.set()
    for f in futs:
        f.result(timeout=5)
    assert ops.copy_count == 3
    eng.shutdown()


def test_limiter_backpressure_then_release() -> None:
    """限速器钩子：False 期间不发起，True 后执行（E15 接入点）。"""
    allow = {"v": False}
    ops = FakeOps(seq=[TaskStatus.READY])
    eng = TaskEngine(ops, limiter=lambda: allow["v"])
    fut = eng.submit(_P, _T, poll_s=0.01)
    time.sleep(0.1)
    assert ops.copy_count == 0  # 背压留在 worker
    allow["v"] = True
    fut.result(timeout=5)
    assert ops.copy_count == 1
    eng.shutdown()


def test_in_flight_visible_during_run_then_cleared() -> None:
    """handle_sink：run 进行中句柄在在途集，结束即清。"""
    gate = threading.Event()
    ops = FakeOps(seq=[TaskStatus.READY], query_gate=gate)
    eng = TaskEngine(ops)
    fut = eng.submit(_P, _T, poll_s=0.01)
    deadline = time.monotonic() + 2
    while not eng._in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert eng._in_flight == {_H}
    gate.set()
    fut.result(timeout=5)
    deadline = time.monotonic() + 2
    while eng._in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert eng._in_flight == set()
    eng.shutdown()


# ---------------------------------------------------------------------------
# track（rebuild 恢复收编）
# ---------------------------------------------------------------------------


def test_track_runs_to_terminal() -> None:
    ops = FakeOps(seq=[TaskStatus.RUNNING, TaskStatus.READY])
    eng = TaskEngine(ops)
    eng.track(_H, poll_s=0.01).result(timeout=5)
    assert ops.abandoned == []
    eng.shutdown()


def test_track_query_error_retried() -> None:
    ops = FakeOps(seq=[QueryError("boom"), TaskStatus.READY])
    eng = TaskEngine(ops)
    eng.track(_H, poll_s=0.01).result(timeout=5)
    assert ops.abandoned == []
    eng.shutdown()


def test_track_timeout_abandons() -> None:
    ops = FakeOps(seq=[TaskStatus.RUNNING] * 100)
    eng = TaskEngine(ops)
    eng.track(_H, poll_s=0.01, timeout_s=0.05).result(timeout=5)
    assert ops.abandoned == [_H]
    eng.shutdown()


def test_track_verify_error_stops_tracking() -> None:
    ops = FakeOps(seq=[VerifyError("replica missing")])
    eng = TaskEngine(ops)
    eng.track(_H, poll_s=0.01).result(timeout=5)
    assert ops.abandoned == []
    eng.shutdown()


def test_track_keyerror_stops_tracking() -> None:
    """句柄已被收口（shutdown abandon 后）→ track 安静结束。"""
    ops = FakeOps(seq=[KeyError("unknown task handle")])
    eng = TaskEngine(ops)
    eng.track(_H, poll_s=0.01).result(timeout=5)
    assert ops.abandoned == []
    eng.shutdown()


# ---------------------------------------------------------------------------
# shutdown（决策四②③）
# ---------------------------------------------------------------------------


def test_shutdown_stops_accepting_and_abandons_in_flight() -> None:
    gate = threading.Event()
    ops = FakeOps(seq=[TaskStatus.RUNNING] * 100, query_gate=gate)
    eng = TaskEngine(ops)
    fut = eng.submit(_P, _T, poll_s=0.01, timeout_s=60)
    deadline = time.monotonic() + 2
    while not eng._in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    eng.shutdown()
    gate.set()
    assert ops.abandoned == [_H]  # 在途 abandon（记账不等待）
    with pytest.raises(RuntimeError, match="shutting down"):
        eng.submit(_P, _T)
    # 工作线程内流程因句柄已收口而结束（退出期 KeyError 被吞，安静收场）
    fut.result(timeout=5)
