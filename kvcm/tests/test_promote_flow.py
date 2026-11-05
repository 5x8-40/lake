"""PromoteFlow：已有 memory 直接成功；否则触发后轮询 check_promote。"""

from __future__ import annotations

import logging

import pytest

from conftest import run
from kvcm.flows.promote import PromoteError, PromoteFlow, PromoteResult
from kvcm.ops.base import KvOps, QueryError, TaskHandle
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, Prefix

_P = Prefix(model_id="qwen", token_ids=(1,))
_I = InstanceId("inst")


class FakeOps(KvOps):
    def __init__(self, checks, *, holder: InstanceId | None = _I) -> None:
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

    async def check_promote(self, prefix) -> bool:
        result = self._checks.pop(0) if self._checks else False
        if isinstance(result, Exception):
            raise result
        return result

    async def memory_instance(self, prefix) -> InstanceId | None:
        return self._holder


def test_promote_flow_skips_trigger_when_already_in_memory() -> None:
    ops = FakeOps([True])
    result = run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert result == PromoteResult(instance=_I)
    assert ops.promote_calls == 0


def test_promote_flow_polls_until_ready() -> None:
    ops = FakeOps([False, False, True])
    result = run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert result.instance == _I
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
    ops = FakeOps([True], holder=None)
    result = run(PromoteFlow(ops).run(_P, poll_s=0.001, timeout_s=1.0))
    assert result.instance is None
    assert ops.promote_calls == 0
