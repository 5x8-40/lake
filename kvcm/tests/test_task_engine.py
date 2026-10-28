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


async def _wait_registered(eng, key, timeout=2.0):
    """等 intent 记录出现（submit_intent 是投递式：返回时可能还没入队）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if eng.intent_status(key) is not None:
            return True
        await asyncio.sleep(0.005)
    return False


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
    # 上限：不能超过闸名额
    assert active["peak"] <= 2, f"传输闸失效: peak={active['peak']}"
    # 且必须真到过 2（否则闸被误设成 1 也能过）
    assert active["peak"] == 2, f"闸没跑满（峰值 {active['peak']}）——闸可能被设小了"


# ---------------------------------------------------------------------------
# intent 提交 / 幂等 / 回执
# ---------------------------------------------------------------------------


def test_submit_intent_dedupes_in_flight() -> None:
    """同键在途：第二次提交返回同一 intent_id，不重复执行。"""

    async def scenario():
        gate = threading.Event()  # 让任务卡住，保持「在途」
        ops = FakeOps(query_gate=gate)
        eng = TaskEngine(ops)
        eng.start()
        try:
            id1 = eng.submit_intent(_P, _T, poll_s=0.01)
            assert await _wait_registered(eng, id1)
            # 等它真的开始执行（copy 被调用）
            deadline = time.monotonic() + 2
            while ops.copy_count == 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert ops.copy_count == 1
            id2 = eng.submit_intent(_P, _T, poll_s=0.01)
            assert id1 == id2
            # 关键：去重 = 不重复执行（不是只比 id——id 是纯函数的恒定输出）
            await asyncio.sleep(0.1)
            assert ops.copy_count == 1, f"在途重复提交导致重复执行: {ops.copy_count}"
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
            for _ in range(200):
                if eng.intent_status(key) is IntentStatus.READY:
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
            for _ in range(200):
                if eng.intent_status(key) is IntentStatus.FAILED:
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
    """在途任务被取消：终态 FAILED，detail 是「在途取消」而非「从未启动」，
    且句柄被 abandon（账本不留 running）。"""

    async def scenario():
        gate = threading.Event()
        ops = FakeOps(query_gate=gate)
        eng = TaskEngine(ops)
        eng.start()
        key = eng.submit_intent(_P, _T, poll_s=0.01)
        # 确认真的进了 copy（不靠固定 sleep——copy_count 涨了就说明在跑）
        deadline = time.monotonic() + 3
        while ops.copy_count == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert ops.copy_count >= 1, "没进到 copy，这个用例测不到在途取消"
        await eng.shutdown(timeout_s=2)
        assert eng.intent_status(key) is IntentStatus.FAILED
        # 区分「在途取消」与「从未启动」
        detail = eng.intent_detail(key)
        assert "cancelled (shutdown)" in detail and "never started" not in detail, (
            f"detail 说明它其实没跑起来: {detail}"
        )
        assert ops.abandoned, "在途取消没 abandon（账本会留 running）"

    run(scenario())


def test_shutdown_cancels_queued_intents() -> None:
    """队列里还没被取走的 intent：退出时标记失败，**原因文案必须是「从未
    启动」而不是「在途取消」**（后者是子串，用 in 会误判）。"""

    async def scenario():
        gate = threading.Event()
        eng = TaskEngine(FakeOps(query_gate=gate), max_concurrent=1, consumer_count=1)
        eng.start()
        keys = [
            eng.submit_intent(Prefix(model_id="q", token_ids=(i,)), _T, poll_s=0.01)
            for i in range(3)
        ]
        # 等第一个真的开始跑（占住唯一消费者），其余两个留在队列里
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rec = eng._records.get(keys[0])
            if rec is not None:
                break
            await asyncio.sleep(0.01)
        await eng.shutdown(timeout_s=2)

        for k in keys:
            assert eng.intent_status(k) is IntentStatus.FAILED
        details = {k: eng.intent_detail(k) for k in keys}
        # 至少有一个是「排队中就被停收」——文案精确匹配，不许用 in + 子串
        assert any(
            d == "cancelled (shutdown, never started)" for d in details.values()
        ), f"没有一条是「从未启动」: {details}"
        # 在途取消的文案由 test_shutdown_cancels_in_flight 锁（那里的断言
        # 排除 "never started"）；这里不再做字面量自比较（恒真）

    run(scenario())


def test_max_concurrent_must_be_positive() -> None:
    with pytest.raises(ValueError):
        TaskEngine(FakeOps(), max_concurrent=0)


# ---------------------------------------------------------------------------
# R1 复核修项的行为锁
# ---------------------------------------------------------------------------


def test_exists_not_blocked_by_full_gate() -> None:
    """传输闸满时，新 intent 的 exists 探测仍能进行（闸只管传输段）。

    构造：闸名额 1，第一个 intent 卡在 copy 之后的轮询里（占着闸）；
    第二个 intent 应当能完成 exists 探测——若闸包住了 exists，它会被卡。
    """
    probes: list[str] = []

    class ProbeOps(FakeOps):
        async def exists(self, prefix) -> bool:
            probes.append(prefix.model_id)
            return True

        async def copy(self, prefix, target):
            await asyncio.sleep(0.2)  # 长时间占着闸
            return await super().copy(prefix, target)

    async def scenario():
        ops = ProbeOps(seq=[])
        eng = TaskEngine(ops, max_concurrent=1)
        t1 = asyncio.create_task(
            eng.run_once(Prefix(model_id="first", token_ids=(1,)), _T, poll_s=0.01)
        )
        await asyncio.sleep(0.05)  # 让 first 拿到闸
        t2 = asyncio.create_task(
            eng.run_once(Prefix(model_id="second", token_ids=(2,)), _T, poll_s=0.01)
        )
        await asyncio.sleep(0.05)
        # second 的 exists 应该已经跑过（闸外），即使它在闸上等着
        assert "second" in probes, f"exists 被闸堵住了: {probes}"
        await asyncio.gather(t1, t2, return_exceptions=True)

    run(scenario())


def test_cancel_abandons_handle() -> None:
    """取消在途任务要 abandon 句柄（否则账本留 running，重启又被捞回）。"""

    async def scenario():
        gate = threading.Event()
        ops = FakeOps(query_gate=gate)
        eng = TaskEngine(ops, max_concurrent=1)
        eng.start()
        key = eng.submit_intent(_P, _T, poll_s=0.01)
        deadline = time.monotonic() + 3
        while not ops.abandoned and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            if eng.intent_status(key) is IntentStatus.PENDING and ops.copy_count:
                break
        await eng.shutdown(timeout_s=2)
        assert ops.abandoned, "取消后没有 abandon（账本会留 running）"

    run(scenario())


def test_consumer_survives_query_error() -> None:
    """exists 的 QueryError 不能杀掉消费者（几次抖动后全部消费者死光，
    新 intent 永远 PENDING）。"""

    async def scenario():
        ops = FakeOps(exists=False)

        class FlakyOps(FakeOps):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.n = 0

            async def exists(self, prefix) -> bool:
                self.n += 1
                if self.n <= 3:
                    raise QueryError("rpc boom")
                return True

        flaky = FlakyOps(seq=[])
        eng = TaskEngine(flaky, max_concurrent=1, consumer_count=1)
        eng.start()
        try:
            for i in range(4):
                eng.submit_intent(Prefix(model_id="q", token_ids=(i,)), _T, poll_s=0.01)
                await asyncio.sleep(0.05)
            # 消费者还活着：第 4 个（exists 不再报错）应当完成
            await asyncio.sleep(0.2)
            assert len(eng._consumers) == 1 and not eng._consumers[0].done(), (
                "消费者协程已被 QueryError 杀掉"
            )
        finally:
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_admit_after_shutdown_records_failed_not_dropped() -> None:
    """停收后到达的 intent 记 FAILED（不能丢弃——调用方已拿到 key，
    丢弃会让它的 GetTask 永远 UNKNOWN）。"""

    async def scenario():
        eng = TaskEngine(FakeOps())
        eng.start()
        await eng.shutdown(timeout_s=1)
        # 模拟「已经通过同步检查、但 _admit 才执行」的那条 intent
        eng._admit("k-late", _P, _T, 0.01, 1.0)
        assert eng.intent_status("k-late") is IntentStatus.FAILED
        assert "shutting down" in eng.intent_detail("k-late")

    run(scenario())


def test_consumer_count_defaults_above_gate() -> None:
    """消费者数默认远大于闸名额：排队 intent 出队做 exists 不被在途传输堵。"""
    eng = TaskEngine(FakeOps(), max_concurrent=2)
    assert eng._consumer_count >= 8, (
        f"消费者数 {eng._consumer_count} 不够——排队 intent 的 exists 会被堵"
    )
    eng2 = TaskEngine(FakeOps(), max_concurrent=4)
    assert eng2._consumer_count >= 16


def test_admit_shutdown_does_not_overwrite_existing() -> None:
    """停收分支不能覆盖已有记录（正在跑的那条还在改旧对象）。"""

    async def scenario():
        gate = threading.Event()
        eng = TaskEngine(FakeOps(query_gate=gate))
        eng.start()
        key = eng.submit_intent(_P, _T, poll_s=0.01)
        assert await _wait_registered(eng, key)
        original = eng._records[key]
        eng._accepting = False  # 模拟停收
        eng._admit(key, _P, _T, 0.01, 1.0)  # 迟到的 _admit
        assert eng._records[key] is original, "已有记录被覆盖了"
        assert eng.intent_status(key) is IntentStatus.PENDING  # 仍在跑
        gate.set()
        await eng.shutdown(timeout_s=2)

    run(scenario())


def test_queued_intent_exists_runs_while_gate_full() -> None:
    """闸满时，排队的 intent 仍能出队做 exists（消费者数 > 闸名额）。"""
    probes: list[str] = []

    class ProbeOps(FakeOps):
        async def exists(self, prefix) -> bool:
            probes.append(prefix.model_id)
            return True

        async def copy(self, prefix, target):
            await asyncio.sleep(0.3)  # 长时间占着闸
            return await super().copy(prefix, target)

    async def scenario():
        eng = TaskEngine(ProbeOps(seq=[]), max_concurrent=1)
        eng.start()
        try:
            eng.submit_intent(Prefix(model_id="first", token_ids=(1,)), _T, poll_s=0.01)
            await asyncio.sleep(0.05)  # first 拿到闸
            eng.submit_intent(Prefix(model_id="second", token_ids=(2,)), _T, poll_s=0.01)
            await asyncio.sleep(0.1)
            assert "second" in probes, f"排队 intent 的 exists 被堵: {probes}"
        finally:
            await eng.shutdown(timeout_s=2)

    run(scenario())
