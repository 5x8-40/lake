"""PrefetchFlow 编排检查：fake KvOps 验证调用顺序与失败路径（离线）。"""

from __future__ import annotations

import pytest

from kvcm.flows import PrefetchError, PrefetchFlow
from kvcm.ops.base import KvOps, OpKind, TaskHandle, TaskStatus, VerifyError
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, PinKind, Prefix

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")


class FakeOps(KvOps):
    """脚本化的 KvOps：记录调用顺序，按构造参数演出 exists/状态序列。"""

    def __init__(self, *, exists: bool, seq: list[object]) -> None:
        self._exists = exists
        self._seq = list(seq)
        self.calls: list[str] = []

    def capability(self) -> BackendCapability:
        raise NotImplementedError

    def exists(self, prefix: Prefix) -> bool:
        self.calls.append("exists")
        return self._exists

    def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        self.calls.append("copy")
        return TaskHandle(op=OpKind.COPY, task_ids=((1, 2), (3, 4)))

    def query_task(self, handle: TaskHandle) -> TaskStatus:
        self.calls.append("query_task")
        nxt = self._seq.pop(0) if self._seq else TaskStatus.RUNNING
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    # 其余操作本流程不触及
    def move(self, prefix, target, source=None):
        raise NotImplementedError

    def keepalive(self, prefix):
        raise NotImplementedError

    def stop_keepalive(self, prefix):
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


def test_happy_path_order() -> None:
    ops = FakeOps(exists=True, seq=[TaskStatus.RUNNING, TaskStatus.READY])
    PrefetchFlow(ops).run(_P, _T, poll_s=0)
    assert ops.calls == ["exists", "copy", "query_task", "query_task"]


def test_exists_false_fails_before_copy() -> None:
    ops = FakeOps(exists=False, seq=[])
    with pytest.raises(PrefetchError, match="not fully in pool"):
        PrefetchFlow(ops).run(_P, _T, poll_s=0)
    assert ops.calls == ["exists"]  # copy 未发起


def test_task_failed_raises() -> None:
    ops = FakeOps(exists=True, seq=[TaskStatus.FAILED])
    with pytest.raises(PrefetchError, match="copy task failed"):
        PrefetchFlow(ops).run(_P, _T, poll_s=0)


def test_verify_error_is_terminal_with_detail() -> None:
    ops = FakeOps(exists=True, seq=[VerifyError("k1 missing on seg-A")])
    with pytest.raises(PrefetchError, match="k1 missing on seg-A"):
        PrefetchFlow(ops).run(_P, _T, poll_s=0)


def test_query_rpc_error_retries_then_ready() -> None:
    """查询 RPC 失败可重试：第一次抛错，第二次 READY，流程应成功。"""
    ops = FakeOps(exists=True, seq=[RuntimeError("rpc boom"), TaskStatus.READY])
    PrefetchFlow(ops).run(_P, _T, poll_s=0)
    assert ops.calls.count("query_task") == 2


def test_timeout_raises() -> None:
    ops = FakeOps(exists=True, seq=[])  # 永远 RUNNING
    with pytest.raises(PrefetchError, match="not READY"):
        PrefetchFlow(ops).run(_P, _T, poll_s=0, timeout_s=0.01)
