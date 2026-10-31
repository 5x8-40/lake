"""PrefetchFlow 编排检查：fake KvOps 验证调用顺序与失败路径（离线）。"""

from __future__ import annotations

import pytest

from conftest import run

from kvcm.flows import PrefetchError, PrefetchFlow
from kvcm.ops.base import (
    KvOps,
    OpKind,
    QueryError,
    SubmitError,
    TaskHandle,
    TaskStatus,
    VerifyError,
)
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, PinKind, Prefix

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")


class FakeOps(KvOps):
    """脚本化的 KvOps：记录调用顺序，按构造参数演出 exists/状态序列。"""

    def __init__(
        self, *, exists: bool, seq: list[object], copy_error: Exception | None = None
    ) -> None:
        self._exists = exists
        self._seq = list(seq)
        self._copy_error = copy_error
        self.calls: list[str] = []

    def capability(self) -> BackendCapability:
        raise NotImplementedError

    async def exists(self, prefix: Prefix) -> bool:
        self.calls.append("exists")
        return self._exists

    async def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        self.calls.append("copy")
        if self._copy_error is not None:
            raise self._copy_error
        return TaskHandle(op=OpKind.COPY, task_ids=((1, 2), (3, 4)))

    async def query_task(self, handle: TaskHandle) -> TaskStatus:
        self.calls.append("query_task")
        nxt = self._seq.pop(0) if self._seq else TaskStatus.RUNNING
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def abandon(self, handle: TaskHandle) -> None:
        self.calls.append("abandon")

    # 其余操作本流程不触及
    async def move(self, prefix, target, source=None):
        raise NotImplementedError

    async def keepalive(self, prefix):
        raise NotImplementedError

    async def stop_keepalive(self, prefix):
        raise NotImplementedError

    async def pin(self, prefix, kind: PinKind = PinKind.SOFT):
        raise NotImplementedError

    async def demote(self, prefix):
        raise NotImplementedError

    async def promote(self, prefix, target):
        raise NotImplementedError

    async def retain(self, prefix):
        raise NotImplementedError

    async def remove(self, prefix):
        raise NotImplementedError


def test_happy_path_order() -> None:
    ops = FakeOps(exists=True, seq=[TaskStatus.RUNNING, TaskStatus.READY])
    run(PrefetchFlow(ops).run(_P, _T, poll_s=0))
    assert ops.calls == ["exists", "copy", "query_task", "query_task"]


def test_exists_false_fails_before_copy() -> None:
    ops = FakeOps(exists=False, seq=[])
    with pytest.raises(PrefetchError, match="not fully in pool"):
        run(PrefetchFlow(ops).run(_P, _T, poll_s=0))
    assert ops.calls == ["exists"]  # copy 未发起


def test_submit_error_wrapped_as_prefetch_error() -> None:
    """copy 提交失败（SubmitError 带明细）统一出口为 PrefetchError。"""
    ops = FakeOps(
        exists=True, seq=[], copy_error=SubmitError("1/2 tasks submitted: [(1, 101)]")
    )
    with pytest.raises(PrefetchError, match=r"copy submit failed.*\(1, 101\)"):
        run(PrefetchFlow(ops).run(_P, _T, poll_s=0))


_PARTIAL = TaskHandle(op=OpKind.COPY, task_ids=((1, 101),))


def test_submit_abandon_attribute_error_propagates() -> None:
    """abandon 的编程错误要冒出去，不要变成 PrefetchError。"""

    class _Ops(FakeOps):
        async def abandon(self, handle: TaskHandle) -> None:
            raise AttributeError("bug")

    ops = _Ops(
        exists=True,
        seq=[],
        copy_error=SubmitError("submit boom", handle=_PARTIAL),
    )
    with pytest.raises(AttributeError, match="bug"):
        run(PrefetchFlow(ops).run_after_exists(_P, _T, poll_s=0, timeout_s=5))


def test_submit_abandon_sqlite_error_stays_prefetch_error() -> None:
    """账本写失败只记日志。提交失败仍是 PrefetchError，原因是原来的 SubmitError。"""
    import sqlite3

    cause = SubmitError("submit boom", handle=_PARTIAL)

    class _Ops(FakeOps):
        async def abandon(self, handle: TaskHandle) -> None:
            raise sqlite3.OperationalError("disk I/O error")

    ops = _Ops(exists=True, seq=[], copy_error=cause)
    with pytest.raises(PrefetchError, match="copy submit failed") as ei:
        run(PrefetchFlow(ops).run_after_exists(_P, _T, poll_s=0, timeout_s=5))
    assert ei.value.__cause__ is cause


def test_task_failed_raises() -> None:
    ops = FakeOps(exists=True, seq=[TaskStatus.FAILED])
    with pytest.raises(PrefetchError, match="copy task failed"):
        run(PrefetchFlow(ops).run(_P, _T, poll_s=0))


