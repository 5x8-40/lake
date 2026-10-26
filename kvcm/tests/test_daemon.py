"""进程壳（daemon）单元检查：启动序列、恢复轮询、优雅退出。

fake client/backend 注入（monkeypatch StoreClient 与 build_backend），
不发真实信号——stop event 直接注入。
"""

from __future__ import annotations

import argparse
import os
import tempfile
import threading
import time

from kvcm import daemon
from kvcm.ops.base import OpKind, QueryError, TaskHandle, TaskStatus, VerifyError

_H1 = TaskHandle(op=OpKind.COPY, task_ids=((1, 101),))
_H2 = TaskHandle(op=OpKind.COPY, task_ids=((2, 102),))


class FakeClient:
    def __init__(self) -> None:
        self.connected: dict | None = None
        self.closed = False

    def connect(self, **kw) -> None:
        self.connected = kw

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    """脚本化 rebuild/query_task/abandon。"""

    def __init__(self, *, restored=(), statuses=None) -> None:
        self._restored = list(restored)
        self._statuses = dict(statuses or {})
        self.abandoned: list[TaskHandle] = []

    def rebuild(self):
        return list(self._restored)

    def query_task(self, handle):
        return self._statuses.get(handle, TaskStatus.RUNNING)

    def abandon(self, handle):
        self.abandoned.append(handle)
        self._statuses[handle] = TaskStatus.FAILED


def _args(tmp: str) -> argparse.Namespace:
    return argparse.Namespace(
        master="127.0.0.1:50088",
        local_hostname="127.0.0.1:13021",
        protocol="tcp",
        model_name="qwen",
        block_size=128,
        tp_size=2,
        peer_tp_size=None,
        put_step=1,
        hash_algo="sha256",
        segs="seg-A,seg-B",
        capability="v0311",
        ledger=os.path.join(tmp, "ledger.sqlite"),
        keepalive_interval_s=999.0,  # 测试不依赖周期 sweep
        keepalive_window_s=600.0,
    )


def _run_with_fakes(monkeypatch, backend, tmp, stop):
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda a, c, l: backend)
    rc = daemon.run(_args(tmp), stop=stop)
    return rc, client


def test_startup_rebuild_loop_shutdown_sequence(monkeypatch, tmp_path) -> None:
    """启动 → rebuild 恢复 → 优雅退出：在途 abandon + close + 账本收口。"""
    backend = FakeBackend(restored=[_H1, _H2])
    stop = threading.Event()
    t = threading.Thread(
        target=_run_with_fakes,
        args=(monkeypatch, backend, str(tmp_path), stop),
    )
    t.start()
    # 等启动完成（恢复线程把 _H1 跑到 READY、_H2 仍在途）
    backend._statuses[_H1] = TaskStatus.READY
    deadline = time.monotonic() + 5
    while t.is_alive() and time.monotonic() < deadline:
        # 等 rebuild 恢复生效（ledger 文件建出来 = 启动序列已过）
        if os.path.exists(os.path.join(str(tmp_path), "ledger.sqlite")):
            break
        time.sleep(0.01)
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    # 退出时 _H2 在途 → abandon 记账
    assert _H2 in backend.abandoned


def test_no_restored_tasks_still_clean_shutdown(monkeypatch, tmp_path) -> None:
    rc_holder = {}
    stop = threading.Event()
    backend = FakeBackend(restored=[])

    def _r():
        rc_holder["rc"], client = _run_with_fakes(monkeypatch, backend, str(tmp_path), stop)
        rc_holder["client"] = client

    t = threading.Thread(target=_r)
    t.start()
    time.sleep(0.2)  # 启动（无恢复任务）
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert rc_holder["rc"] == 0
    assert rc_holder["client"].closed  # ④ close 必达
    assert rc_holder["client"].connected["master_server_address"] == "127.0.0.1:50088"


# ---------------------------------------------------------------------------
# recover_poll（极简恢复轮询）
# ---------------------------------------------------------------------------


class _PollBackend(FakeBackend):
    def __init__(self, script):
        super().__init__()
        self._script = list(script)
        self.calls = 0

    def query_task(self, handle):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_recover_poll_runs_to_terminal() -> None:
    backend = _PollBackend([TaskStatus.RUNNING, TaskStatus.READY])
    daemon.recover_poll(backend, [_H1], threading.Event(), poll_s=0.01)
    assert backend.calls == 2
    assert backend.abandoned == []


def test_recover_poll_query_error_retried() -> None:
    backend = _PollBackend([QueryError("boom"), TaskStatus.READY])
    daemon.recover_poll(backend, [_H1], threading.Event(), poll_s=0.01)
    assert backend.calls == 2
    assert backend.abandoned == []


def test_recover_poll_timeout_abandons() -> None:
    backend = _PollBackend([TaskStatus.RUNNING] * 100)
    daemon.recover_poll(backend, [_H1], threading.Event(), poll_s=0.01, timeout_s=0.05)
    assert backend.abandoned == [_H1]  # 超时收口（与 PrefetchFlow 同语义）


def test_recover_poll_verify_error_stops_tracking() -> None:
    backend = _PollBackend([VerifyError("replica missing")])
    daemon.recover_poll(backend, [_H1], threading.Event(), poll_s=0.01)
    assert backend.abandoned == []  # 终态（核验失败已记账），不再跟踪


def test_recover_poll_stop_event_interrupts() -> None:
    backend = _PollBackend([TaskStatus.RUNNING] * 100)
    stop = threading.Event()
    t = threading.Thread(
        target=daemon.recover_poll, args=(backend, [_H1], stop), kwargs={"poll_s": 0.02}
    )
    t.start()
    time.sleep(0.05)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert backend.abandoned == []  # stop 不是超时，不 abandon（归退出流程统一做）
