"""PrefetchFlow 编排检查：fake KvOps 验证调用顺序与失败路径（离线）。"""

from __future__ import annotations

import pytest

from kvcm.flows import PrefetchError, PrefetchFlow
from kvcm.ops.base import KvOps, TaskHandle, TaskStatus
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, PinKind, Prefix

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")


class FakeOps(KvOps):
    """脚本化的 KvOps：记录调用顺序，按构造参数演出 exists/状态序列。"""

    def __init__(self, *, exists: bool, status_seq: list[TaskStatus]) -> None:
        self._exists = exists
        self._seq = list(status_seq)
        self.calls: list[str] = []

    def capability(self) -> BackendCapability:
        raise NotImplementedError

    def exists(self, prefix: Prefix) -> bool:
        self.calls.append("exists")
        return self._exists

    def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        self.calls.append("copy")
        return TaskHandle(op="copy", task_ids=("t1", "t2"))

    def query_task(self, handle: TaskHandle) -> TaskStatus:
        self.calls.append("query_task")
        return self._seq.pop(0) if self._seq else TaskStatus.RUNNING

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
    ops = FakeOps(exists=True, status_seq=[TaskStatus.RUNNING, TaskStatus.READY])
    PrefetchFlow(ops).run(_P, _T, poll_s=0)
    assert ops.calls == ["exists", "copy", "query_task", "query_task"]


def test_exists_false_fails_before_copy() -> None:
    ops = FakeOps(exists=False, status_seq=[])
    with pytest.raises(PrefetchError, match="not fully in pool"):
        PrefetchFlow(ops).run(_P, _T, poll_s=0)
    assert ops.calls == ["exists"]  # copy 未发起


def test_task_failed_raises() -> None:
    ops = FakeOps(exists=True, status_seq=[TaskStatus.FAILED])
    with pytest.raises(PrefetchError, match="copy task failed"):
        PrefetchFlow(ops).run(_P, _T, poll_s=0)


def test_timeout_raises() -> None:
    ops = FakeOps(exists=True, status_seq=[])  # 永远 RUNNING
    with pytest.raises(PrefetchError, match="not READY"):
        PrefetchFlow(ops).run(_P, _T, poll_s=0, timeout_s=0.01)


def test_ledger_records_task_transitions(tmp_path) -> None:
    from kvcm.ledger import Ledger

    led = Ledger(str(tmp_path / "l.sqlite"))
    ops = FakeOps(exists=True, status_seq=[TaskStatus.READY])
    PrefetchFlow(ops, ledger=led).run(_P, _T, poll_s=0)
    for tid in ("t1", "t2"):
        rec = led.get_task(tid)
        assert rec is not None and rec.status == "ready" and rec.op == "copy"
    led.close()
