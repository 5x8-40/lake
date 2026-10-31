"""MooncakeBackend 单元检查（fake store 绑定注入 sys.modules，离线可跑）。

关键锁定：任务标识（mooncake 整数对）在 copy → query_task 之间**原样
透传**——字符串化会让真实绑定的 query_task 直接 TypeError。
"""

from __future__ import annotations

import asyncio

import enum
import sys
import tempfile
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import run

from conftest import run

from kvcm.backends.mooncake.backend import MooncakeBackend, encode_task_id
from kvcm.backends.mooncake.capability import mooncake_v0311, mooncake_main
from kvcm.backends.mooncake.client import StoreClient, StoreClientError, entry_exists
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger
from kvcm.ops.capability import Support
from kvcm.ops.base import (
    IntentInputError,
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
    _install_verify_admin(monkeypatch)
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
        self._id_lock = threading.Lock()

    def create_copy_task(self, key, targets):
        # 并发提交时 `_next += 1` 是读改写，需保护（真实绑定在 C++ 侧发号）
        with self._id_lock:
            self.created.append((key, list(targets)))
            self._next += 1
            tid = (self._next, 100 + self._next)  # 整数对，模仿绑定 UUID
        return tid

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


def _install_verify_admin(monkeypatch) -> None:
    """核验走 admin.batch_query_keys。测试里用 client 上的端点填这份假响应。"""
    from kvcm.backends.mooncake.admin_main import KeyReplicaQuery, MemoryReplicaView

    class _Admin:
        def __init__(self, client):
            self.client = client
            self.calls = 0

        def batch_query_keys(self, keys):
            self.calls += 1
            eps = self.client.batch_memory_replica_endpoints(keys)
            disk = set(getattr(self.client, "disk_keys", ()) or ())
            out = {}
            for key in keys:
                out[key] = KeyReplicaQuery(
                    key=key,
                    ok=True,
                    memory=tuple(MemoryReplicaView(e) for e in eps.get(key, [])),
                    disk_replicas=1 if key in disk or getattr(self.client, "disk_all", False) else 0,
                )
            return out

    orig = MooncakeBackend.__init__

    def wrapped(self, *args, **kwargs):
        if kwargs.get("admin") is None:
            kwargs["admin"] = _Admin(kwargs["client"])
        orig(self, *args, **kwargs)

    monkeypatch.setattr(MooncakeBackend, "__init__", wrapped)


def _backend(
    client,
    kc: KeyComputer,
    monkeypatch,
    ledger=None,
    segs=("seg-A", "seg-B"),
    submit_concurrency=4,
) -> MooncakeBackend:
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    return MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=lambda _inst: list(segs),
        capability=mooncake_v0311(),
        ledger=ledger,
        submit_concurrency=submit_concurrency,
    )


# ---------------------------------------------------------------------------
# copy → query_task 往返
# ---------------------------------------------------------------------------


def test_task_id_roundtrip_is_original_pair(fake_store_module, monkeypatch) -> None:
    """阻塞回归：任务号禁止字符串化——query_task 必须收到原整数对。"""
    client = FakeClient()
    b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1, 2, 3)), InstanceId("inst-B")))
    assert handle.op is OpKind.COPY
    assert all(isinstance(t, tuple) for t in handle.task_ids)
    status = run(b.query_task(handle))
    assert status is TaskStatus.READY
    # 锁死：查询收到的标识与创建返回的完全一致（未被 str()）
    assert sorted(client.queried) == sorted(handle.task_ids)
    # 终态即删核验登记（防长驻泄漏）
    assert handle not in b._plans


def test_plan_rank_mapped_via_backend(fake_store_module, monkeypatch) -> None:
    client = FakeClient()
    b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
    segs = [seg for _, segs in client.created for seg in segs]
    assert segs == ["seg-A", "seg-B"]  # rank0→seg-A, rank1→seg-B


