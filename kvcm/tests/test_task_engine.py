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
from kvcm.task_engine import IntentStatus, TaskEngine, _IntentRecord, intent_id_of

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


def test_intent_status_visible_before_admit_runs() -> None:
    """`submit_intent` 返回后同一个 tick 内查状态，必须能查到。

    原实现用 `call_soon_threadsafe` 把 `_admit` 排队，返回时循环还没跑到
    它，`_records` 里没记录；`grpc_server.py` 把「查不到」映射成
    STATUS_UNKNOWN，语义是「没有这个 id」——而调用方手里刚拿到这个 id。

    在引擎层测而不是走 gRPC：一次 RPC 往返足够让事件循环跑完 `_admit`，
    缝会被吃掉（实测改回旧实现走 gRPC 仍然全绿）。这里不 await。
    """

    async def scenario():
        eng = TaskEngine(FakeOps())
        eng.start()
        try:
            key = eng.submit_intent(_P, _T, poll_s=0.01)
            assert eng.intent_status(key) is IntentStatus.PENDING, (
                "submit_intent 已返回 id，但同 tick 内查不到记录"
            )
        finally:
            await eng.shutdown(timeout_s=2)

    run(scenario())


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


def test_shutdown_sweeps_placeholders_without_runtime_error() -> None:
    """shutdown 扫描未入队占位：不能边迭代边删（会抛 RuntimeError）。

    `_finish` 会从 `_records` 删条目；直接迭代 `values()` 是「边迭代边改
    大小」，CPython 抛 `RuntimeError: dictionary changed size during
    iteration`。一条占位就够触发。
    """

    async def scenario():
        eng = TaskEngine(FakeOps())
        eng.start()
        # 直接塞占位（模拟 submit_intent 已写、_admit 还没跑）
        for i in range(3):
            k = f"k{i}"
            eng._records[k] = _IntentRecord(
                intent_id=k, prefix=_P, target=_T, poll_s=0.01, timeout_s=1.0
            )
        await eng.shutdown(timeout_s=0.05)  # 不抛
        assert len(eng._records) == 0
        for i in range(3):
            assert eng.intent_status(f"k{i}") is IntentStatus.FAILED
            assert "never started" in eng.intent_detail(f"k{i}")

    run(scenario())


def test_shutdown_drain_pairs_task_done() -> None:
    """清队时 `get_nowait` 必须配 `task_done`。

    不配对会让 `_unfinished_tasks` 泄漏——将来有人 `await queue.join()`
    就永久挂住。
    """

    async def scenario():
        eng = TaskEngine(FakeOps())
        # 不起消费者，手工把记录塞进队列——确保 shutdown 时队列非空
        for i in range(3):
            k = f"q{i}"
            rec = _IntentRecord(
                intent_id=k, prefix=_P, target=_T, poll_s=0.01, timeout_s=1.0
            )
            rec.queued = True
            eng._records[k] = rec
            eng._queue.put_nowait(rec)
        assert eng._queue.qsize() == 3
        await eng.shutdown(timeout_s=0.05)
        assert eng._queue._unfinished_tasks == 0, (
            f"task_done 没配对，剩 {eng._queue._unfinished_tasks} 条未完成"
        )
        assert eng._queue.qsize() == 0
        for i in range(3):
            assert eng.intent_status(f"q{i}") is IntentStatus.FAILED

    run(scenario())


class _RacingRecords(dict):
    """逼两个线程都带着「空槽」这个观察结果去装槽。

    两个关键点（错一处就撞不出来）：
    - `get` 在屏障放开后**返回进屏障前的空槽结果**（`default`），不能再
      `super().get()` 重读——否则后一个线程会看到先装上的记录、走「已有
      PENDING」直接返回，根本不会覆盖
    - 第一次 `__setitem__` 立刻放行（A 装槽后马上去 `call_soon`）；第二次
      等 `allow_overwrite`，让 A 先入队再覆盖

    `setdefault` 不走 `get` / `__setitem__`，现实现不会进这些钩子。
    """

    def __init__(self) -> None:
        super().__init__()
        self.miss = threading.Barrier(2, timeout=2)
        self.writes = 0
        self._lock = threading.Lock()
        self.first_installed = threading.Event()
        self.allow_overwrite = threading.Event()

    def get(self, key, default=None):
        if key not in self:
            try:
                self.miss.wait()
            except threading.BrokenBarrierError:
                pass
            return default
        return super().get(key, default)

    def __setitem__(self, key, value):
        with self._lock:
            n = self.writes
            self.writes += 1
        if n == 0:
            super().__setitem__(key, value)
            self.first_installed.set()
            return
        self.allow_overwrite.wait(timeout=3)
        super().__setitem__(key, value)


