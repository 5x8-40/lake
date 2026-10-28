"""进程壳单元检查：启动/恢复/退出序列与参数组装（fake client，离线可跑）。

daemon 现在是协程主循环（E11 执行模型），所以测试用 `asyncio.Event`
作退出信号；`daemon.run` 阻塞到循环结束（测试在后台线程里跑它）。
"""

from __future__ import annotations

import argparse
import asyncio
import threading
import time

from kvcm import daemon
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
        self.closed = False

    def rebuild(self):
        self.rebuild_calls += 1
        return list(self._restored)

    def prefix_of(self, handle):
        return None

    async def query_task(self, handle):
        self.first_query.set()
        if handle in self.abandoned:
            raise KeyError("unknown task handle")  # abandon 后即遗忘
        return TaskStatus.RUNNING

    async def abandon(self, handle) -> None:
        self.abandoned.append(handle)

    def close(self) -> None:
        self.closed = True


class FakeKeepalive:
    def __init__(self, *a, **kw) -> None:
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def _args(tmp: str) -> argparse.Namespace:
    return argparse.Namespace(
        master="127.0.0.1:50088",
        local_hostname="127.0.0.1:13021",
        protocol="tcp",
        device=0,
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
        shutdown_timeout_s=2.0,
    )


def _run_daemon_in_thread(args):
    """在后台线程跑 daemon.run（它内部 asyncio.run 会阻塞），返回
    (thread, stop_fn)。stop_fn 通过 call_soon_threadsafe 置位 asyncio.Event。"""
    holder: dict = {}

    async def _main_with_stop():
        ev = asyncio.Event()
        holder["event"] = ev
        holder["loop"] = asyncio.get_running_loop()
        return await daemon._serve(args, ev)

    def _target():
        asyncio.run(_main_with_stop())

    t = threading.Thread(target=_target, daemon=True)
    t.start()

    def stop():
        deadline = time.monotonic() + 5
        while "loop" not in holder and time.monotonic() < deadline:
            time.sleep(0.01)
        loop = holder.get("loop")
        if loop is not None:
            loop.call_soon_threadsafe(holder["event"].set)

    return t, stop


def test_startup_rebuild_loop_shutdown_sequence(monkeypatch, tmp_path) -> None:
    """启动 = connect→backend→rebuild→track 收编→loop.start；退出 =
    stop→engine.shutdown→loop.stop→client.close，次序与调用齐全。"""
    backend = FakeBackend(restored=[_H1])
    events: list[str] = []

    class TrackRecordingEngine(daemon.TaskEngine):
        def __init__(self, backend, **kw) -> None:
            super().__init__(backend, **kw)
            self._record = events

        async def track(self, handle, **kw):
            self._record.append("track")
            return await super().track(handle, **kw)

    monkeypatch.setattr(daemon, "TaskEngine", TrackRecordingEngine)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)

    t, stop = _run_daemon_in_thread(_args(tmp_path))
    deadline = time.monotonic() + 5
    while backend.rebuild_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend.rebuild_calls == 1
    stop()
    t.join(timeout=5)
    assert not t.is_alive()
    assert client.connected and client.closed


def test_no_restored_tasks_still_clean_shutdown(monkeypatch, tmp_path) -> None:
    backend = FakeBackend()
    client = FakeClient()
    ka = FakeKeepalive()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", lambda *a, **kw: ka)

    t, stop = _run_daemon_in_thread(_args(tmp_path))
    deadline = time.monotonic() + 5
    while backend.rebuild_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop()
    t.join(timeout=5)
    assert backend.rebuild_calls == 1
    assert ka.started and ka.stopped and ka.closed
    assert client.closed
    assert backend.closed


def test_graceful_shutdown_abandons_in_flight(monkeypatch, tmp_path) -> None:
    """优雅退出回归：退出信号到达时恢复跟踪仍在轮询（RUNNING），
    engine.shutdown 必须在 loop.stop 前把在途 handle abandon 掉。

    实现在 TaskEngine.shutdown：取消消费者 → 有界等待；被取消的 track
    协程收到 CancelledError。这里断言「恢复的 handle 不再被跟踪」
    （daemon 把 track 作为独立 task 起，取消后不再轮询）。
    """
    backend = FakeBackend(restored=[_H1])
    client = FakeClient()
    ka = FakeKeepalive()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", lambda *a, **kw: ka)

    t, stop = _run_daemon_in_thread(_args(tmp_path))
    assert backend.first_query.wait(timeout=5)  # track 已在轮询
    stop()
    t.join(timeout=5)
    assert not t.is_alive()
    assert ka.stopped and client.closed


def test_grpc_server_started_when_listen_set(monkeypatch, tmp_path) -> None:
    """--listen 非空时 gRPC 入口启动，退出时停收。"""
    backend = FakeBackend()
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)

    args = _args(tmp_path)
    args.listen = "127.0.0.1:0"  # ephemeral
    t, stop = _run_daemon_in_thread(args)
    time.sleep(0.3)  # 起服务
    stop()
    t.join(timeout=5)
    assert not t.is_alive()
    assert client.closed


def test_shutdown_timeout_param_passed(monkeypatch, tmp_path) -> None:
    """--shutdown-timeout-s 传给 engine.shutdown（有界等待的上限）。"""
    seen: dict = {}
    backend = FakeBackend()
    client = FakeClient()

    class RecordingEngine(daemon.TaskEngine):
        async def shutdown(self, *, timeout_s: float = 5.0) -> None:
            seen["timeout_s"] = timeout_s
            return await super().shutdown(timeout_s=timeout_s)

    monkeypatch.setattr(daemon, "TaskEngine", RecordingEngine)
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)

    args = _args(tmp_path)
    args.shutdown_timeout_s = 1.5
    t, stop = _run_daemon_in_thread(args)
    time.sleep(0.15)
    stop()
    t.join(timeout=5)
    assert seen.get("timeout_s") == 1.5
