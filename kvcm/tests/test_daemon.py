"""进程壳单元检查：启动/恢复/退出序列与参数组装（fake client，离线可跑）。"""

from __future__ import annotations

import argparse
import threading
import time

from kvcm import daemon
from kvcm.ledger import Ledger
from kvcm.ops.base import OpKind, TaskHandle, TaskStatus

_H1 = TaskHandle(op=OpKind.COPY, task_ids=((1, 2),))


class FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.closed = False

    def connect(self, **kw) -> None:
        self.kw = kw
        self.connected = True

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    """记录 rebuild 调用；恢复 handle 脚本化；任务面（query/abandon）
    支持 track 轮询与退出收口剧本。"""

    def __init__(self, restored=()) -> None:
        self._restored = list(restored)
        self.rebuild_calls = 0
        self.first_query = threading.Event()
        self.abandoned: list[TaskHandle] = []

    def rebuild(self):
        self.rebuild_calls += 1
        return list(self._restored)

    def prefix_of(self, handle):
        return None

    def query_task(self, handle):
        self.first_query.set()
        if handle in self.abandoned:
            raise KeyError("unknown task handle")  # abandon 后即遗忘
        return TaskStatus.RUNNING

    def abandon(self, handle) -> None:
        self.abandoned.append(handle)


class FakeKeepalive:
    def __init__(self, *a, **kw) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


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
        segs="127.0.0.1:10001,127.0.0.1:10002",
        capability="v0311",
        ledger=f"{tmp}/ledger.sqlite",
        max_concurrent=4,
        listen="",
        keepalive_interval_s=0.05,
        keepalive_window_s=600.0,
    )


def test_startup_rebuild_loop_shutdown_sequence(monkeypatch, tmp_path) -> None:
    """启动 = connect→backend→rebuild→track 收编→loop.start；退出 =
    stop→engine.shutdown→loop.stop→client.close，次序与调用齐全。"""
    backend = FakeBackend(restored=[_H1])
    events: list[str] = []

    class TrackRecordingEngine(daemon.TaskEngine):
        def __init__(self, backend, **kw) -> None:
            super().__init__(backend, **kw)
            self._record = events

        def track(self, handle, **kw):
            self._record.append("track")
            return super().track(handle, **kw)

    monkeypatch.setattr(daemon, "TaskEngine", TrackRecordingEngine)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)

    stop = threading.Event()
    t = threading.Thread(target=daemon.run, args=(_args(tmp_path),), kwargs={"stop": stop})
    t.start()
    deadline = time.monotonic() + 5
    while backend.rebuild_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "track" in events
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert client.connected and client.closed


def test_no_restored_tasks_still_clean_shutdown(monkeypatch, tmp_path) -> None:
    backend = FakeBackend()
    client = FakeClient()
    loop = FakeKeepalive()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", lambda *a, **kw: loop)

    stop = threading.Event()
    t = threading.Thread(target=daemon.run, args=(_args(tmp_path),), kwargs={"stop": stop})
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(timeout=5)
    assert backend.rebuild_calls == 1
    assert loop.started and loop.stopped
    assert client.closed


def test_graceful_shutdown_abandons_in_flight(monkeypatch, tmp_path) -> None:
    """优雅退出回归：退出信号到达时恢复跟踪仍在轮询（RUNNING），
    engine.shutdown 必须在 loop.stop 前把在途 handle abandon 掉。"""
    backend = FakeBackend(restored=[_H1])
    client = FakeClient()
    loop = FakeKeepalive()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", lambda *a, **kw: loop)

    stop = threading.Event()
    t = threading.Thread(target=daemon.run, args=(_args(tmp_path),), kwargs={"stop": stop})
    t.start()
    assert backend.first_query.wait(timeout=5)  # track 已在轮询 ⇒ handle 已在在途集
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert backend.abandoned == [_H1]  # 在途收口
    assert loop.stopped and client.closed


def test_http_stub_started_when_listen_set(monkeypatch, tmp_path) -> None:
    """--listen 非空时 HTTP 占位启动，退出时停收。"""
    backend = FakeBackend()
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)

    args = _args(tmp_path)
    args.listen = "127.0.0.1:0"  # ephemeral
    stop = threading.Event()
    t = threading.Thread(target=daemon.run, args=(args,), kwargs={"stop": stop})
    t.start()
    time.sleep(0.2)  # 起服务
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert client.closed
