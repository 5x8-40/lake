"""TaskEngine 检查：协程消费者、传输闸、幂等、track 收编、优雅退出。

fake KvOps（离线可跑）：脚本化 exists/copy/query_task/abandon。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from conftest import run
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
from kvcm.task_engine import IntentStatus, TaskEngine, intent_id_of

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")
_H = TaskHandle(op=OpKind.COPY, task_ids=((1, 101),))


class FakeOps(KvOps):
    def __init__(
        self,
        *,
        exists: bool = True,
        seq=(),
        query_gate: threading.Event | None = None,
        handle_fn=None,
        on_copy=None,
    ):
        self._exists = exists
        self._seq = list(seq)
        self._gate = query_gate
        self._handle_fn = handle_fn or (lambda _p: _H)
        self._on_copy = on_copy
        self.abandoned: list[TaskHandle] = []
        self.copy_count = 0

    def capability(self) -> BackendCapability:
        raise NotImplementedError

    async def exists(self, prefix) -> bool:
        return self._exists

    async def copy(self, prefix, target) -> TaskHandle:
        self.copy_count += 1
        if self._on_copy is not None:
            self._on_copy(prefix)
        return self._handle_fn(prefix)

    async def query_task(self, handle) -> TaskStatus:
        if handle in self.abandoned:
            raise KeyError("unknown task handle")  # 后端 abandon 后即遗忘
        if self._gate is not None:
            # 协程里不能阻塞等线程事件：轮询代替（测试用）
            deadline = time.monotonic() + 5
            while not self._gate.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        nxt = self._seq.pop(0) if self._seq else TaskStatus.READY
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def abandon(self, handle) -> None:
        self.abandoned.append(handle)

    def prefix_of(self, handle):
        return _P


class FakeKeepalive:
    def __init__(self) -> None:
        self.registered: list[Prefix] = []

    def register(self, prefix: Prefix) -> None:
        self.registered.append(prefix)


def _engine(ops, **kw) -> TaskEngine:
    return TaskEngine(ops, **kw)


# ---------------------------------------------------------------------------
# 直接执行（run_once）
# ---------------------------------------------------------------------------


def test_run_once_ready_registers_keepalive() -> None:
    ka = FakeKeepalive()
    eng = _engine(FakeOps(seq=[TaskStatus.READY]), keepalive=ka)
    run(eng.run_once(_P, _T, poll_s=0.01))
    assert ka.registered == [_P]  # READY 接线：保活注册


def test_run_once_failure_does_not_register() -> None:
    ka = FakeKeepalive()
    eng = _engine(FakeOps(seq=[TaskStatus.FAILED]), keepalive=ka)
    with pytest.raises(PrefetchError):
        run(eng.run_once(_P, _T, poll_s=0.01))
    assert ka.registered == []


def test_transfer_gate_limits_concurrency() -> None:
    """传输闸限制同时执行的任务数（max_concurrent=2，跑 5 个）。

    度量方式：任务在 flow 内的「进入 copy → 离开 run」这一段区间里，
    并发数不得超过闸名额。用计数器的进入/离开配对来测。
    """
    active = {"now": 0, "peak": 0}

    class GateOps(FakeOps):
        async def copy(self, prefix, target):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.05)  # 模拟一段占用
            return await super().copy(prefix, target)

        async def query_task(self, handle):
            try:
                return await super().query_task(handle)
            finally:
                pass

    async def scenario():
        ops = GateOps(seq=[])
        eng = TaskEngine(ops, max_concurrent=2)

        async def one(i):
            try:
                await eng.run_once(Prefix(model_id="q", token_ids=(i,)), _T, poll_s=0.01)
            finally:
                active["now"] -= 1

        await asyncio.gather(*(one(i) for i in range(5)), return_exceptions=True)

    run(scenario())
    assert active["peak"] <= 2, f"传输闸失效: peak={active['peak']}"


# ---------------------------------------------------------------------------
# intent 提交 / 幂等 / 回执
# ---------------------------------------------------------------------------


def test_submit_intent_dedupes_in_flight() -> None:
    """同键在途：第二次提交返回同一 intent_id，不重复执行。"""

    async def scenario():
        gate = threading.Event()  # 让任务卡住，保持「在途」
        eng = TaskEngine(FakeOps(query_gate=gate))
        eng.start()
        try:
            id1 = eng.submit_intent(_P, _T, poll_s=0.01)
            id2 = eng.submit_intent(_P, _T, poll_s=0.01)
            assert id1 == id2
            assert eng.intent_status(id1) is IntentStatus.PENDING
        finally:
            gate.set()
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_submit_intent_after_terminal_restarts() -> None:
    """同键已终态：视为新意图，重新预热（返回同一 key，但会再跑一次）。"""

    async def scenario():
        ops = FakeOps(seq=[TaskStatus.READY, TaskStatus.READY])
        eng = TaskEngine(ops)
        eng.start()
        try:
            key = eng.submit_intent(_P, _T, poll_s=0.01)
            for _ in range(100):
                if eng.intent_status(key) is not IntentStatus.PENDING:
                    break
                await asyncio.sleep(0.01)
            assert eng.intent_status(key) is IntentStatus.READY
            eng.submit_intent(_P, _T, poll_s=0.01)  # 终态后重新提交
            for _ in range(100):
                if ops.copy_count >= 2:
                    break
                await asyncio.sleep(0.01)
            assert ops.copy_count == 2
        finally:
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_intent_detail_contains_failure_reason() -> None:
    """失败必须带原因（真机排障教训：不带原因的回执等于没有回执）。"""

    async def scenario():
        eng = TaskEngine(FakeOps(seq=[TaskStatus.FAILED]))
        eng.start()
        try:
            key = eng.submit_intent(_P, _T, poll_s=0.01)
            for _ in range(100):
                if eng.intent_status(key) is not IntentStatus.PENDING:
                    break
                await asyncio.sleep(0.01)
            assert eng.intent_status(key) is IntentStatus.FAILED
            assert "PrefetchError" in eng.intent_detail(key)
        finally:
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_intent_status_unknown_returns_none() -> None:
    eng = TaskEngine(FakeOps())
    assert eng.intent_status("nope") is None
    assert eng.intent_detail("nope") == ""


def test_intent_id_is_prefix_times_target() -> None:
    """幂等键 = 前缀 × 目标实例（D1 §4）。"""
    a = intent_id_of(_P, InstanceId("inst-A"))
    b = intent_id_of(_P, InstanceId("inst-B"))
    assert a != b
    assert a == intent_id_of(_P, InstanceId("inst-A"))


# ---------------------------------------------------------------------------
# track（rebuild 恢复的句柄收编）
# ---------------------------------------------------------------------------


def test_track_ready_registers_keepalive() -> None:
    ka = FakeKeepalive()
    eng = _engine(FakeOps(seq=[TaskStatus.READY]), keepalive=ka)
    run(eng.track(_H, poll_s=0.01))
    assert ka.registered == [_P]


def test_track_without_prefix_skips_keepalive() -> None:
    """prefix_of 返回 None（句柄已终态）：不注册保活，不报错。"""
    ka = FakeKeepalive()

    class NoPrefixOps(FakeOps):
        def prefix_of(self, handle):
            return None

    eng = _engine(NoPrefixOps(seq=[TaskStatus.READY]), keepalive=ka)
    run(eng.track(_H, poll_s=0.01))
    assert ka.registered == []


def test_track_query_error_retries_then_ready() -> None:
    ka = FakeKeepalive()
    eng = _engine(
        FakeOps(seq=[QueryError("rpc boom"), TaskStatus.READY]), keepalive=ka
    )
    run(eng.track(_H, poll_s=0.01))
    assert ka.registered == [_P]


def test_track_verify_error_stops() -> None:
    ka = FakeKeepalive()
    eng = _engine(FakeOps(seq=[VerifyError("missing")]), keepalive=ka)
    run(eng.track(_H, poll_s=0.01))
    assert ka.registered == []  # 核验失败不注册


def test_track_keyerror_stops_tracking() -> None:
    ka = FakeKeepalive()
    eng = _engine(FakeOps(seq=[KeyError("gone")]), keepalive=ka)
    run(eng.track(_H, poll_s=0.01))
    assert ka.registered == []


def test_track_timeout_abandons() -> None:
    ops = FakeOps(seq=[])  # 永远 RUNNING（seq 空 → 默认 READY，改用显式）
    ops._seq = [TaskStatus.RUNNING]

    class AlwaysRunning(FakeOps):
        async def query_task(self, handle):
            return TaskStatus.RUNNING

    ops = AlwaysRunning(seq=[])
    eng = _engine(ops)
    run(eng.track(_H, poll_s=0.01, timeout_s=0.05))
    assert ops.abandoned == [_H]


# ---------------------------------------------------------------------------
# 优雅退出
# ---------------------------------------------------------------------------


def test_shutdown_stops_accepting() -> None:
    async def scenario():
        eng = TaskEngine(FakeOps())
        eng.start()
        await eng.shutdown(timeout_s=1)
        with pytest.raises(RuntimeError, match="shutting down"):
            eng.submit_intent(_P, _T)

    run(scenario())


def test_shutdown_cancels_in_flight() -> None:
    """在途任务被取消：终态 FAILED，detail 写明 cancelled。"""

    async def scenario():
        gate = threading.Event()
        eng = TaskEngine(FakeOps(query_gate=gate))
        eng.start()
        key = eng.submit_intent(_P, _T, poll_s=0.01)
        await asyncio.sleep(0.05)  # 让它进入执行
        await eng.shutdown(timeout_s=2)
        assert eng.intent_status(key) is IntentStatus.FAILED
        assert "cancelled" in eng.intent_detail(key)

    run(scenario())


def test_shutdown_cancels_queued_intents() -> None:
    """队列里还没被取走的 intent：退出时标记失败，不让调用方永远 PENDING。"""

    async def scenario():
        gate = threading.Event()
        eng = TaskEngine(FakeOps(query_gate=gate), max_concurrent=1, consumer_count=1)
        eng.start()
        keys = [eng.submit_intent(Prefix(model_id="q", token_ids=(i,)), _T, poll_s=0.01)
                for i in range(3)]
        await asyncio.sleep(0.05)
        await eng.shutdown(timeout_s=2)
        for k in keys:
            assert eng.intent_status(k) is IntentStatus.FAILED

    run(scenario())


def test_max_concurrent_must_be_positive() -> None:
    with pytest.raises(ValueError):
        TaskEngine(FakeOps(), max_concurrent=0)
