"""MooncakeBackend 单元检查（fake store 绑定注入 sys.modules，离线可跑）。

关键锁定：任务标识（mooncake 整数对）在 copy → query_task 之间**原样
透传**——字符串化会让真实绑定的 query_task 直接 TypeError。
"""

from __future__ import annotations

import enum
import sys
import tempfile
import types

import pytest

from kvcm.backends.mooncake.backend import MooncakeBackend, encode_task_id
from kvcm.backends.mooncake.capability import mooncake_v0311, mooncake_main
from kvcm.backends.mooncake.client import StoreClient, entry_exists
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger, prefix_key
from kvcm.ops.base import (
    OpKind,
    SubmitError,
    TaskStatus,
    UnsupportedCapabilityError,
    VerifyError,
)
from kvcm.ops.types import InstanceId, PinKind, Prefix

# ---------------------------------------------------------------------------
# fake mooncake.store 绑定
# ---------------------------------------------------------------------------


class _FakeStoreTaskStatus(enum.Enum):
    PENDING = 0
    RUNNING = 1
    SUCCESS = 2
    FAILED = 3


class _FakeTaskResp:
    def __init__(self, status):
        self.status = status


@pytest.fixture()
def fake_store_module(monkeypatch):
    mod = types.ModuleType("mooncake.store")
    mod.TaskStatus = _FakeStoreTaskStatus
    pkg = types.ModuleType("mooncake")
    monkeypatch.setitem(sys.modules, "mooncake", pkg)
    monkeypatch.setitem(sys.modules, "mooncake.store", mod)
    return mod


class FakeClient(StoreClient):
    """脚本化 store：create_copy_task 返回整数对（与真实绑定同形态）。"""

    def __init__(self, *, task_status=_FakeStoreTaskStatus.SUCCESS, replicas_on: list[str] | None = None):
        super().__init__()
        self._status = task_status
        self._replicas_on = replicas_on
        self.created: list[tuple[str, list[str]]] = []
        self.queried: list[object] = []
        self._next = 0

    def create_copy_task(self, key, targets):
        self.created.append((key, list(targets)))
        self._next += 1
        return (self._next, 100 + self._next)  # 整数对，模仿绑定 UUID

    def query_task(self, task_id):
        self.queried.append(task_id)
        return _FakeTaskResp(self._status)

    def is_exist(self, keys):
        return [True] * len(keys)

    def batch_memory_replica_endpoints(self, keys):
        on = self._replicas_on if self._replicas_on is not None else [
            seg for _, segs in self.created for seg in segs
        ]
        return {k: list(on) for k in keys}


def _backend(client, kc: KeyComputer, monkeypatch, ledger=None, segs=("seg-A", "seg-B")) -> MooncakeBackend:
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    return MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=lambda _inst: list(segs),
        capability=mooncake_v0311(),
        ledger=ledger,
    )


# ---------------------------------------------------------------------------
# copy → query_task 往返
# ---------------------------------------------------------------------------


def test_task_id_roundtrip_is_original_pair(fake_store_module, monkeypatch) -> None:
    """阻塞回归：任务号禁止字符串化——query_task 必须收到原整数对。"""
    client = FakeClient()
    b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    handle = b.copy(Prefix(model_id="qwen", token_ids=(1, 2, 3)), InstanceId("inst-B"))
    assert handle.op is OpKind.COPY
    assert all(isinstance(t, tuple) for t in handle.task_ids)
    status = b.query_task(handle)
    assert status is TaskStatus.READY
    # 锁死：查询收到的标识与创建返回的完全一致（未被 str()）
    assert client.queried == list(handle.task_ids)
    # 终态即删核验登记（防长驻泄漏）
    assert handle not in b._plans


def test_plan_rank_mapped_via_backend(fake_store_module, monkeypatch) -> None:
    client = FakeClient()
    b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
    segs = [seg for _, segs in client.created for seg in segs]
    assert segs == ["seg-A", "seg-B"]  # rank0→seg-A, rank1→seg-B