def test_task_failed_is_terminal_and_purged(fake_store_module, monkeypatch) -> None:
    client = FakeClient(task_status=_FakeStoreTaskStatus.FAILED)
    b = _backend(client, KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
    assert run(b.query_task(handle)) is TaskStatus.FAILED
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
        handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
        with pytest.raises(VerifyError, match="missing on seg-A"):
            run(b.query_task(handle))
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
    with pytest.raises(IntentInputError, match="no keys"):
        run(b.copy(Prefix(model_id="qwen", token_ids=()), InstanceId("inst-B")))


def test_cancelled_submit_records_partial_ids(fake_store_module, monkeypatch) -> None:
    """提交中途被取消：已发出的任务号必须落账成 running。

    `CancelledError` 不是 `Exception` 的子类。只捕 `Exception` 的话，退出
    时取消落在提交循环里，已发出的号就丢了——`rebuild` 只认
    `status='running'` 的行，这些在途传输重启后再也没人跟踪。

    必须是多 key 的 plan，且第 2 个提交卡住：取消点才落得到「第 1 个已
    发出、第 2 个还没回」的窗口上，否则测试会假通过。
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    gate = threading.Event()
    try:
        class SlowSecondClient(FakeClient):
            def create_copy_task(self, key, targets):
                if self.created:  # 第 2 个起：卡住，制造在飞窗口
                    gate.wait(5.0)
                return super().create_copy_task(key, targets)

        client = SlowSecondClient()
        kc = KeyComputer(model_name="qwen", tp_size=2)
        monkeypatch.setattr(
            kc, "keys_for_tokens", lambda ids: kc.expand(["h0", "h1"])
        )
        b = MooncakeBackend(
            client=client,
            key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(),
            ledger=led,
            submit_concurrency=1,  # 串行：第 2 个必然在取消点之后才发出
        )

        async def scenario():
            task = asyncio.create_task(
                b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
            )
            deadline = time.monotonic() + 5
            while not client.created and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert client.created, "第 1 个任务都没发出，前提不成立"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task  # 取消必须照常传播，不能被包成 SubmitError

        run(scenario())
        # 前提校验：取消时确实只发出了 1 个，测的才是「提交中途」
        assert len(client.created) == 1, f"取消点不在提交循环内: {client.created}"
        running = led.list_running_tasks()
        assert len(running) == 1, f"取消时已发出的任务号没入账: {running}"
        from kvcm.backends.mooncake.backend import decode_task_id

        assert decode_task_id(running[0].task_id) == (1, 101)
    finally:
        gate.set()
        led.close()
        os.unlink(path)


def test_cancel_during_drain_after_submit_failure_still_records(
    fake_store_module, monkeypatch
) -> None:
    """某 key 提交失败后，等其余在飞提交时被取消：已拿到的号仍要落账。

    退出路径的最坏形状：先有一个提交抛普通异常 → 进 `except` 排空其余在飞
    调用 → 这时退出信号到达。若让 `CancelledError` 直接从排空那一步冒出，
    后面读槽和记账都不跑，已经拿到的号全丢（重启的 `rebuild` 捞不到）。
    排空可以放弃等待，但**记账不能跳过**。
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    gate = threading.Event()
    calls = 0
    lock = threading.Lock()
    try:
        class MixedClient(FakeClient):
            def create_copy_task(self, key, targets):
                nonlocal calls
                with lock:
                    calls += 1
                    nth = calls
                if nth == 2:
                    time.sleep(0.05)  # 让第 1 个先拿到号并写槽
                    raise RuntimeError("submit boom")
                if nth == 3:
                    gate.wait(5.0)  # 卡住：排空阶段会停在这里
                return super().create_copy_task(key, targets)

        client = MixedClient()
        kc = KeyComputer(model_name="qwen", tp_size=1)
        monkeypatch.setattr(
            kc, "keys_for_tokens", lambda ids: kc.expand(["h0", "h1", "h2"])
        )
        b = MooncakeBackend(
            client=client,
            key_computer=kc,
            locate=lambda _inst: ["seg-A"],
            capability=mooncake_v0311(),
            ledger=led,
            submit_concurrency=3,  # 三个一起在飞：失败与在飞并存
        )

        async def scenario():
            task = asyncio.create_task(
                b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
            )
            deadline = time.monotonic() + 5
            while calls < 3 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert calls == 3, f"三个提交没都在飞: {calls}"
            await asyncio.sleep(0.15)  # 让它进到排空（卡在第 3 个上）
            assert not task.done(), "复制已经结束了，取消点不在排空阶段"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # 取消可以不等在飞的提交，但已拿到的号必须落账。
            assert [r.task_id for r in led.list_running_tasks()] == ["1:101"], (
                "排空阶段被取消，已拿到的号没落账"
            )

        run(scenario())
    finally:
        gate.set()
        led.close()
        os.unlink(path)


def test_submit_cap_is_process_wide_across_concurrent_copies(
    fake_store_module, monkeypatch
) -> None:
    """上限是**进程级**的：两个 copy 同时跑，在飞总数仍不超过上限。

    为什么必须锁这条：传输闸（TaskEngine `max_concurrent`，默认 4）允许
    4 个 copy 同时提交。若每个 copy 各持一把默认 4 的窗口，在飞建链就是
    4×4=16——正好踩过 0.3.11 实测打爆建链的点（14 连发约半数
    TRANSFER_FAIL）。信号量必须是所有 copy 共用的一把。

    用 8 线程的池：默认池在小机器上只有 4 个 worker，会把峰值挡在 4，
    实现改回「每 copy 一把」也照样绿（假通过）。
    """
    inflight = 0
    peak = 0
    lock = threading.Lock()

    class SlowClient(FakeClient):
        def create_copy_task(self, key, targets):
            nonlocal inflight, peak
            with lock:
                inflight += 1
                peak = max(peak, inflight)
            try:
                time.sleep(0.1)  # 占住，制造重叠
                return super().create_copy_task(key, targets)
            finally:
                with lock:
                    inflight -= 1

    client = SlowClient()
    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(
        kc,
        "keys_for_tokens",
        lambda ids: kc.expand(["h0", "h1", "h2", "h3"]),
    )
    pool = ThreadPoolExecutor(max_workers=8)  # 默认池只有 4 线程，会挡住峰值
    try:
        b = MooncakeBackend(
            client=client,
            key_computer=kc,
            locate=lambda _inst: ["seg-A"],
            capability=mooncake_v0311(),
            submit_concurrency=4,
            executor=pool,
        )

        async def scenario():
            # 两个 copy 同时跑：每个 4 key，合计 8 个提交
            await asyncio.gather(
                b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")),
                b.copy(Prefix(model_id="qwen", token_ids=(2,)), InstanceId("inst-B")),
            )

        run(scenario())
        assert peak == 4, (
            f"跨 copy 的在飞数没被进程级信号量压住: peak={peak}（应为 4）"
        )
    finally:
        pool.shutdown(wait=True)


def test_daemon_passes_copy_submit_concurrency(monkeypatch, tmp_path) -> None:
    """`--copy-submit-concurrency` 真的传到 backend。

    不锁的话，`build_backend` 里少写一行参数，单测全绿（默认值恰好是 4），
    生产里调参就是空转。
    """
    from kvcm import daemon
    import argparse

    seen: dict = {}

    def fake_backend(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(daemon, "MooncakeBackend", fake_backend)
    monkeypatch.setattr(
        daemon,
        "KeyComputer",
        lambda **kw: object(),
    )
    args = argparse.Namespace(
        model_name="qwen",
        block_size=128,
        tp_size=1,
        peer_tp_size=None,
        put_step=1,
        hash_algo="sha256",
        instance_segs=["inst-B=seg-A"],
        capability="v0311",
        rpc_timeout_s=30.0,
        copy_submit_concurrency=7,  # 非默认值，才能证明真的传了
        lease_ttl_s=11.0,
        promote_hits=3,
    )
    daemon.build_backend(args, client=object(), ledger=object())
    assert seen["submit_concurrency"] == 7, (
        f"--copy-submit-concurrency 没传下去: {seen.get('submit_concurrency')}"
    )
    assert seen["lease_ttl_s"] == 11.0
    assert seen["promote_hits"] == 3
    assert list(seen["locate"]("inst-B")) == ["seg-A"]
    with pytest.raises(IntentInputError, match="not registered"):
        seen["locate"]("other")


def test_copy_submits_concurrently_up_to_cap(fake_store_module, monkeypatch) -> None:
    """一次 copy 内多 key 并发提交，但在飞数不超过 `submit_concurrency`。

    串行提交时提交阶段是 N 倍往返（大批量预热慢得不可用）；不限并发则
    在目标侧形成建链风暴（0.3.11 实测 14 连发约半数 TRANSFER_FAIL）。
    两个方向都要锁：**并发度 > 1** 证明并行，**不超上限** 证明没失控。
    """
    inflight = 0
    peak = 0
    lock = threading.Lock()

    class SlowClient(FakeClient):
        def create_copy_task(self, key, targets):
            nonlocal inflight, peak
            with lock:
                inflight += 1
                peak = max(peak, inflight)
            try:
                # 短暂占用：并发的提交必然重叠（peak 到上限），串行的
                # 不会。0.1 秒远大于调度抖动，不需要任何事件/门槛。
                time.sleep(0.1)
                return super().create_copy_task(key, targets)
            finally:
                with lock:
                    inflight -= 1

    client = SlowClient()
    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(
        kc,
        "keys_for_tokens",
        lambda ids: kc.expand(["h0", "h1", "h2", "h3", "h4", "h5"]),
    )
    b = MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
        submit_concurrency=3,
    )
    handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
    assert len(handle.task_ids) == 6
    assert peak == 3, f"并发度不对: peak={peak}"
    # 并发完成顺序不定，号本身是乱序发放的；但六个数一个不能少。
    assert sorted(handle.task_ids) == sorted((i, 100 + i) for i in range(1, 7))


def test_ledger_write_uses_ledger_pool_not_rpc_pool(
    fake_store_module, monkeypatch
) -> None:
    """Ledger 写入走 Ledger 自己的池，不占 RPC 池。

    用 RPC 池有两个害处：占掉传输名额；退出时池已关，`abandon` 若恰好落在
    那个窗口，会抛 `cannot schedule new futures after shutdown`，账本留
    `running`。

    测法：copy 正常跑完（RPC 池还活着），然后关掉 RPC 池，再调 `abandon`
    —— 它只写 ledger，不该需要 RPC 池。
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os
    from concurrent.futures import ThreadPoolExecutor

    led = Ledger(path)
    rpc_pool = ThreadPoolExecutor(max_workers=2)
    try:
        client = FakeClient()
        kc = KeyComputer(model_name="qwen", tp_size=2)
        monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
        b = MooncakeBackend(
            client=client,
            key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(),
            ledger=led,
            executor=rpc_pool,
        )
        handle = run(
            b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
        )
        rows = led.list_running_tasks()
        assert rows, "copy 后没有 running 行"
        handle_id = rows[0].handle_id
        rpc_pool.shutdown(wait=True)  # 模拟退出期那一瞬
        run(b.abandon(handle))  # 只写 ledger，不该需要 RPC 池
        statuses = {r.status for r in led.list_tasks_by_handle(handle_id)}
        assert "abandoned" in statuses, f"abandon 没记账: {statuses}"
    finally:
        led.close()
        os.unlink(path)


def test_cancel_during_ledger_write_after_failure_propagates(
    fake_store_module, monkeypatch
) -> None:
    """提交失败后的记账途中被取消：`CancelledError` 原样抛，不包成 `SubmitError`。

    原始异常是普通 `Exception`（比如第 2 个 key 提交失败）；此时若把记账途中的
    取消吞掉，调用方会收到 `SubmitError`——取消没传出去，消费者会当成预热失败
    继续拿下一个而不是退出。
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    try:
        class BoomClient(FakeClient):
            def create_copy_task(self, key, targets):
                if len(self.created) >= 1:
                    raise RuntimeError("submit boom")
                return super().create_copy_task(key, targets)

        client = BoomClient()
        kc = KeyComputer(model_name="qwen", tp_size=2)
        monkeypatch.setattr(
            kc, "keys_for_tokens", lambda ids: kc.expand(["h0", "h1"])
        )
        b = MooncakeBackend(
            client=client,
            key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(),
            ledger=led,
            submit_concurrency=1,  # 逐 key：第 2 个必失败，取消点确定
        )
        entered = threading.Event()
        orig_record = b._record

        async def slow_record(handle, status, tids=None):
            entered.set()
            await asyncio.sleep(0.5)  # 留出取消窗口
            await orig_record(handle, status, tids)

        b._record = slow_record

        async def scenario():
            task = asyncio.create_task(
                b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))
            )
            deadline = time.monotonic() + 3
            while not entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert entered.is_set(), "没进到记账，前提不成立"
            task.cancel()
            # 关键：必须是 CancelledError，不是 SubmitError
            with pytest.raises(asyncio.CancelledError):
                await task
            # 取消只打断调用方；被 shield 的记账要在同一事件循环里跑完。
            # 断言那条已发出的 task id 在 Ledger 里是 running——去掉 shield
            # 时取消直接从 `_record` 冒出，行没写上，而调用方照样收到
            # CancelledError，只有这条断言能抓住。
            deadline = time.monotonic() + 3
            rows = []
            while time.monotonic() < deadline:
                rows = led.list_running_tasks()
                if rows:
                    break
                await asyncio.sleep(0.01)
            assert [r.task_id for r in rows] == ["1:101"], (
                f"取消时已发出的任务号没落账成 running: {rows}"
            )

        run(scenario())
    finally:
        led.close()
        os.unlink(path)


