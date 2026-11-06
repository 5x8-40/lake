"""PromoteFlow：不传 target 等到任意 memory；传 target 则 copy / 先提回再 copy。"""

from __future__ import annotations

import logging

import pytest

from conftest import run
from kvcm.flows.promote import PromoteError, PromoteFlow, PromoteResult
from kvcm.ops.base import KvOps, OpKind, QueryError, TaskHandle, TaskStatus
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, Prefix

_P = Prefix(model_id="qwen", token_ids=(1,))
_I = InstanceId("inst")
_T = InstanceId("target")


class FakeOps(KvOps):
    def __init__(
        self,
        checks,
        *,
        holder: InstanceId | None | Exception = _I,
    ) -> None:
        self._checks = list(checks)
        self.promote_calls = 0
        self._holder = holder

    def capability(self) -> BackendCapability:
        raise NotImplementedError

    async def copy(self, prefix, target) -> TaskHandle:
        raise NotImplementedError

    async def query_task(self, handle):
        raise NotImplementedError

    async def abandon(self, handle) -> None:
        raise NotImplementedError

    async def exists(self, prefix) -> bool:
        return True

    async def promote(self, prefix) -> None:
        self.promote_calls += 1

    async def check_promote(self, prefix, target=None) -> bool:
        if target is not None:
            raise AssertionError("无 target 路径不应带 target 查")
        result = self._checks.pop(0) if self._checks else False
        if isinstance(result, Exception):
            raise result
        return result

    async def memory_instance(self, prefix) -> InstanceId | None:
        if isinstance(self._holder, Exception):
            raise self._holder
        return self._holder


def test_promote_flow_skips_trigger_when_already_in_memory() -> None:
    ops = FakeOps([True])
    result = run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert result == PromoteResult.schedulable(_I)
    assert result.ready is True
    assert ops.promote_calls == 0


def test_promote_flow_polls_until_ready() -> None:
    ops = FakeOps([False, False, True])
    result = run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert result == PromoteResult.schedulable(_I)
    assert ops.promote_calls == 1


def test_promote_flow_rejects_when_not_in_pool() -> None:
    class MissingOps(FakeOps):
        async def exists(self, prefix) -> bool:
            return False

    ops = MissingOps([])
    with pytest.raises(PromoteError, match="没有可提回"):
        run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert ops.promote_calls == 0


def test_promote_flow_retries_query_error() -> None:
    ops = FakeOps([QueryError("rpc"), True])
    run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert ops.promote_calls == 1


def test_promote_flow_logs_query_error(caplog) -> None:
    ops = FakeOps([QueryError("key-237 has no endpoint"), True])
    with caplog.at_level(logging.WARNING):
        run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert "key-237 has no endpoint" in caplog.text
    assert caplog.text.count("check_promote failed") == 1


def test_promote_flow_times_out() -> None:
    ops = FakeOps([False])
    with pytest.raises(PromoteError, match="timeout"):
        run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=0.0))


def test_promote_flow_allows_unknown_instance() -> None:
    """有 memory 但反查不到持有方：提回完成，不上报 READY。"""
    ops = FakeOps([True], holder=None)
    result = run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert result == PromoteResult.unschedulable()
    assert result.ready is False
    assert ops.promote_calls == 0


def test_promote_flow_memory_instance_query_error_still_succeeds(caplog) -> None:
    """check 已成功时，反查 QueryError 不得拖成 timeout；ready=False。"""
    ops = FakeOps([True], holder=QueryError("directory rpc down"))
    with caplog.at_level(logging.WARNING):
        result = run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert result == PromoteResult.unschedulable()
    assert ops.promote_calls == 0
    assert "memory_instance failed" in caplog.text


def test_promote_flow_memory_instance_error_after_trigger(caplog) -> None:
    ops = FakeOps([False, True], holder=QueryError("lookup failed"))
    with caplog.at_level(logging.WARNING):
        result = run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert result == PromoteResult.unschedulable()
    assert ops.promote_calls == 1
    assert "memory_instance failed" in caplog.text


class TargetFakeOps(KvOps):
    """带 target 路径：控制任意 memory / 是否已在 target / copy。"""

    def __init__(
        self,
        *,
        on_target: bool = False,
        any_memory: bool = True,
        any_after_promote: bool = True,
        copy_ok: bool = True,
        # copy 成功后若仍为 False，模拟 READY 与 check_promote(target) 不一致
        on_target_after_copy: bool | None = None,
    ) -> None:
        self.on_target = on_target
        self.any_memory = any_memory
        self.any_after_promote = any_after_promote
        self.copy_ok = copy_ok
        self.on_target_after_copy = (
            on_target if on_target_after_copy is None else on_target_after_copy
        )
        self.promote_calls = 0
        self.copy_calls = 0
        self._handle = TaskHandle(op=OpKind.COPY, task_ids=(("t", 1),))

    def capability(self) -> BackendCapability:
        raise NotImplementedError

    async def exists(self, prefix) -> bool:
        return True

    async def promote(self, prefix) -> None:
        self.promote_calls += 1
        if self.any_after_promote:
            self.any_memory = True

    async def check_promote(self, prefix, target=None) -> bool:
        if target is not None:
            return self.on_target
        return self.any_memory

    async def memory_instance(self, prefix) -> InstanceId | None:
        return None

    async def copy(self, prefix, target) -> TaskHandle:
        self.copy_calls += 1
        if not self.copy_ok:
            from kvcm.ops.base import SubmitError

            raise SubmitError("copy boom", handle=None)
        self.on_target = self.on_target_after_copy
        return self._handle

    async def query_task(self, handle):
        return TaskStatus.READY

    async def abandon(self, handle) -> None:
        return None