def test_install_does_not_overwrite_queued_record() -> None:
    """装槽不能覆盖已入队的记录——同键双开的根源。

    旧实现 `self._records[key] = rec` 是覆盖。窗口：

    1. A、B 的 `get` 都看到空槽
    2. A 装槽，`_admit` 把它置 `queued=True` 并入队
    3. B 这时才赋值覆盖 → 新记录 `queued=False`
    4. B 的 `_admit` 再入队一次 → 同一前缀执行两遍（`copy_count == 2`）

    `setdefault` 让插入本身决定归属：槽非空时返回既有记录，自己的写不生效，
    也就不再投递 `_admit`。

    时序靠 `_RacingRecords` 固定，不靠线程调度。实测：旧实现 `writes=2,
    copy_count=2`；现实现 `writes=0, copy_count=1`。
    """

    async def scenario():
        gate = threading.Event()
        ops = FakeOps(query_gate=gate)
        eng = TaskEngine(ops, max_concurrent=2, consumer_count=2)
        eng.start()
        rd = _RacingRecords()
        eng._records = rd
        key = intent_id_of(_P, _T)
        try:
            threads = [
                threading.Thread(
                    target=lambda: eng.submit_intent(_P, _T, poll_s=0.01)
                )
                for _ in range(2)
            ]
            for t in threads:
                t.start()

            if not rd.first_installed.wait(timeout=1):
                # setdefault：没人走 __setitem__，不会覆盖
                for t in threads:
                    t.join(timeout=2)
                gate.set()
                deadline = time.monotonic() + 2
                while ops.copy_count < 1 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert rd.writes == 0, f"setdefault 不该走 __setitem__（{rd.writes} 次）"
                assert ops.copy_count == 1
                return

            # 旧实现：等 A 入队（只等 rec.queued，不等队列长度——消费者可能
            # 已经把 A 取走，队列为空但 A 已在跑），再放行 B 的覆盖
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                rec = dict.get(rd, key)  # 避开子类 get 上的屏障
                if rec is not None and rec.queued:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("A 没有入队，前提不成立")
            rd.allow_overwrite.set()
            for t in threads:
                t.join(timeout=2)
            gate.set()
            await asyncio.sleep(0.4)
            assert ops.copy_count == 1, (
                f"同键双开: copy 被调 {ops.copy_count} 次（覆盖了已入队的记录）"
            )
        finally:
            rd.allow_overwrite.set()
            gate.set()
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_stale_finish_does_not_evict_new_generation() -> None:
    """迟到的一代收口不能删掉后一代的 PENDING。

    `_finish` 若按 intent_id 无条件删，先结束的那代会把新一代的记录删掉，
    新一代还在跑却查不到，第三次提交还能再开一轮。
    """

    async def scenario():
        eng = TaskEngine(FakeOps())
        eng.start()
        try:
            key = "same-key"
            old = _IntentRecord(
                intent_id=key, prefix=_P, target=_T, poll_s=0.01, timeout_s=1.0
            )
            new = _IntentRecord(
                intent_id=key, prefix=_P, target=_T, poll_s=0.01, timeout_s=1.0
            )
            eng._records[key] = new  # 新一代占着槽
            # 老一代迟到的收口
            eng._finish(old, IntentStatus.FAILED, "late")
            assert eng._records.get(key) is new, "老一代把新一代删掉了"
            assert eng.intent_status(key) is IntentStatus.PENDING
        finally:
            await eng.shutdown(timeout_s=0.5)

    run(scenario())


def test_finish_does_not_overwrite_terminal_status() -> None:
    """已终态的记录再被收口：不把状态改写成后来的值。

    drain 与 sweep 可能同时收口同一条。没有状态判据时，第二次会把
    READY 改写成 FAILED。
    """

    async def scenario():
        eng = TaskEngine(FakeOps())
        eng.start()
        try:
            key = "k"
            rec = _IntentRecord(
                intent_id=key, prefix=_P, target=_T, poll_s=0.01, timeout_s=1.0
            )
            # 先手工标成 READY，但**仍留在槽里**（模拟消费者刚写完状态、
            # 还没走完 _finish 的中间态）
            eng._records[key] = rec
            rec.status = IntentStatus.READY
            # 此刻另一条收口路径进来
            eng._finish(rec, IntentStatus.FAILED, "late drain")
            assert eng.intent_status(key) is IntentStatus.READY, "READY 被改写成 FAILED"
        finally:
            await eng.shutdown(timeout_s=0.5)

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
        assert "shutdown" in eng.intent_detail("k-late")

    run(scenario())