def test_submit_failure_carries_partial_ids(fake_store_module, monkeypatch) -> None:
    """生成器中途失败：已发起的任务号必须附在异常上（否则无人跟踪）。"""

    class BoomClient(FakeClient):
        def create_copy_task(self, key, targets):
            with self._id_lock:
                already = len(self.created)
            if already >= 1:
                raise RuntimeError("submit boom")
            return super().create_copy_task(key, targets)

    client = BoomClient()
    b = _backend(
        client,
        KeyComputer(model_name="qwen", tp_size=2),
        monkeypatch,
        submit_concurrency=1,  # 逐 key：第 2 个必失败，不靠撞时序
    )
    with pytest.raises(SubmitError, match=r"1/2 tasks.*\[\(1, 101\)\]") as ei:
        run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
    assert ei.value.handle is not None
    assert ei.value.handle.task_ids == ((1, 101),)


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
        handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
        # 第一轮：全部失败且全是在途冲突 → 观测态 RUNNING，句柄保留
        assert run(b.query_task(handle)) is TaskStatus.RUNNING
        assert handle in b._plans
        # 冲突任务如实记 failed（它们确实失败了，数据是别人搬的）
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "failed"  # type: ignore[union-attr]
        # 观测态后续调用不再查任务（任务已死），只核验副本
        n_queried = len(client.queried)
        assert run(b.query_task(handle)) is TaskStatus.RUNNING
        assert len(client.queried) == n_queried
        # 副本到位（别人的任务传完）→ READY，终态删登记
        client._replicas_on = ["seg-A", "seg-B"]
        assert run(b.query_task(handle)) is TaskStatus.READY
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
    handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
    assert run(b.query_task(handle)) is TaskStatus.FAILED
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
        handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
        assert run(b.query_task(handle)) is TaskStatus.RUNNING  # 进观测态
        run(b.abandon(handle))
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
    assert run(b.exists(Prefix(model_id="qwen", token_ids=(1,)))) is False