def test_task_failed_is_terminal_and_purged(fake_store_module, monkeypatch) -> None:
    client = FakeClient(task_status=_FakeStoreTaskStatus.FAILED)
    b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    handle = b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
    assert b.query_task(handle) is TaskStatus.FAILED
    assert handle not in b._plans


def test_verify_failure_raises_with_detail(fake_store_module, monkeypatch) -> None:
    """任务全部成功但副本不到位：VerifyError 带明细（非静默 FAILED）。"""
    client = FakeClient(replicas_on=["seg-ELSEWHERE"])
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _backend(
            client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch, ledger=led
        )
        handle = b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
        with pytest.raises(VerifyError, match="missing on seg-A"):
            b.query_task(handle)
        assert handle not in b._plans
        # 核验失败是终态：Ledger 迁 failed
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "failed"  # type: ignore[union-attr]
    finally:
        led.close()
        import os

        os.unlink(path)


def test_empty_prefix_raises(fake_store_module, monkeypatch) -> None:
    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: [])
    b = MooncakeBackend(
        client=FakeClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
    )
    with pytest.raises(ValueError, match="no keys"):
        b.copy(Prefix(model_id="qwen", token_ids=()), InstanceId("inst-B"))


def test_submit_failure_carries_partial_ids(fake_store_module, monkeypatch) -> None:
    """生成器中途失败：已发起的任务号必须附在异常上（否则无人跟踪）。"""

    class BoomClient(FakeClient):
        def create_copy_task(self, key, targets):
            if len(self.created) >= 1:
                raise RuntimeError("submit boom")
            return super().create_copy_task(key, targets)

    client = BoomClient()
    b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    with pytest.raises(SubmitError, match=r"1/2 tasks.*\[\(1, 101\)\]"):
        b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))


# ---------------------------------------------------------------------------
# exists / entry_exists
# ---------------------------------------------------------------------------


def test_entry_exists_truth_table() -> None:
    assert entry_exists(1) and entry_exists(True) and entry_exists(2)
    assert not entry_exists(0) and not entry_exists(False) and not entry_exists(-1)


def test_exists_checks_length(fake_store_module, monkeypatch) -> None:
    """绑定短回（异常）不得误判为全部存在。"""

    class ShortClient(FakeClient):
        def is_exist(self, keys):
            return [True]  # 短于 keys

    kc = KeyComputer(model_name="qwen", tp_size=2)
    b = _backend(ShortClient(), kc, monkeypatch)
    assert b.exists(Prefix(model_id="qwen", token_ids=(1,))) is False


def test_exists_all_present(fake_store_module, monkeypatch) -> None:
    b = _backend(FakeClient(), KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    assert b.exists(Prefix(model_id="qwen", token_ids=(1,))) is True


# ---------------------------------------------------------------------------
# encode_task_id / Ledger 迁移
# ---------------------------------------------------------------------------


def test_encode_task_id_pair() -> None:
    assert encode_task_id((1, 101)) == "1:101"
    assert encode_task_id("plain") == "plain"


def test_ledger_transitions(fake_store_module, monkeypatch) -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        client = FakeClient()
        b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch, ledger=led)
        handle = b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "running"  # type: ignore[union-attr]
        b.query_task(handle)
        for tid in handle.task_ids:
            rec = led.get_task(encode_task_id(tid))
            assert rec and rec.status == "ready" and rec.op == "copy"
    finally:
        led.close()
        import os

        os.unlink(path)


# ---------------------------------------------------------------------------
# Ledger 持久性（不可丢：关闭重开数据仍在）
# ---------------------------------------------------------------------------


def test_ledger_survives_reopen() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    try:
        p = Prefix(model_id="qwen", token_ids=(1, 2, 3))
        led = Ledger(path)
        led.register_keepalive(p)
        led.close()
        led2 = Ledger(path)
        assert led2.list_keepalive() == [prefix_key(p)]
        led2.close()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# abandon（放弃跟踪）
# ---------------------------------------------------------------------------


