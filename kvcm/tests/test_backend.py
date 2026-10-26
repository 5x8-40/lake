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
from kvcm.backends.mooncake.client import StoreClient, StoreClientError, entry_exists
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger
from kvcm.ops.base import (
    OpKind,
    QueryError,
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
# 在途冲突观测态（OBJECT_HAS_REPLICATION_TASK → 不当失败，只核验副本）
# ---------------------------------------------------------------------------

_CONFLICT_MSG = "OBJECT_HAS_REPLICATION_TASK (max retries reached: 3)"


class ConflictClient(FakeClient):
    """全部任务报在途冲突；副本到位与否由 replicas_on 剧本控制。"""

    def query_task(self, task_id):
        self.queried.append(task_id)
        return _FakeTaskResp(_FakeStoreTaskStatus.FAILED, _CONFLICT_MSG)


def test_conflict_watch_running_then_ready(fake_store_module, monkeypatch) -> None:
    """在途冲突 → 转观测态：副本未到位 RUNNING（不丢句柄），到位 READY。"""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    try:
        client = ConflictClient(replicas_on=[])  # 副本还没到位
        b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch, ledger=led)
        handle = b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
        # 第一轮：全部失败且全是在途冲突 → 观测态 RUNNING，句柄保留
        assert b.query_task(handle) is TaskStatus.RUNNING
        assert handle in b._plans
        # 冲突任务如实记 failed（它们确实失败了，数据是别人搬的）
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "failed"  # type: ignore[union-attr]
        # 观测态后续调用不再查任务（任务已死），只核验副本
        n_queried = len(client.queried)
        assert b.query_task(handle) is TaskStatus.RUNNING
        assert len(client.queried) == n_queried
        # 副本到位（别人的任务传完）→ READY，终态删登记
        client._replicas_on = ["seg-A", "seg-B"]
        assert b.query_task(handle) is TaskStatus.READY
        assert handle not in b._plans
        assert handle not in b._watch
    finally:
        led.close()
        os.unlink(path)


def test_conflict_mixed_with_real_failure_is_terminal(fake_store_module, monkeypatch) -> None:
    """失败不全是冲突（混有真失败）→ 维持终态 FAILED，不进观测态。"""

    class MixedClient(FakeClient):
        def __init__(self):
            super().__init__()
            self._n = 0

        def query_task(self, task_id):
            self._n += 1
            if self._n == 1:
                return _FakeTaskResp(_FakeStoreTaskStatus.FAILED, _CONFLICT_MSG)
            return _FakeTaskResp(_FakeStoreTaskStatus.FAILED, "OBJECT_NOT_FOUND")

    b = _backend(MixedClient(), KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    handle = b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
    assert b.query_task(handle) is TaskStatus.FAILED
    assert handle not in b._plans
    assert handle not in b._watch


def test_abandon_during_watch_keeps_conflict_ledger(fake_store_module, monkeypatch) -> None:
    """观测态放弃：冲突任务保持 failed 不改写，其余记 abandoned。"""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    try:
        client = ConflictClient(replicas_on=[])
        b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch, ledger=led)
        handle = b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
        assert b.query_task(handle) is TaskStatus.RUNNING  # 进观测态
        b.abandon(handle)
        assert handle not in b._plans
        assert handle not in b._watch
        # 本例全部任务都冲突 → 全部保持 failed（无 abandoned 改写）
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "failed"  # type: ignore[union-attr]
    finally:
        led.close()
        os.unlink(path)


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


def test_is_exist_per_key_failure_raises() -> None:
    """batch_is_exist 单 key 返 -1（查询失败）→ StoreClientError，
    不得吞成「不存在」。"""

    class FakeBinding:
        def batch_is_exist(self, keys):
            return [1, -1]

    client = StoreClient()
    client._store = FakeBinding()
    with pytest.raises(StoreClientError, match=r"per-key query failed \(1/2\)"):
        client.is_exist(["k0", "k1"])