def test_exists_all_present(fake_store_module, monkeypatch) -> None:
    b = _backend(FakeClient(), KeyComputer(model_name="qwen", tp_size=2), monkeypatch)
    assert run(b.exists(Prefix(model_id="qwen", token_ids=(1,)))) is True


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
        run(b.exists(Prefix(model_id="qwen", token_ids=(1,))))


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
        handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "running"  # type: ignore[union-attr]
        run(b.query_task(handle))
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
        run(b1.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
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
        assert len(b2.rebuild()) == 1  # 一个 handle（两任务一组）
        handle = next(iter(b2._plans))
        # 任务号解码回整数对（与创建时同形态）
        assert all(isinstance(t, tuple) for t in handle.task_ids)
        # 继续 query_task → 核验通过 → READY；Ledger 迁移 ready
        assert run(b2.query_task(handle)) is TaskStatus.READY
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
        led._corrupt_task_row("t-old")
        kc = KeyComputer(model_name="qwen", tp_size=2)
        monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
        b = MooncakeBackend(
            client=FakeClient(),
            key_computer=kc,
            locate=lambda _inst: ["seg-A", "seg-B"],
            capability=mooncake_v0311(),
            ledger=led,
        )
        assert b.rebuild() == []
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
        h1 = run(b1.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
        assert run(b1.query_task(h1)) is TaskStatus.RUNNING  # 观测态等副本
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
        assert len(b2.rebuild()) == 1
        h2 = next(iter(b2._plans))
        assert h2 in b2._watch  # 观测态恢复了
        assert run(b2.query_task(h2)) is TaskStatus.RUNNING  # 缺 seg-B 副本：等，不是终态

        # 副本全部到位 → READY；冲突行保持 failed，其余 ready
        client2._replicas_on = ["seg-A", "seg-B"]
        assert run(b2.query_task(h2)) is TaskStatus.READY
        assert led.get_task(encode_task_id((1, 101))).status == "failed"  # type: ignore[union-attr]
        assert led.get_task(encode_task_id((2, 102))).status == "ready"  # type: ignore[union-attr]
    finally:
        led.close()
        os.unlink(path)


def test_empty_handle_is_immediately_ready(fake_store_module) -> None:
    """空句柄（task_ids=()）立即 READY，不查登记、不核验。
    提回只在确认目标上已有内存副本之后才返回这种句柄。"""
    b = MooncakeBackend(
        client=FakeClient(),
        key_computer=KeyComputer(model_name="qwen", tp_size=1),
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
    )
    from kvcm.ops.base import TaskHandle

    empty = TaskHandle(op=OpKind.COPY, task_ids=())
    assert run(b.query_task(empty)) is TaskStatus.READY


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
        run(b1.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))

        def dead_locate(_inst):
            raise RuntimeError("instance gone")

        b2 = MooncakeBackend(
            client=FakeClient(),
            key_computer=kc,
            locate=dead_locate,
            capability=mooncake_v0311(),
            ledger=led,
        )
        assert b2.rebuild() == []
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
        handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B")))
        run(b.abandon(handle))
        assert handle not in b._plans
        for tid in handle.task_ids:
            assert led.get_task(encode_task_id(tid)).status == "abandoned"  # type: ignore[union-attr]
        # 放弃后句柄失效
        with pytest.raises(KeyError):
            run(b.query_task(handle))
        with pytest.raises(KeyError):
            run(b.abandon(handle))
    finally:
        led.close()
        import os

        os.unlink(path)


# ---------------------------------------------------------------------------
# pin：SOFT 走空写；HARD 登记保活，不改 hard pin 位
# ---------------------------------------------------------------------------


def _pin_backend(monkeypatch, cap) -> MooncakeBackend:
    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: ["k0"] if ids else [])
    return MooncakeBackend(
        client=FakeClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=cap,
    )


def test_pin_soft_calls_client(fake_store_module, monkeypatch) -> None:
    assert mooncake_main().soft_pin_after.channel is Support.YES
    seen = {}

    class Client(FakeClient):
        def soft_pin(self, keys, sizes, *, enable=True):
            seen["keys"] = list(keys)
            seen["sizes"] = list(sizes)
            seen["enable"] = enable

        def batch_get_replica_desc(self, keys):
            raise AssertionError("软钉取尺寸不能走 GetReplicaList")

    class Admin:
        def batch_query_keys(self, keys):
            from kvcm.backends.mooncake.admin_main import KeyReplicaQuery, MemoryReplicaView

            return {
                k: KeyReplicaQuery(
                    key=k, ok=True, memory=(MemoryReplicaView("seg-A", size=64),)
                )
                for k in keys
            }

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: ["k0"] if ids else [])
    b = MooncakeBackend(
        client=Client(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_main(),
        admin=Admin(),
    )
    run(b.pin(Prefix(model_id="qwen", token_ids=(1,)), PinKind.SOFT))
    assert seen["enable"] is True
    assert seen["keys"] == ["k0"]
    assert seen["sizes"] == [64]


def test_pin_soft_disable_calls_empty_write(fake_store_module, monkeypatch) -> None:
    seen = {}

    class Client(FakeClient):
        def soft_pin(self, keys, sizes, *, enable=True):
            seen["keys"] = list(keys)
            seen["sizes"] = list(sizes)
            seen["enable"] = enable

    class Admin:
        def batch_query_keys(self, keys):
            from kvcm.backends.mooncake.admin_main import KeyReplicaQuery, MemoryReplicaView

            return {
                k: KeyReplicaQuery(
                    key=k, ok=True, memory=(MemoryReplicaView("seg-A", size=8),)
                )
                for k in keys
            }

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: ["k0"] if ids else [])
    b = MooncakeBackend(
        client=Client(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_main(),
        admin=Admin(),
    )
    run(b.pin(Prefix(model_id="qwen", token_ids=(1,)), PinKind.SOFT, enable=False))
    assert seen == {"keys": ["k0"], "sizes": [8], "enable": False}
    with pytest.raises(ValueError, match="enable=False"):
        run(b.pin(Prefix(model_id="qwen", token_ids=(1,)), PinKind.HARD, enable=False))


def test_pin_soft_refuses_disk_replica_before_empty_write(
    fake_store_module, monkeypatch
) -> None:
    """有盘副本时空写收尾盖不住 PROCESSING 的盘副本，不能调用扩展。"""

    class Client(FakeClient):
        def soft_pin(self, keys, sizes, *, enable=True):
            raise AssertionError("有盘副本不应进入空写")

    def admin_for(**kwargs):
        class Admin:
            def batch_query_keys(self, keys):
                from kvcm.backends.mooncake.admin_main import (
                    KeyReplicaQuery,
                    MemoryReplicaView,
                )

                return {
                    k: KeyReplicaQuery(
                        key=k,
                        ok=True,
                        memory=(MemoryReplicaView("seg-A", size=64),),
                        **kwargs,
                    )
                    for k in keys
                }

        return Admin()

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: ["k0"] if ids else [])
    for extra in (
        {"disk_replicas": 1},
        {"local_disk_endpoints": ("10.0.0.2:9",)},
    ):
        b = MooncakeBackend(
            client=Client(),
            key_computer=kc,
            locate=lambda _inst: ["seg-A"],
            capability=mooncake_main(),
            admin=admin_for(**extra),
        )
        with pytest.raises(StoreClientError, match="有盘副本"):
            run(b.pin(Prefix(model_id="qwen", token_ids=(1,)), PinKind.SOFT))


def test_remove_false_stops_keepalive_and_sleeps(
    fake_store_module, monkeypatch
) -> None:
    client = RemoveClient()
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("kvcm.backends.mooncake.backend.asyncio.sleep", sleep)
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _move_backend(client, monkeypatch, ledger=led)
        b._lease_ttl_s = 10
        prefix = Prefix(model_id="qwen", token_ids=(1, 2))
        run(b.keepalive(prefix))
        run(b.remove(prefix, force=False))
        assert slept == [10]
        assert led.list_keepalive() == []
        assert client.removed[-1][1] is False

        slept.clear()
        run(b.keepalive(prefix))
        run(b.remove(prefix, force=True))
        assert slept == []
        assert led.list_keepalive() == []
        assert client.removed[-1][1] is True

        class Fail(RemoveClient):
            def batch_remove(self, keys, force=False):
                raise StoreClientError("batch_remove failed")

        fail = Fail()
        b._client = fail
        run(b.keepalive(prefix))
        with pytest.raises(StoreClientError, match="batch_remove failed"):
            run(b.remove(prefix, force=False))
        assert led.list_keepalive() == [prefix]
    finally:
        led.close()
        import os

        os.unlink(path)


