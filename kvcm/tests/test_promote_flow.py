"""PromoteFlow：触发后轮询 check_promote，看到目标 memory replica 即返回。"""

from __future__ import annotations

import logging

import pytest

from conftest import run
from kvcm.flows.promote import PromoteError, PromoteFlow
from kvcm.ops.base import KvOps, QueryError, TaskHandle
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, Prefix

_P = Prefix(model_id="qwen", token_ids=(1,))
_T = InstanceId("inst")


class FakeOps(KvOps):
    def __init__(self, checks) -> None:
        self._checks = list(checks)
        self.promote_calls = 0

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

    async def promote(self, prefix, target) -> None:
        self.promote_calls += 1

    async def check_promote(self, prefix, target) -> bool:
        result = self._checks.pop(0) if self._checks else False
        if isinstance(result, Exception):
            raise result
        return result


def test_promote_flow_polls_until_ready() -> None:
    ops = FakeOps([False, False, True])
    run(PromoteFlow(ops).run(_P, _T, poll_s=0.001, timeout_s=1.0))
    assert ops.promote_calls == 1


def test_promote_flow_retries_query_error() -> None:
    ops = FakeOps([QueryError("rpc"), True])
    run(PromoteFlow(ops).run(_P, _T, poll_s=0.001, timeout_s=1.0))
    assert ops.promote_calls == 1


def test_promote_flow_logs_query_error(caplog) -> None:
    ops = FakeOps([QueryError("key-237 has no endpoint"), True])
    with caplog.at_level(logging.WARNING):
        run(PromoteFlow(ops).run(_P, _T, poll_s=0.001, timeout_s=1.0))
    assert "key-237 has no endpoint" in caplog.text
    assert caplog.text.count("check_promote failed") == 1


def test_promote_flow_times_out() -> None:
    ops = FakeOps([False])
    with pytest.raises(PromoteError, match="timeout"):
        run(PromoteFlow(ops).run(_P, _T, poll_s=0.001, timeout_s=0.0))