def test_finished_record_survives_without_prefix() -> None:
    """终态后可查（READY / detail），但大字段已释放。

    `_records` 只存在途：终态即移走，换成瘦记录。否则每条记录都挂着
    `prefix.token_ids`（几千个 int，约 115 KB），进程长跑一直涨。
    """

    async def scenario():
        eng = TaskEngine(FakeOps(seq=[TaskStatus.READY]))
        eng.start()
        try:
            key = eng.submit_intent(_P, _T, poll_s=0.01)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if eng.intent_status(key) is IntentStatus.READY:
                    break
                await asyncio.sleep(0.01)
            assert eng.intent_status(key) is IntentStatus.READY
            # 关键：在途表里已经没有它了（大字段随之释放）
            assert key not in eng._records, "终态没从在途表移走，prefix 还挂着"
            # 但回执仍可查
            assert key in eng._finished
        finally:
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_failed_detail_survives_in_finished() -> None:
    """失败原因在终态之后仍可查（detail 是回执的全部价值）。"""

    async def scenario():
        eng = TaskEngine(FakeOps(exists=False))
        eng.start()
        try:
            key = eng.submit_intent(_P, _T, poll_s=0.01)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if eng.intent_status(key) is IntentStatus.FAILED:
                    break
                await asyncio.sleep(0.01)
            assert eng.intent_status(key) is IntentStatus.FAILED
            detail = eng.intent_detail(key)
            assert "PrefetchError" in detail, f"detail 丢了: {detail!r}"
            assert "not fully in pool" in detail
        finally:
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_pending_records_never_evicted() -> None:
    """在途记录不会被终态环的容量限制挤掉。

    `_records` 同时是去重表：PENDING 被淘汰，同键会再执行一次。终态环
    再多也不该影响在途表。
    """

    async def scenario():
        gate = threading.Event()
        ops = FakeOps(query_gate=gate)
        # 终态上限设成 1：跑满之后仍在途的那条必须还在
        eng = TaskEngine(ops, max_concurrent=8, consumer_count=8, finished_max=1)
        eng.start()
        try:
            stuck = eng.submit_intent(_P, _T, poll_s=0.01)
            # 等它真的开始执行（卡在 query 上）
            deadline = time.monotonic() + 3
            while ops.copy_count == 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert ops.copy_count >= 1
            # 再提交一批别的键，把终态环塞满
            for i in range(5):
                eng.submit_intent(
                    Prefix(model_id="other", token_ids=(i,)), _T, poll_s=0.01
                )
            await asyncio.sleep(0.2)
            # 关键：在途那条仍在，且仍是 PENDING
            assert stuck in eng._records, "在途记录被终态环挤掉了"
            assert eng.intent_status(stuck) is IntentStatus.PENDING
            assert len(eng._finished) <= 1
            gate.set()
        finally:
            gate.set()
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_finished_ring_evicts_oldest() -> None:
    """终态环满后淘汰最旧的，被淘汰的查不到（UNKNOWN）。"""

    async def scenario():
        eng = TaskEngine(FakeOps(seq=[TaskStatus.READY] * 10), finished_max=2)
        eng.start()
        try:
            keys = []
            for i in range(4):
                k = eng.submit_intent(
                    Prefix(model_id="m", token_ids=(i,)), _T, poll_s=0.01
                )
                keys.append(k)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if eng.intent_status(k) is IntentStatus.READY:
                        break
                    await asyncio.sleep(0.01)
            assert len(eng._finished) == 2, f"环没界: {len(eng._finished)}"
            # 最早两个已被淘汰
            assert eng.intent_status(keys[0]) is None
            assert eng.intent_status(keys[1]) is None
            # 最近两个还在
            assert eng.intent_status(keys[2]) is IntentStatus.READY
            assert eng.intent_status(keys[3]) is IntentStatus.READY
        finally:
            await eng.shutdown(timeout_s=2)

    run(scenario())


def test_resubmit_same_key_after_terminal_restarts() -> None:
    """终态后同键再提交 = 新意图（重新预热），旧回执被新一代覆盖。"""

    async def scenario():
        ops = FakeOps(seq=[TaskStatus.READY] * 2)
        eng = TaskEngine(ops)
        eng.start()
        try:
            k1 = eng.submit_intent(_P, _T, poll_s=0.01)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if eng.intent_status(k1) is IntentStatus.READY:
                    break
                await asyncio.sleep(0.01)
            assert ops.copy_count == 1
            k2 = eng.submit_intent(_P, _T, poll_s=0.01)
            assert k1 == k2  # 幂等键相同
            # 提交后立刻是 PENDING（占位）
            assert eng.intent_status(k2) is IntentStatus.PENDING
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if ops.copy_count == 2:
                    break
                await asyncio.sleep(0.01)
            assert ops.copy_count == 2, "终态后同键提交没有重新执行"
            # 环里只有一条（新代覆盖旧代）
            assert len(eng._finished) == 1
        finally:
            await eng.shutdown(timeout_s=2)

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
        # 等它真的入队（queued 由 _admit 置位）
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rec = eng._records.get(key)
            if rec is not None and rec.queued:
                break
            await asyncio.sleep(0.01)
        original = eng._records[key]
        assert original.queued, "前提不成立：intent 还没入队"
        eng._accepting = False  # 模拟停收
        eng._admit(key, _P, _T, 0.01, 1.0)  # 迟到的 _admit
        assert eng._records[key] is original, "已有记录被覆盖了"
        assert eng.intent_status(key) is IntentStatus.PENDING  # 仍在跑，没被标失败
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