def test_verify_error_is_terminal_with_detail() -> None:
    ops = FakeOps(exists=True, seq=[VerifyError("k1 missing on seg-A")])
    with pytest.raises(PrefetchError, match="k1 missing on seg-A"):
        run(PrefetchFlow(ops).run(_P, _T, poll_s=0))


def test_query_error_retries_then_ready() -> None:
    """查询失败（QueryError）可重试：第一次抛错，第二次 READY，流程成功。"""
    ops = FakeOps(exists=True, seq=[QueryError("rpc boom"), TaskStatus.READY])
    run(PrefetchFlow(ops).run(_P, _T, poll_s=0))
    assert ops.calls.count("query_task") == 2


def test_programming_error_propagates_not_swallowed() -> None:
    """编程错误（非 QueryError）直接传播——不得被吞成超时重试。"""
    ops = FakeOps(exists=True, seq=[TypeError("boom: wrong type")])
    with pytest.raises(TypeError, match="wrong type"):
        run(PrefetchFlow(ops).run(_P, _T, poll_s=0, timeout_s=60))


def test_cancel_during_poll_abandons_handle() -> None:
    """轮询中被取消：先 `abandon` 收口，再把 `CancelledError` 抛出去。

    退出时取消协程，若不断开跟踪，Ledger 里那行会留 `running`，重启又被
    `rebuild` 捞回来跟——而进程已经在退出，没人会再轮询它。
    """

    async def scenario():
        import asyncio as _aio

        ops = FakeOps(exists=True, seq=[TaskStatus.RUNNING] * 50)
        flow = PrefetchFlow(ops)
        task = _aio.create_task(
            flow.run(_P, _T, poll_s=0.05, timeout_s=10.0)
        )
        # 等它进入轮询（query_task 被调过至少一次）
        deadline = _aio.get_running_loop().time() + 2
        while ops.calls.count("query_task") == 0:
            if _aio.get_running_loop().time() > deadline:
                raise AssertionError("没进入轮询，前提不成立")
            await _aio.sleep(0.01)
        task.cancel()
        with pytest.raises(_aio.CancelledError):
            await task
        assert "abandon" in ops.calls, f"取消时没 abandon: {ops.calls}"

    run(scenario())


def test_timeout_abandons_handle() -> None:
    """超时收口：放弃跟踪（abandon），再抛 PrefetchError。"""
    ops = FakeOps(exists=True, seq=[])  # 永远 RUNNING
    with pytest.raises(PrefetchError, match="not READY"):
        run(PrefetchFlow(ops).run(_P, _T, poll_s=0, timeout_s=0.01))
    assert "abandon" in ops.calls


def test_timeout_cancels_copy_before_handle() -> None:
    """copy 还没返回时超时：取消等待。没有句柄，不 abandon，也不另记一笔。"""
    import asyncio

    class _Hang(FakeOps):
        async def copy(self, prefix, target):
            self.calls.append("copy")
            await asyncio.sleep(30)
            self.calls.append("copy_returned")
            return TaskHandle(op=OpKind.COPY, task_ids=((1, 2),))

    async def scenario():
        ops = _Hang(exists=True, seq=[])
        with pytest.raises(PrefetchError, match="cancelled before handle"):
            await PrefetchFlow(ops).run_after_exists(
                _P, _T, poll_s=0.01, timeout_s=0.05
            )
        assert "abandon" not in ops.calls
        assert "copy_returned" not in ops.calls

    run(scenario())


def test_shutdown_during_drain_is_cancelled_error() -> None:
    """排空进行中当前任务被取消：CancelledError 冒出去，不变成 PrefetchError。"""
    import asyncio

    entered_drain = asyncio.Event()

    class _SlowCancel(FakeOps):
        async def copy(self, prefix, target):
            self.calls.append("copy")
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                entered_drain.set()
                await asyncio.sleep(30)
                raise

    async def scenario():
        ops = _SlowCancel(exists=True, seq=[])
        task = asyncio.create_task(
            PrefetchFlow(ops).run_after_exists(
                _P, _T, poll_s=0.01, timeout_s=0.05
            )
        )
        await asyncio.wait_for(entered_drain.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())


def test_timeout_budget_covers_copy_and_poll() -> None:
    """同一段 timeout_s 覆盖提交和轮询，不是两段相加。"""
    import asyncio
    import time

    class _SlowCopy(FakeOps):
        async def copy(self, prefix, target):
            self.calls.append("copy")
            await asyncio.sleep(0.3)
            return TaskHandle(op=OpKind.COPY, task_ids=((1, 2),))

    async def scenario():
        ops = _SlowCopy(exists=True, seq=[])  # 轮询永远 RUNNING
        start = time.monotonic()
        with pytest.raises(PrefetchError, match="not READY"):
            await PrefetchFlow(ops).run_after_exists(
                _P, _T, poll_s=0.05, timeout_s=0.45
            )
        elapsed = time.monotonic() - start
        # 分段相加至少是 0.3+0.45。同一段预算应靠近 0.45。
        assert elapsed < 0.7, f"超时被加成两段: {elapsed:.2f}s"
        assert "abandon" in ops.calls

    run(scenario())