def test_abandon_purges_and_marks(fake_store_module, monkeypatch) -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _backend(
            FakeClient(), KeyComputer(model_name="qwen", tp_size=2), monkeypatch, ledger=led
        )
        handle = b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
        b.abandon(handle)
        assert handle not in b._plans
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "abandoned"  # type: ignore[union-attr]
        # 放弃后句柄失效
        with pytest.raises(KeyError):
            b.query_task(handle)
        with pytest.raises(KeyError):
            b.abandon(handle)
    finally:
        led.close()
        import os

        os.unlink(path)


# ---------------------------------------------------------------------------
# pin 能力判定（SOFT 看 soft_pin_after，HARD 看 hard_pin_after）
# ---------------------------------------------------------------------------


def _pin_backend(monkeypatch, cap) -> MooncakeBackend:
    kc = KeyComputer(model_name="qwen", tp_size=1)
    return MooncakeBackend(
        client=FakeClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=cap,
    )


def test_pin_soft_unsupported_when_channel_no(fake_store_module, monkeypatch) -> None:
    b = _pin_backend(monkeypatch, mooncake_v0311())  # soft 通道 NO
    with pytest.raises(UnsupportedCapabilityError, match="soft pin"):
        b.pin(Prefix(model_id="qwen", token_ids=(1,)), PinKind.SOFT)


def test_pin_hard_always_unsupported(fake_store_module, monkeypatch) -> None:
    b = _pin_backend(monkeypatch, mooncake_main())  # hard 通道 NO（全版本）
    with pytest.raises(UnsupportedCapabilityError, match="hard pin"):
        b.pin(Prefix(model_id="qwen", token_ids=(1,)), PinKind.HARD)


def test_pin_soft_not_implemented_when_channel_yes(fake_store_module, monkeypatch) -> None:
    cap = mooncake_v0311()
    from dataclasses import replace

    from kvcm.ops.capability import Capability, Support

    cap_yes = replace(
        cap, soft_pin_after=Capability(Support.YES, Support.YES, "fake channel")
    )
    b = _pin_backend(monkeypatch, cap_yes)
    with pytest.raises(NotImplementedError):
        b.pin(Prefix(model_id="qwen", token_ids=(1,)), PinKind.SOFT)


# ---------------------------------------------------------------------------
# Ledger：批量事务 / status 校验 / 上下文管理器
# ---------------------------------------------------------------------------


def test_ledger_upsert_tasks_single_transaction() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    try:
        with Ledger(path) as led:
            p = Prefix(model_id="qwen", token_ids=(1,))
            led.upsert_tasks(["t1", "t2", "t3"], op="copy", prefix=p, target="i", status="running")
            for tid in ("t1", "t2", "t3"):
                assert led.get_task(tid).status == "running"  # type: ignore[union-attr]
    finally:
        os.unlink(path)


def test_ledger_status_validation() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    try:
        with Ledger(path) as led:
            with pytest.raises(ValueError, match="unknown task status"):
                led.upsert_task(
                    "t1", op="copy", prefix=Prefix(model_id="q", token_ids=(1,)),
                    target="i", status="reddy",  # 拼写错误必须在写入时失败
                )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Ledger 副本/会话表（增删）
# ---------------------------------------------------------------------------


def test_ledger_replicas_and_sessions_crud() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    try:
        led = Ledger(path)
        p = Prefix(model_id="qwen", token_ids=(1, 2, 3))
        led.put_replica("k1", "inst-B")
        led.put_replica("k1", "inst-C")
        assert sorted(led.list_replicas("k1")) == ["inst-B", "inst-C"]
        led.delete_replica("k1", "inst-B")
        assert led.list_replicas("k1") == ["inst-C"]
        led.bind_session("s1", p, "inst-B")
        led.bind_session("s1", Prefix(model_id="qwen", token_ids=(4,)), "inst-B")
        assert len(led.list_session("s1")) == 2
        led.unbind_session("s1")
        assert led.list_session("s1") == []
        led.close()
    finally:
        os.unlink(path)