def test_pin_hard_registers_keepalive(fake_store_module, monkeypatch) -> None:
    """pin(HARD) 只登记保活。不走空写，也不因为补不上 hard pin 位而拒绝。"""
    assert mooncake_main().hard_pin_after.channel is Support.NO
    assert mooncake_v0311().hard_pin_after.channel is Support.NO
    called = {"soft": 0}

    class Client(FakeClient):
        def soft_pin(self, keys, sizes, *, enable=True):
            called["soft"] += 1

        def batch_get_replica_desc(self, keys):
            raise AssertionError("pin(HARD) 不应读副本")

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        kc = KeyComputer(model_name="qwen", tp_size=1)
        monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: ["k0"] if ids else [])
        b = MooncakeBackend(
            client=Client(),
            key_computer=kc,
            locate=lambda _inst: ["seg-A"],
            capability=mooncake_main(),
            ledger=led,
            lease_ttl_s=0,
        )
        prefix = Prefix(model_id="qwen", token_ids=(1,))
        run(b.pin(prefix, PinKind.HARD))
        run(b.pin(prefix, PinKind.HARD))
        assert led.list_keepalive() == [prefix]
        assert called["soft"] == 0
    finally:
        led.close()
        import os

        os.unlink(path)


def test_memory_replica_predicate_calls_method() -> None:
    """is_memory_replica 是方法时要调用。布尔属性仍然直接用。"""
    called = {"disk": 0, "mem": 0}

    class Disk:
        def is_memory_replica(self):
            return False

        def get_memory_descriptor(self):
            called["disk"] += 1
            raise RuntimeError("Expected MemoryDescriptor")

    class MemBuf:
        size = 32
        transport_endpoint = "10.0.0.3:1"

    class MemDesc:
        buffer_descriptor = MemBuf()

    class Mem:
        def is_memory_replica(self):
            return True

        def get_memory_descriptor(self):
            called["mem"] += 1
            return MemDesc()

    class BoolMem:
        is_memory_replica = True

        def get_memory_descriptor(self):
            return MemDesc()

    class Store:
        def batch_get_replica_desc(self, keys):
            return {keys[0]: [Disk(), Mem(), BoolMem()]}

    client = StoreClient()
    client._store = Store()
    eps = client.batch_memory_replica_endpoints(["k"])
    assert eps["k"] == ["10.0.0.3:1", "10.0.0.3:1"]
    assert called["disk"] == 0

    class Bare:
        pass

    class BareStore:
        def batch_get_replica_desc(self, keys):
            return {keys[0]: [Bare()]}

    bare = StoreClient()
    bare._store = BareStore()
    with pytest.raises(StoreClientError, match="绑定面不对"):
        bare.batch_memory_replica_endpoints(["k"])


def test_soft_pin_reuses_existing_capsule(monkeypatch) -> None:
    seen = {}

    class Buf:
        size = 64

    class Mem:
        buffer_descriptor = Buf()

    class Desc:
        is_memory_replica = True

        def get_memory_descriptor(self):
            return Mem()

    class Store:
        def batch_get_replica_desc(self, keys):
            raise AssertionError("软钉取尺寸不能走 GetReplicaList")

        def _get_pyclient_capsule(self):
            return "existing-client"

    def _pin(capsule, keys, sizes, enable):
        seen["capsule"] = capsule
        seen["keys"] = list(keys)
        seen["sizes"] = list(sizes)
        seen["enable"] = enable

    monkeypatch.setattr("kvcm.native.empty_pin.empty_soft_pin", _pin)
    client = StoreClient()
    client._store = Store()
    client.soft_pin(["h0"], [64], enable=True)
    assert seen == {
        "capsule": "existing-client",
        "keys": ["h0"],
        "sizes": [64],
        "enable": True,
    }


def test_soft_pin_missing_size_does_not_call_extension(monkeypatch) -> None:
    def _pin(*_args):
        raise AssertionError("没有尺寸时不应进入扩展")

    monkeypatch.setattr("kvcm.native.empty_pin.empty_soft_pin", _pin)

    class Store:
        def batch_get_replica_desc(self, keys):
            return {k: [] for k in keys}

        def _get_pyclient_capsule(self):
            return "existing-client"

    client = StoreClient()
    client._store = Store()
    with pytest.raises(StoreClientError, match="没有钉上"):
        client.soft_pin(["h0"], [], enable=True)


def test_empty_pin_extension_rejects_non_capsule() -> None:
    """虚拟环境里加载编好的扩展。不连 master，只检查入参被拒绝。

    ``_empty_pin.so`` 被 gitignore，CI 不编译。导不进就跳过，
    干净检出不应因此失败。
    """
    ext = pytest.importorskip("kvcm.native._empty_pin")

    assert callable(ext.empty_soft_pin)
    with pytest.raises(TypeError):
        ext.empty_soft_pin("not-a-capsule", ["k"], [8], True)


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
            led._backdate_tasks(days=8, exclude=("new-ready",))
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


# ---------------------------------------------------------------------------
# drain（退出时等 RPC 线程真正退完）
# ---------------------------------------------------------------------------


def test_rpc_timeout_raises_query_error(fake_store_module, monkeypatch) -> None:
    """RPC 卡住超过 rpc_timeout_s → 抛 QueryError（可重试语义）。

    线程不会被打断（Python 无法中断 C++ 调用）——它继续跑到自然结束，
    由退出时 `close()` 的有界等待兜底。调用方按可重试处理，不会无限等。
    """

    class HangingClient(FakeClient):
        def is_exist(self, keys):
            time.sleep(1.0)
            return [True] * len(keys)

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    _backend_with_timeout = MooncakeBackend(
        client=HangingClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
        rpc_timeout_s=0.1,
    )
    from kvcm.ops.base import QueryError

    async def scenario():
        with pytest.raises(QueryError, match="RPC timeout"):
            await _backend_with_timeout.exists(Prefix(model_id="qwen", token_ids=(1,)))

    run(scenario())
    _backend_with_timeout.close()


def test_rpc_timeout_disabled_when_zero(fake_store_module, monkeypatch) -> None:
    """rpc_timeout_s <= 0 表示不设超时（离线/调试场景）。"""

    class FastClient(FakeClient):
        def is_exist(self, keys):
            return [True] * len(keys)

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    b = MooncakeBackend(
        client=FastClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
        rpc_timeout_s=0,
    )

    async def scenario():
        assert await b.exists(Prefix(model_id="qwen", token_ids=(1,))) is True

    run(scenario())
    b.close()


def test_copy_submit_timeout_is_submit_error(fake_store_module, monkeypatch) -> None:
    """copy 提交超时 → SubmitError（不是 QueryError）——提交阶段的失败
    语义归 SubmitError，异常消息带已发任务号。"""

    class SlowSubmitClient(FakeClient):
        def create_copy_task(self, key, targets):
            time.sleep(1.0)
            return super().create_copy_task(key, targets)

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    b = MooncakeBackend(
        client=SlowSubmitClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
        rpc_timeout_s=0.1,
    )
    from kvcm.ops.base import SubmitError

    async def scenario():
        with pytest.raises(SubmitError):
            await b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst-B"))

    run(scenario())
    b.close()