def test_promote_flow_target_already_there_skips_promote_and_copy() -> None:
    ops = TargetFakeOps(on_target=True, any_memory=True)
    result = run(
        PromoteFlow(ops).run(_P, target=_T, poll_s=0.001, timeout_s=1.0)
    )
    assert result == PromoteResult.schedulable(_T)
    assert result.ready is True
    assert ops.promote_calls == 0
    assert ops.copy_calls == 0


def test_promote_flow_target_copies_when_memory_elsewhere() -> None:
    ops = TargetFakeOps(
        on_target=False, any_memory=True, on_target_after_copy=True
    )
    result = run(
        PromoteFlow(ops).run(_P, target=_T, poll_s=0.001, timeout_s=1.0)
    )
    assert result == PromoteResult.schedulable(_T)
    assert ops.promote_calls == 0
    assert ops.copy_calls == 1


def test_promote_flow_target_promotes_then_copies_when_disk_only() -> None:
    ops = TargetFakeOps(
        on_target=False,
        any_memory=False,
        any_after_promote=True,
        on_target_after_copy=True,
    )
    result = run(
        PromoteFlow(ops).run(_P, target=_T, poll_s=0.001, timeout_s=1.0)
    )
    assert result == PromoteResult.schedulable(_T)
    assert ops.promote_calls == 1
    assert ops.copy_calls == 1


def test_promote_flow_target_rejects_when_ready_but_check_misses() -> None:
    ops = TargetFakeOps(
        on_target=False, any_memory=True, on_target_after_copy=False
    )
    with pytest.raises(PromoteError, match="check_promote"):
        run(
            PromoteFlow(ops).run(_P, target=_T, poll_s=0.001, timeout_s=1.0)
        )
    assert ops.copy_calls == 1


def test_promote_flow_target_verify_query_error_retries(caplog) -> None:
    """copy 后 check_promote QueryError 当未就绪，重试到成功。"""

    class FlakyVerify(TargetFakeOps):
        def __init__(self) -> None:
            super().__init__(
                on_target=False,
                any_memory=True,
                on_target_after_copy=True,
            )
            self._verify_n = 0

        async def check_promote(self, prefix, target=None) -> bool:
            if target is not None and self.copy_calls > 0:
                self._verify_n += 1
                if self._verify_n == 1:
                    raise QueryError("rpc after copy")
                return True
            return await super().check_promote(prefix, target)

    ops = FlakyVerify()
    with caplog.at_level(logging.WARNING):
        result = run(
            PromoteFlow(ops).run(_P, target=_T, poll_s=0.001, timeout_s=1.0)
        )
    assert result.ready is True
    assert "rpc after copy" in caplog.text


def test_promote_flow_target_admission_wait_bubbles() -> None:
    from kvcm.ops.base import AdmissionWait

    class FullSeg(TargetFakeOps):
        async def copy(self, prefix, target):
            self.copy_calls += 1
            raise AdmissionWait("segment full")

    ops = FullSeg(on_target=False, any_memory=True)
    with pytest.raises(AdmissionWait, match="segment full"):
        run(
            PromoteFlow(ops).run(_P, target=_T, poll_s=0.001, timeout_s=1.0)
        )


def test_promote_flow_target_copy_failure_is_promote_error() -> None:
    ops = TargetFakeOps(on_target=False, any_memory=True, copy_ok=False)
    with pytest.raises(PromoteError, match="copy failed"):
        run(
            PromoteFlow(ops).run(_P, target=_T, poll_s=0.001, timeout_s=1.0)
        )
    assert ops.promote_calls == 0
    assert ops.copy_calls == 1


def test_promote_flow_target_timeout_before_ensure_memory() -> None:
    ops = TargetFakeOps(on_target=False, any_memory=False)
    with pytest.raises(PromoteError, match="timeout"):
        run(
            PromoteFlow(ops).run(_P, target=_T, poll_s=0.001, timeout_s=0.0)
        )
    assert ops.promote_calls == 0
    assert ops.copy_calls == 0


def test_promote_flow_uses_injected_prefetch() -> None:
    calls: list[str] = []

    async def gated(prefix, target, *, poll_s, timeout_s):
        calls.append("gated")
        ops.on_target = True

    ops = TargetFakeOps(on_target=False, any_memory=True)
    result = run(
        PromoteFlow(ops, prefetch=gated).run(
            _P, target=_T, poll_s=0.001, timeout_s=1.0
        )
    )
    assert calls == ["gated"]
    assert ops.copy_calls == 0
    assert result.ready is True
