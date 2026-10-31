"""重启 e2e：进程死亡 → 新进程 rebuild → TaskEngine.track → 终态 + 保活。

「进程」语义用对象生命周期模拟：第一生命（b1）copy 提交后**不做任何
清理直接丢弃**（= 进程崩溃），第二生命（b2/engine）从同一 Ledger 文件
与同一 master（fake client 脚本推进）恢复。覆盖的正是重启后最容易丢
的一环：恢复任务 READY 后的保活注册（重启前来不及注册，副本不续租
就会被驱逐、预热白做）。

离线可跑：fake store 绑定注入 sys.modules。
"""

from __future__ import annotations

import enum
import os
import sys
import tempfile
import types

import pytest

from conftest import run

from kvcm.backends.mooncake.backend import MooncakeBackend, encode_task_id
from kvcm.backends.mooncake.capability import mooncake_v0311
from kvcm.backends.mooncake.client import StoreClient
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger
from kvcm.ops.types import InstanceId, Prefix
from kvcm.task_engine import TaskEngine

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")


class _FakeStoreTaskStatus(enum.Enum):
    PENDING = 0
    RUNNING = 1
    SUCCESS = 2
    FAILED = 3


class _FakeTaskResp:
    def __init__(self, status, message=""):
        self.status = status
        self.message = message


@pytest.fixture()
def fake_store_module(monkeypatch):
    mod = types.ModuleType("mooncake.store")
    mod.TaskStatus = _FakeStoreTaskStatus
    pkg = types.ModuleType("mooncake")
    monkeypatch.setitem(sys.modules, "mooncake", pkg)
    monkeypatch.setitem(sys.modules, "mooncake.store", mod)
    from test_backend import _install_verify_admin

    _install_verify_admin(monkeypatch)
    return mod


class FakeClient(StoreClient):
    def __init__(self, *, task_status, replicas_on):
        super().__init__()
        self._status = task_status
        self._replicas_on = replicas_on
        self._next = 0

    def create_copy_task(self, key, targets):
        self._next += 1
        return (self._next, 100 + self._next)

    def query_task(self, task_id):
        return _FakeTaskResp(self._status)

    def is_exist(self, keys):
        return [True] * len(keys)

    def batch_memory_replica_endpoints(self, keys):
        return {k: list(self._replicas_on) for k in keys}


class FakeKeepalive:
    def __init__(self) -> None:
        self.registered: list[Prefix] = []

    def register(self, prefix: Prefix) -> None:
        self.registered.append(prefix)


def _backend(client, kc: KeyComputer, ledger: Ledger) -> MooncakeBackend:
    return MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=lambda _inst: ["seg-A", "seg-B"],
        capability=mooncake_v0311(),
        ledger=ledger,
    )


@pytest.fixture()
def ledger_path():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    yield path
    os.unlink(path)


def _first_life(path: str, kc: KeyComputer) -> None:
    """进程第一生命：copy 提交成功、任务 RUNNING，随即「崩溃」（不清理）。"""
    led = Ledger(path)
    b1 = _backend(
        FakeClient(task_status=_FakeStoreTaskStatus.RUNNING, replicas_on=[]),
        kc,
        led,
    )
    run(b1.copy(_P, _T))
    assert len(led.list_running_tasks()) == 2  # 两 key 两任务
    led.close()  # 进程死亡：backend 对象随进程消失，无任何退出序列


def test_restart_recovers_to_ready_and_registers_keepalive(
    fake_store_module, monkeypatch, ledger_path
) -> None:
    kc = KeyComputer(model_name="qwen", tp_size=2)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    _first_life(ledger_path, kc)

    # 第二生命：master 侧任务已成功、副本到位（fake 脚本推进 = 时间流逝）
    led2 = Ledger(ledger_path)
    b2 = _backend(
        FakeClient(
            task_status=_FakeStoreTaskStatus.SUCCESS,
            replicas_on=["seg-A", "seg-B"],
        ),
        kc,
        led2,
    )
    ka = FakeKeepalive()
    eng = TaskEngine(b2, keepalive=ka)
    restored = b2.rebuild()
    assert len(restored) == 1
    for h in restored:
        run(eng.track(h, poll_s=0.01))
    run(eng.shutdown())

    assert ka.registered == [_P]  # 重启后来不及注册的保活在 track READY 补上
    assert led2.list_running_tasks() == []
    for tid in restored[0].task_ids:
        assert led2.get_task(encode_task_id(tid)).status == "ready"
    led2.close()


def test_restart_terminal_failure_does_not_register(
    fake_store_module, monkeypatch, ledger_path
) -> None:
    kc = KeyComputer(model_name="qwen", tp_size=2)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    _first_life(ledger_path, kc)

    led2 = Ledger(ledger_path)
    b2 = _backend(
        FakeClient(task_status=_FakeStoreTaskStatus.FAILED, replicas_on=[]),
        kc,
        led2,
    )
    ka = FakeKeepalive()
    eng = TaskEngine(b2, keepalive=ka)
    restored = b2.rebuild()
    for h in restored:
        run(eng.track(h, poll_s=0.01))
    run(eng.shutdown())

    assert ka.registered == []  # 失败终态不保活
    assert led2.list_running_tasks() == []
    led2.close()