def test_exists_query_failure_is_retryable_query_error(fake_store_module, monkeypatch) -> None:
    """exists 的查询失败（RPC/单 key -1）→ QueryError（可重试），
    与「不存在」（False，触发重算/预热）严格区分。"""

    class BoomClient(FakeClient):
        def is_exist(self, keys):
            raise StoreClientError("batch_is_exist per-key query failed (1/2): ['k1']")

    b = _backend(BoomClient(), KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    with pytest.raises(QueryError):
        b.exists(Prefix(model_id="qwen", token_ids=(1,)))


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
        led.bind_session("s1", p, "inst-B")
        led.close()
        led2 = Ledger(path)
        # 重开必须还原 Prefix（token_ids 随表持久化——prefix_key 单向
        # digest 反推不出 token 序列，重启恢复全靠本列）
        assert led2.list_keepalive() == [p]
        assert led2.list_session("s1") == [p]
        led2.close()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# rebuild（重启恢复跟踪：tasks 表存可重算输入，plan 现场重算）
# ---------------------------------------------------------------------------


def test_rebuild_restores_running_handle(fake_store_module, monkeypatch) -> None:
    """重启恢复：新 backend 从同一 Ledger 重建 running 跟踪，继续到 READY。"""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    try:
        kc = KeyComputer(model_name="qwen", tp_size=2)
        monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
        # 第一次生命：copy 发起，Ledger 记 running
        b1 = MooncakeBackend(
            client=FakeClient(task_status=_FakeStoreTaskStatus.RUNNING),
            key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(),
            ledger=led,
        )
        b1.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
        assert len(led.list_running_tasks()) == 2  # 两 key 两任务

        # 模拟重启：全新 backend（空 _plans），同一 Ledger、同一 master
        client2 = FakeClient(
            task_status=_FakeStoreTaskStatus.SUCCESS,
            replicas_on=["seg-A", "seg-B"],
        )
        b2 = MooncakeBackend(
            client=client2,
            key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(),
            ledger=led,
        )
        assert b2.rebuild() == 1  # 一个 handle（两任务一组）
        handle = next(iter(b2._plans))
        # 任务号解码回整数对（与创建时同形态）
        assert all(isinstance(t, tuple) for t in handle.task_ids)
        # 继续 query_task → 核验通过 → READY；Ledger 迁移 ready
        assert b2.query_task(handle) is TaskStatus.READY
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "ready"  # type: ignore[union-attr]
    finally:
        led.close()
        os.unlink(path)


def test_rebuild_skips_empty_inputs(fake_store_module, monkeypatch) -> None:
    """空 handle_id / 空 token_ids（数据残缺行）不登记——防空 plan 假 READY。"""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    try:
        # 模拟数据残缺行：handle_id 空、token_ids 空（upsert 走正常通道后手工改空）
        p = Prefix(model_id="qwen", token_ids=(1,))
        led.upsert_task("t-old", handle_id="h-old", op="copy", prefix=p, target="i", status="running")
        led._conn.execute("UPDATE tasks SET handle_id = '', token_ids = ''")
        led._conn.commit()
        kc = KeyComputer(model_name="qwen", tp_size=2)
        monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
        b = MooncakeBackend(
            client=FakeClient(),
            key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(),
            ledger=led,
        )
        assert b.rebuild() == 0
        assert not b._plans
    finally:
        led.close()
        os.unlink(path)


def test_rebuild_restores_watch_state(fake_store_module, monkeypatch) -> None:
    """观测态恢复：同组冲突 failed 兄弟进 _watch——缺副本保持 RUNNING
    （不抛 VerifyError），副本到位后 READY 且冲突行保持 failed。"""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    try:
        kc = KeyComputer(model_name="qwen", tp_size=2)
        monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))

        class ConflictClient(FakeClient):
            def query_task(self, task_id):
                self.queried.append(task_id)
                if task_id == (1, 101):  # 第一个任务：冲突失败
                    return _FakeConflictResp()
                return _FakeTaskResp(self._status)

        class _FakeConflictResp:
            status = _FakeStoreTaskStatus.FAILED
            message = "OBJECT_HAS_REPLICATION_TASK (max retries reached: 3)"

        # 第一次生命：copy → 任务 1 冲突失败、任务 2 在途 → 进观测态
        # （副本只到 seg-A，观测态等 seg-B）
        client = ConflictClient(
            task_status=_FakeStoreTaskStatus.RUNNING, replicas_on=["seg-A"]
        )
        b1 = MooncakeBackend(
            client=client, key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(), ledger=led,
        )
        h1 = b1.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
        assert b1.query_task(h1) is TaskStatus.RUNNING  # 观测态等副本
        assert h1 in b1._watch

        # 重启：副本未到位 → RUNNING（不得 VerifyError）；冲突行保持 failed
        client2 = ConflictClient(
            task_status=_FakeStoreTaskStatus.SUCCESS, replicas_on=["seg-A"]  # 只到一个
        )
        b2 = MooncakeBackend(
            client=client2, key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(), ledger=led,
        )
        assert b2.rebuild() == 1
        h2 = next(iter(b2._plans))
        assert h2 in b2._watch  # 观测态恢复了
        assert b2.query_task(h2) is TaskStatus.RUNNING  # 缺 seg-B 副本：等，不是终态

        # 副本全部到位 → READY；冲突行保持 failed，其余 ready
        client2._replicas_on = ["seg-A", "seg-B"]
        assert b2.query_task(h2) is TaskStatus.READY
        assert led.get_task(encode_task_id((1, 101))).status == "failed"  # type: ignore[union-attr]
        assert led.get_task(encode_task_id((2, 102))).status == "ready"  # type: ignore[union-attr]
    finally:
        led.close()
        os.unlink(path)


