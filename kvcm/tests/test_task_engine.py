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
from kvcm.task_engine import IntentStatus, TaskEngine

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")
_H = TaskHandle(op=OpKind.COPY, task_ids=((1, 101),))


class FakeOps(KvOps):
    def __init__(self, *, exists=True, seq=(), query_gate: threading.Event | None = None,
                 handle_fn=None):
        self._exists = exists
        self._seq = list(seq)
        self._gate = query_gate  # 设置后 query_task 阻塞在此事件上（测并发/在途）
        self._handle_fn = handle_fn or (lambda _p: _H)
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
        return self._handle_fn(prefix)

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


def test_concurrent_intents_distinct_prefixes_no_crosstalk() -> None:
    """多 intent 并发：不同 prefix 各自跑通、各自注册保活，互不干扰。"""
    ka = FakeKeepalive()
    ops = FakeOps(
        seq=[TaskStatus.READY] * 8,
        handle_fn=lambda p: TaskHandle(op=OpKind.COPY, task_ids=((p.token_ids[0], 1),)),
    )
    eng = TaskEngine(ops, keepalive=ka, max_concurrent=3)
    prefixes = [Prefix(model_id="qwen", token_ids=(i, i + 1)) for i in range(3)]
    futs = [eng.submit(p, _T, poll_s=0.01) for p in prefixes]
    for f in futs:
        f.result(timeout=5)
    assert ops.copy_count == 3
    assert sorted(ka.registered, key=lambda p: p.token_ids) == prefixes
    assert eng._in_flight == set()
    eng.shutdown()


# ---------------------------------------------------------------------------
# intent 注册表（E12：幂等键 + 状态查询）
# ---------------------------------------------------------------------------


def test_intent_dedup_and_status_transitions() -> None:
    """在途去重返回同键；状态 PENDING→READY；未知键 None。"""
    gate = threading.Event()
    ops = FakeOps(seq=[TaskStatus.READY], query_gate=gate)
    eng = TaskEngine(ops)
    k1 = eng.submit_intent(_P, _T, poll_s=0.01)
    k2 = eng.submit_intent(_P, _T, poll_s=0.01)
    assert k1 == k2  # 幂等键 = 前缀 × 目标实例
    assert eng.intent_status(k1) is IntentStatus.PENDING
    assert eng.intent_status("no-such") is None
    gate.set()
    deadline = time.monotonic() + 5
    while eng.intent_status(k1) is IntentStatus.PENDING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert eng.intent_status(k1) is IntentStatus.READY
    assert ops.copy_count == 1
    eng.shutdown()


def test_intent_resubmit_after_terminal_reexecutes() -> None:
    """同键已终态 → 视为新意图重新执行（预热语义，不是永久去重）。"""
    ops = FakeOps(seq=[TaskStatus.READY, TaskStatus.READY])
    eng = TaskEngine(ops)
    k1 = eng.submit_intent(_P, _T, poll_s=0.01)
    deadline = time.monotonic() + 5
    while eng.intent_status(k1) is IntentStatus.PENDING and time.monotonic() < deadline:
        time.sleep(0.01)
    k2 = eng.submit_intent(_P, _T, poll_s=0.01)
    assert k2 == k1  # 键不变，执行重来
    deadline = time.monotonic() + 5
    while eng.intent_status(k2) is IntentStatus.PENDING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ops.copy_count == 2
    eng.shutdown()


def test_intent_failed_status() -> None:
    eng = TaskEngine(FakeOps(seq=[TaskStatus.FAILED]))
    k = eng.submit_intent(_P, _T, poll_s=0.01)
    deadline = time.monotonic() + 5
    while eng.intent_status(k) is IntentStatus.PENDING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert eng.intent_status(k) is IntentStatus.FAILED
    # 失败回执必须带原因（真机排障教训：空 detail 等于没有回执）
    assert eng.intent_detail(k)
    eng.shutdown()


def test_intent_detail_empty_when_not_failed() -> None:
    eng = TaskEngine(FakeOps(seq=[TaskStatus.READY]))
    k = eng.submit_intent(_P, _T, poll_s=0.01)
    deadline = time.monotonic() + 5
    while eng.intent_status(k) is IntentStatus.PENDING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert eng.intent_status(k) is IntentStatus.READY
    assert eng.intent_detail(k) == ""
    assert eng.intent_detail("no-such") == ""
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


class _PrefixAwareOps(FakeOps):
    """prefix_of 已接线的后端（恢复 READY 保活注册的来源）。"""

    def prefix_of(self, handle) -> Prefix | None:
        return _P


def test_track_ready_registers_keepalive() -> None:
    """恢复任务 READY → 注册保活（重启前来不及注册，靠本路径补上）。"""
    ka = FakeKeepalive()
    eng = TaskEngine(_PrefixAwareOps(seq=[TaskStatus.READY]), keepalive=ka)
    eng.track(_H, poll_s=0.01).result(timeout=5)
    assert ka.registered == [_P]
    eng.shutdown()


def test_track_ready_without_prefix_skips_keepalive() -> None:
    """后端未接线 prefix_of（None）→ 退化不保活，不报错。"""
    ka = FakeKeepalive()
    eng = TaskEngine(FakeOps(seq=[TaskStatus.READY]), keepalive=ka)
    eng.track(_H, poll_s=0.01).result(timeout=5)
    assert ka.registered == []
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