def test_close_waits_bounded_for_inflight_rpc(fake_store_module, monkeypatch) -> None:
    """close() 有界等线程：慢 RPC 会被等到，卡死的不会永远拖住退出。"""
    from kvcm.ops.base import QueryError

    class SlowClient(FakeClient):
        def is_exist(self, keys):
            time.sleep(0.2)
            return [True] * len(keys)

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    b = MooncakeBackend(
        client=SlowClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
        rpc_timeout_s=0.05,
    )
    p = Prefix(model_id="qwen", token_ids=(1,))

    async def scenario():
        with pytest.raises(QueryError):
            await b.exists(p)  # 超时返回，但线程还在跑

    run(scenario())
    # close 会等这个线程（0.2s 内跑完，5s 上限足够）
    import time as _t

    start = _t.monotonic()
    got = b.close(wait_timeout_s=5.0)
    elapsed = _t.monotonic() - start
    # 关键：close 等了那个慢线程（≥0.15s），不是立刻返回
    assert elapsed >= 0.1, f"close 没等慢 RPC（{elapsed:.2f}s）"
    # 等完了要如实返回 True——调用方靠这个值决定要不要硬退出
    assert got is True, f"线程等完了却报没等完（返回 {got!r}）"


def test_close_bounded_when_rpc_hangs(fake_store_module, monkeypatch) -> None:
    """RPC 卡死：close 不会永远挂住（有界）。"""
    import time as _time

    class HangingClient(FakeClient):
        def is_exist(self, keys):
            _time.sleep(3)
            return [True] * len(keys)

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    b = MooncakeBackend(
        client=HangingClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
        rpc_timeout_s=0.05,
    )
    p = Prefix(model_id="qwen", token_ids=(1,))

    async def scenario():
        with pytest.raises(Exception):
            await b.exists(p)

    run(scenario())
    start = _time.monotonic()
    got = b.close(wait_timeout_s=0.3)  # 远小于 RPC 的 3s
    elapsed = _time.monotonic() - start
    assert elapsed < 1.5, f"close 没有有界返回（用了 {elapsed:.2f}s）"
    # 超时必须如实报 False：调用方正是靠它触发 os._exit(1)。
    # 原实现无返回值（隐式 None），这个信号被静默丢掉。
    assert got is False, f"超时没报 False（返回 {got!r}），调用方无从判断"


# ---------------------------------------------------------------------------
# move / remove（绑定 create_move_task、batch_remove；容量判断只在 move）
# ---------------------------------------------------------------------------


def _move_directory():
    """源、目标各一份段名单。没登记的实例不会落到其中任何一份。"""
    from kvcm.instances import InstanceDirectory

    directory = InstanceDirectory()
    directory.register(InstanceId("src"), ["seg-S0", "seg-S1"])
    directory.register(InstanceId("tgt"), ["seg-T0", "seg-T1"])
    return directory


class MoveClient(FakeClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.moves: list[tuple[str, str, str]] = []

    def create_move_task(self, key, source, target):
        with self._id_lock:
            self.moves.append((key, source, target))
            self._next += 1
            tid = (self._next, 100 + self._next)
        return tid


class RemoveClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.removed: list[tuple[list[str], bool]] = []

    def batch_remove(self, keys, force=False):
        self.removed.append((list(keys), force))
        return [0] * len(keys)


def _move_backend(client, monkeypatch, ledger=None, capability=None):
    from kvcm.backends.mooncake.capability import mooncake_v0311

    kc = KeyComputer(model_name="qwen", tp_size=2)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    return MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=_move_directory().locate,
        capability=capability or mooncake_v0311(),
        ledger=ledger,
    )


def test_move_between_two_registered_instances(fake_store_module, monkeypatch) -> None:
    """A、B 各有自己的段。移动用 locate(源) 和 locate(目标)，两边不是同一段。"""
    from kvcm.instances import InstanceDirectory

    directory = InstanceDirectory()
    directory.register(InstanceId("A"), ["seg-A0", "seg-A1"])
    directory.register(InstanceId("B"), ["seg-B0", "seg-B1"])
    assert directory.locate(InstanceId("A")) != directory.locate(InstanceId("B"))

    client = MoveClient()
    kc = KeyComputer(model_name="qwen", tp_size=2)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    b = MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=directory.locate,
        capability=mooncake_v0311(),
    )

    async def admit(plan):
        return None

    b._admit_new_bytes = admit
    run(b.move(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("B"), InstanceId("A")))
    assert [(src, tgt) for _, src, tgt in client.moves] == [
        ("seg-A0", "seg-B0"),
        ("seg-A1", "seg-B1"),
    ]
    assert {src for _, src, _ in client.moves}.isdisjoint(
        {tgt for _, _, tgt in client.moves}
    )

    # 进程已经在用这份目录，还可以再登记一个新实例。
    directory.register(InstanceId("C"), ["seg-C0"])
    assert directory.locate(InstanceId("C")) == ("seg-C0",)

    with pytest.raises(IntentInputError, match="not registered"):
        directory.locate(InstanceId("missing"))
    client.moves.clear()

    async def admit_should_not_run(plan):
        raise AssertionError("未登记的实例不该进入容量判断")

    b._admit_new_bytes = admit_should_not_run
    with pytest.raises(IntentInputError, match="not registered"):
        run(b.move(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("B"), InstanceId("missing")))
    assert client.moves == []


def test_move_admits_then_submits_one_task_per_key(fake_store_module, monkeypatch) -> None:
    """提交前做容量判断，然后按 rank 逐 key 调 create_move_task。"""
    client = MoveClient()
    b = _move_backend(client, monkeypatch)
    log: list[tuple] = []
    original = client.create_move_task

    def record_move(key, source, target):
        log.append(("move", source, target))
        return original(key, source, target)

    client.create_move_task = record_move

    async def admit(plan):
        log.append(("admit", [(k, seg) for k, seg in plan]))

    b._admit_new_bytes = admit
    prefix = Prefix(model_id="qwen", token_ids=(1, 2, 3))
    handle = run(b.move(prefix, InstanceId("tgt"), InstanceId("src")))
    assert handle.op is OpKind.MOVE
    assert log[0][0] == "admit"
    assert [seg for _, seg in log[0][1]] == ["seg-T0", "seg-T1"]
    assert [(src, tgt) for _, src, tgt in client.moves] == [
        ("seg-S0", "seg-T0"),
        ("seg-S1", "seg-T1"),
    ]
    # 容量判断整次 move 只走一遍，且在任何移动任务之前
    assert [kind for kind, *_ in log] == ["admit", "move", "move"]
    assert len(client.moves) == len(handle.task_ids) == 2
    assert all(isinstance(t, tuple) for t in handle.task_ids)


def test_move_does_not_submit_when_admit_rejects(fake_store_module, monkeypatch) -> None:
    client = MoveClient()
    b = _move_backend(client, monkeypatch)

    async def admit(plan):
        raise RuntimeError("no room")

    b._admit_new_bytes = admit
    with pytest.raises(RuntimeError, match="no room"):
        run(b.move(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("tgt"), InstanceId("src")))
    assert client.moves == []