def test_empty_handle_is_immediately_ready(fake_store_module) -> None:
    """空句柄（task_ids=()，retained 注册类无异步工作路径）立即 READY，
    不查登记、不核验。"""
    b = MooncakeBackend(
        client=FakeClient(),
        key_computer=KeyComputer(model_name="qwen", tp_size=1),
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
    )
    from kvcm.ops.base import TaskHandle

    empty = TaskHandle(op=OpKind.COPY, task_ids=())
    assert b.query_task(empty) is TaskStatus.READY


def test_rebuild_skips_unresolvable(fake_store_module, monkeypatch) -> None:
    """目标实例已不在（locate 失败）：跳过该组，不炸恢复流程。"""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    try:
        kc = KeyComputer(model_name="qwen", tp_size=2)
        monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
        b1 = MooncakeBackend(
            client=FakeClient(task_status=_FakeStoreTaskStatus.RUNNING),
            key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(),
            ledger=led,
        )
        b1.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))

        def dead_locate(_inst):
            raise RuntimeError("instance gone")

        b2 = MooncakeBackend(
            client=FakeClient(),
            key_computer=kc,
            locate=dead_locate,
            capability=mooncake_v0311(),
            ledger=led,
        )
        assert b2.rebuild() == 0
        assert not b2._plans
    finally:
        led.close()
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
            led.upsert_tasks(["t1", "t2", "t3"], handle_id="h1", op="copy", prefix=p, target="i", status="running")
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
                    "t1", handle_id="h1", op="copy", prefix=Prefix(model_id="q", token_ids=(1,)),
                    target="i", status="reddy",  # 拼写错误必须在写入时失败
                )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Ledger 副本/会话表（增删）
# ---------------------------------------------------------------------------


def test_sessions_model_id_with_colon_roundtrip() -> None:
    """model_id 含冒号：独立列还原，不被 prefix_key 首个冒号截断。"""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    try:
        p = Prefix(model_id="org:team:qwen3", token_ids=(1, 2, 3))
        with Ledger(path) as led:
            led.bind_session("s1", p, "inst-B")
        with Ledger(path) as led2:
            restored = led2.list_session("s1")
            assert restored == [p]
            assert restored[0].model_id == "org:team:qwen3"
    finally:
        os.unlink(path)


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


# ---------------------------------------------------------------------------
# Ledger tasks 表 GC（终态超龄删除；running 不动）
# ---------------------------------------------------------------------------


def test_ledger_gc_tasks() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os
    import time

    try:
        with Ledger(path) as led:
            p = Prefix(model_id="q", token_ids=(1,))
            led.upsert_task("old-ready", handle_id="h1", op="copy", prefix=p, target="i", status="ready")
            led.upsert_task("old-failed", handle_id="h2", op="copy", prefix=p, target="i", status="failed")
            led.upsert_task("old-abandoned", handle_id="h3", op="copy", prefix=p, target="i", status="abandoned")
            led.upsert_task("old-running", handle_id="h4", op="copy", prefix=p, target="i", status="running")
            led.upsert_task("new-ready", handle_id="h5", op="copy", prefix=p, target="i", status="ready")
            # 手工把四行的 updated_at 拨到 8 天前
            old = time.time() - 8 * 24 * 3600
            led._conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE task_id != 'new-ready'", (old,)
            )
            led._conn.commit()
            assert led.gc_tasks() == 3  # old-ready + old-failed + old-abandoned
            assert led.get_task("old-ready") is None
            assert led.get_task("old-failed") is None
            assert led.get_task("old-abandoned") is None  # abandoned 是终态，超龄同删
            # running 再老也不动（在途或孤儿，处置归恢复策略）
            assert led.get_task("old-running") is not None
            # 新终态不动
            assert led.get_task("new-ready") is not None
    finally:
        os.unlink(path)