def test_move_without_admit_raises_before_submit(fake_store_module, monkeypatch) -> None:
    """本类不定义 _admit_new_bytes。没注入时在 create_move_task 之前失败。"""
    client = MoveClient()
    b = _move_backend(client, monkeypatch)
    assert "_admit_new_bytes" not in MooncakeBackend.__dict__
    with pytest.raises(RuntimeError, match="_admit_new_bytes"):
        run(
            b.move(
                Prefix(model_id="qwen", token_ids=(1,)),
                InstanceId("tgt"),
                InstanceId("src"),
            )
        )
    assert client.moves == []


def test_move_requires_source_instance(fake_store_module, monkeypatch) -> None:
    client = MoveClient()
    b = _move_backend(client, monkeypatch)
    called = []

    async def admit(plan):
        called.append(plan)

    b._admit_new_bytes = admit
    with pytest.raises(ValueError, match="源实例"):
        run(b.move(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("tgt")))
    assert called == []
    assert client.moves == []


def test_remove_deletes_expanded_keys_without_admit(fake_store_module, monkeypatch) -> None:
    client = RemoveClient()
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _move_backend(client, monkeypatch, ledger=led)
        b._lease_ttl_s = 0
        called = []

        async def admit(plan):
            called.append(plan)

        b._admit_new_bytes = admit
        kc = b._keys
        expected = kc.expand(["h0"])
        run(b.remove(Prefix(model_id="qwen", token_ids=(1, 2))))
        assert client.removed == [(expected, False)]
        assert called == []
    finally:
        led.close()
        import os

        os.unlink(path)


def test_client_move_and_remove_use_binding_symbols() -> None:
    """包装层把参数原样交给绑定符号，不改名、不加目标列表。"""

    class Bound:
        def __init__(self):
            self.calls = []

        def create_move_task(self, key, source, target):
            self.calls.append(("create_move_task", key, source, target))
            return ((7, 8), 0)

        def batch_remove(self, keys, force=False):
            self.calls.append(("batch_remove", list(keys), force))
            return [0] * len(keys)

    store = Bound()
    client = StoreClient()
    client._store = store
    assert client.create_move_task("k", "seg-src", "seg-tgt") == (7, 8)
    assert client.batch_remove(["a", "b"]) == [0, 0]
    assert store.calls == [
        ("create_move_task", "k", "seg-src", "seg-tgt"),
        ("batch_remove", ["a", "b"], False),
    ]
    store.calls.clear()
    store.create_move_task = lambda key, source, target: ((0, 0), 5)  # type: ignore[method-assign]
    with pytest.raises(StoreClientError, match="create_move_task failed"):
        client.create_move_task("k", "s", "t")
    store.batch_remove = lambda keys, force=False: [0, -1]  # type: ignore[method-assign]
    with pytest.raises(StoreClientError, match="batch_remove failed"):
        client.batch_remove(["a", "b"])


def test_verify_uses_admin_batch_query_not_binding(fake_store_module, monkeypatch) -> None:
    """核验走 /batch_query_keys，不调用 batch_get_replica_desc。"""
    from kvcm.backends.mooncake.admin_main import KeyReplicaQuery, MemoryReplicaView

    client = MoveClient()
    binding_calls = {"n": 0}
    original = client.batch_memory_replica_endpoints

    def wrapped(keys):
        binding_calls["n"] += 1
        return original(keys)

    client.batch_memory_replica_endpoints = wrapped

    class Admin:
        def __init__(self):
            self.calls = 0

        def batch_query_keys(self, keys):
            self.calls += 1
            return {
                k: KeyReplicaQuery(
                    key=k,
                    ok=True,
                    memory=(MemoryReplicaView("seg-T0"), MemoryReplicaView("seg-T1")),
                )
                for k in keys
            }

    admin = Admin()
    kc = KeyComputer(model_name="qwen", tp_size=2)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    b = MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=_move_directory().locate,
        capability=mooncake_main(),
        admin=admin,
    )
    handle = run(b.copy(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("tgt")))
    assert run(b.query_task(handle)) is TaskStatus.READY
    assert admin.calls == 1
    assert binding_calls["n"] == 0


def test_rebuild_restores_move(fake_store_module, monkeypatch) -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    import os

    led = Ledger(path)
    try:
        client = MoveClient(task_status=_FakeStoreTaskStatus.RUNNING)
        b1 = _move_backend(client, monkeypatch, ledger=led)

        async def admit(plan):
            return None

        b1._admit_new_bytes = admit
        run(b1.move(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("tgt"), InstanceId("src")))
        assert len(led.list_running_tasks()) == 2

        client2 = FakeClient(
            task_status=_FakeStoreTaskStatus.SUCCESS,
            replicas_on=["seg-T0", "seg-T1"],
        )
        b2 = _move_backend(client2, monkeypatch, ledger=led)
        restored = b2.rebuild()
        assert len(restored) == 1
        assert restored[0].op is OpKind.MOVE
        assert run(b2.query_task(restored[0])) is TaskStatus.READY
    finally:
        led.close()
        os.unlink(path)


def test_remove_force_stops_keepalive_first(fake_store_module, monkeypatch) -> None:
    client = RemoveClient()
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _move_backend(client, monkeypatch, ledger=led)
        prefix = Prefix(model_id="qwen", token_ids=(1, 2))
        b._lease_ttl_s = 0
        run(b.keepalive(prefix))
        assert led.list_keepalive() == [prefix]
        run(b.remove(prefix, force=False))
        assert led.list_keepalive() == []
        assert client.removed[-1][1] is False
        run(b.keepalive(prefix))
        run(b.remove(prefix, force=True))
        assert led.list_keepalive() == []
        assert client.removed[-1][1] is True
    finally:
        led.close()
        import os

        os.unlink(path)


def test_keepalive_uses_ledger_table(fake_store_module, monkeypatch) -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _backend(FakeClient(), KeyComputer(model_name="qwen", tp_size=1), monkeypatch, ledger=led)
        prefix = Prefix(model_id="qwen", token_ids=(1,))
        run(b.keepalive(prefix))
        assert led.list_keepalive() == [prefix]
        run(b.stop_keepalive(prefix))
        assert led.list_keepalive() == []
    finally:
        led.close()
        import os

        os.unlink(path)


def _demote_backend(monkeypatch, led, admin, *, lease_ttl_s: float):
    client = FakeClient()

    def batch_get_replica_desc(keys):
        raise AssertionError("demote 不能走 GetReplicaList，那会续租")

    client.batch_get_replica_desc = batch_get_replica_desc
    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: ["k0"] if ids else [])
    return MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_main(),
        ledger=led,
        admin=admin,
        lease_ttl_s=lease_ttl_s,
    )


def test_demote_v0311_refuses_before_stop_keepalive(
    fake_store_module, monkeypatch
) -> None:
    """0.3.11 查不到盘副本字段。通道 NO，停保活之前就拒绝。"""
    assert mooncake_v0311().has_offload.channel is Support.NO
    assert mooncake_main().has_offload.channel is Support.YES
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _demote_backend(monkeypatch, led, object(), lease_ttl_s=0)
        b._cap = mooncake_v0311()
        prefix = Prefix(model_id="qwen", token_ids=(1,))
        run(b.keepalive(prefix))
        with pytest.raises(UnsupportedCapabilityError, match="盘副本"):
            run(b.demote(prefix))
        assert led.list_keepalive() == [prefix]
    finally:
        led.close()
        import os

        os.unlink(path)


def test_demote_fails_without_disk_replica(fake_store_module, monkeypatch) -> None:
    from kvcm.backends.mooncake.admin_main import KeyReplicaQuery

    steps: list[tuple] = []

    async def fake_sleep(seconds):
        steps.append(("sleep", seconds, list(led.list_keepalive())))

    class Admin:
        def batch_query_keys(self, keys):
            steps.append(("query", list(keys)))
            return {k: KeyReplicaQuery(key=k, ok=True, disk_replicas=0) for k in keys}

    monkeypatch.setattr(
        "kvcm.backends.mooncake.backend.asyncio.sleep", fake_sleep
    )
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _demote_backend(monkeypatch, led, Admin(), lease_ttl_s=0)
        prefix = Prefix(model_id="qwen", token_ids=(1,))
        run(b.keepalive(prefix))
        with pytest.raises(VerifyError, match="还没有盘副本"):
            run(b.demote(prefix))
        assert led.list_keepalive() == [prefix]
        assert steps == [("sleep", 0, []), ("query", ["k0"])]
    finally:
        led.close()
        import os

        os.unlink(path)


def test_demote_succeeds_when_every_key_has_disk(fake_store_module, monkeypatch) -> None:
    from kvcm.backends.mooncake.admin_main import KeyReplicaQuery

    steps: list[tuple] = []

    async def fake_sleep(seconds):
        steps.append(("sleep", seconds))

    class Admin:
        def batch_query_keys(self, keys):
            steps.append(("query", list(keys)))
            return {k: KeyReplicaQuery(key=k, ok=True, disk_replicas=1) for k in keys}

    monkeypatch.setattr(
        "kvcm.backends.mooncake.backend.asyncio.sleep", fake_sleep
    )
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _demote_backend(monkeypatch, led, Admin(), lease_ttl_s=0.01)
        prefix = Prefix(model_id="qwen", token_ids=(1,))
        run(b.keepalive(prefix))
        run(b.demote(prefix))
        assert led.list_keepalive() == []
        assert steps == [("sleep", 0.01), ("query", ["k0"])]
    finally:
        led.close()
        import os

        os.unlink(path)


def test_demote_keeps_keepalive_until_preconditions_hold(
    fake_store_module, monkeypatch
) -> None:
    """没有 admin、或展开不出 key 时，还没停保活。"""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    led = Ledger(path)
    try:
        b = _demote_backend(monkeypatch, led, object(), lease_ttl_s=0)
        prefix = Prefix(model_id="qwen", token_ids=(1,))
        run(b.keepalive(prefix))
        b._admin = None
        with pytest.raises(QueryError, match="batch_query_keys"):
            run(b.demote(prefix))
        assert led.list_keepalive() == [prefix]

        monkeypatch.setattr(b._keys, "keys_for_tokens", lambda ids: [])
        b._admin = object()
        with pytest.raises(IntentInputError, match="shorter than one block"):
            run(b.demote(prefix))
        assert led.list_keepalive() == [prefix]
    finally:
        led.close()
        import os

        os.unlink(path)


def test_promote_hits_replica_list_not_admin(fake_store_module, monkeypatch) -> None:
    client = FakeClient(replicas_on=["seg-A"])
    hits = {"n": 0}
    original = client.batch_memory_replica_endpoints

    def wrapped(keys):
        hits["n"] += 1
        return original(keys)

    client.batch_memory_replica_endpoints = wrapped

    class Admin:
        def batch_query_keys(self, keys):
            from kvcm.backends.mooncake.admin_main import KeyReplicaQuery

            return {
                k: KeyReplicaQuery(
                    key=k, ok=True, local_disk_endpoints=("seg-A",)
                )
                for k in keys
            }

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    b = MooncakeBackend(
        client=client,
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_main(),
        admin=Admin(),
        promote_hits=2,
    )
    handle = run(b.promote(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst")))
    assert hits["n"] == 2
    assert handle.task_ids == ()
    assert run(b.query_task(handle)) is TaskStatus.READY

    client._replicas_on = ["seg-ELSE"]
    with pytest.raises(VerifyError, match="已按 2 次触发"):
        run(b.promote(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst")))


def test_promote_offload_endpoint_does_not_skip_replica_list(
    fake_store_module, monkeypatch
) -> None:
    """offload 端点不是 seg 名，不能据此跳过 GetReplicaList。

    打完 N 次仍没有内存副本，仍然 VerifyError。
    """
    hits = {"n": 0}

    class Client(FakeClient):
        def batch_memory_replica_endpoints(self, keys):
            hits["n"] += 1
            return {k: ["seg-ELSE"] for k in keys}

    class Admin:
        def batch_query_keys(self, keys):
            raise AssertionError("提回不该拿 local_disk 端点对 seg 名")

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]) if ids else [])
    b = MooncakeBackend(
        client=Client(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_main(),
        admin=Admin(),
        promote_hits=2,
    )
    with pytest.raises(VerifyError, match="已按 2 次触发"):
        run(b.promote(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst")))
    assert hits["n"] == 2


def test_promote_v0311_hits_without_admin(fake_store_module, monkeypatch) -> None:
    assert mooncake_v0311().has_promotion.channel is Support.YES
    assert mooncake_main().has_promotion.channel is Support.YES

    class Admin:
        def batch_query_keys(self, keys):
            raise AssertionError("提回不该查 admin")

    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]) if ids else [])
    b = MooncakeBackend(
        client=FakeClient(replicas_on=["seg-A"]),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
        admin=Admin(),
        promote_hits=2,
    )
    handle = run(b.promote(Prefix(model_id="qwen", token_ids=(1,)), InstanceId("inst")))
    assert handle.task_ids == ()


def test_retain_channel_not_present(fake_store_module, monkeypatch) -> None:
    assert mooncake_main().has_retained_client.rpc is Support.NO
    assert mooncake_main().has_retained_client.channel is Support.NO
    kc = KeyComputer(model_name="qwen", tp_size=1)
    monkeypatch.setattr(kc, "keys_for_tokens", lambda ids: kc.expand(["h0"]))
    b = MooncakeBackend(
        client=FakeClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_main(),
    )
    with pytest.raises(UnsupportedCapabilityError, match="没有该接口"):
        run(b.retain(Prefix(model_id="qwen", token_ids=(1,))))

    b0311 = MooncakeBackend(
        client=FakeClient(),
        key_computer=kc,
        locate=lambda _inst: ["seg-A"],
        capability=mooncake_v0311(),
    )
    with pytest.raises(UnsupportedCapabilityError):
        run(b0311.retain(Prefix(model_id="qwen", token_ids=(1,))))
