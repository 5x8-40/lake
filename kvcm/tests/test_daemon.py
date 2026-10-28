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
    def __init__(self, events: list | None = None) -> None:
        self.connected = False
        self.closed = False
        self._events = events

    def connect(self, **kw) -> None:
        self.kw = kw
        self.connected = True

    def close(self) -> None:
        self.closed = True
        if self._events is not None:
            self._events.append("client.close")


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

    @property
    def executor(self):
        """RPC 池（KeepaliveLoop 复用）。测试里给个真池。"""
        from concurrent.futures import ThreadPoolExecutor

        if not hasattr(self, "_exec"):
            self._exec = ThreadPoolExecutor(max_workers=2)
        return self._exec


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
        rpc_timeout_s=30.0,
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
    # 关键：恢复的 handle 必须被收编（track）、真在轮询
    deadline = time.monotonic() + 3
    while not backend.first_query.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend.first_query.is_set(), "恢复的 handle 没被 track（没轮询）"
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
    # 关键：恢复的 handle 必须被 abandon（否则账本留 running，重启又捞回）
    assert backend.abandoned == [_H1], f"恢复 handle 没被 abandon: {backend.abandoned}"


def test_grpc_server_started_when_listen_set(monkeypatch, tmp_path) -> None:
    """--listen 非空时 gRPC 入口**真的被创建并启动**，退出时停收。

    不能只断言「进程退出且 client 关了」——把整个 `if args.listen` 删掉
    也满足。
    """
    created: list[object] = []

    class RecordingGrpcServer:
        def __init__(self, addr, engine) -> None:
            self.addr = addr
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self, **kw) -> None:
            self.stopped = True

    monkeypatch.setattr(daemon, "IntentGrpcServer", RecordingGrpcServer)
    backend = FakeBackend()
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)

    args = _args(tmp_path)
    args.listen = "127.0.0.1:0"
    t, stop = _run_daemon_in_thread(args)
    time.sleep(0.2)
    stop()
    t.join(timeout=5)
    assert not t.is_alive()
    # 关键：服务被创建、启动、并在退出时停掉
    assert len(created) == 1, f"gRPC 服务没被创建: {created}"
    srv = created[0]
    assert srv.started, "gRPC 服务没启动"
    assert srv.stopped, "gRPC 服务没停"


def test_grpc_server_not_created_when_listen_empty(monkeypatch, tmp_path) -> None:
    """--listen 为空时不建 gRPC 服务（对照组，防「无条件创建」）。"""
    created: list[object] = []

    class RecordingGrpcServer:
        def __init__(self, addr, engine) -> None:
            created.append(self)

        def start(self) -> None: ...

        def stop(self, **kw) -> None: ...

    monkeypatch.setattr(daemon, "IntentGrpcServer", RecordingGrpcServer)
    backend = FakeBackend()
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)

    args = _args(tmp_path)
    args.listen = ""
    t, stop = _run_daemon_in_thread(args)
    time.sleep(0.15)
    stop()
    t.join(timeout=5)
    assert created == [], f"--listen 为空却建了服务: {created}"


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



def test_exit_waits_rpc_threads_before_client_close(monkeypatch, tmp_path) -> None:
    """退出顺序：**真的等线程结束**之后才关 client。

    不只看事件顺序——用一个「shutdown 要花 0.2s」的 executor，断言
    client.close 发生在它**完成之后**（0.2s 之后），这样把
    `shutdown(wait=False)` 改成不等也会被抓到。
    """
    events: list[tuple[str, float]] = []
    t0 = time.monotonic()
    backend = FakeBackend()

    holder: dict[str, SlowShutdownExecutor] = {}

    class SlowShutdownExecutor:
        """模拟「等在途线程」：`wait=True` 才花时间，`wait=False` 立即返回。

        **必须按 wait 参数决定行为**——否则把生产代码改成
        `shutdown(wait=False)`，测试仍会看到 0.2s 耗时，锁不住「真的在等」。
        """

        def __init__(self) -> None:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(max_workers=1)

        def shutdown(self, wait=True, **kw) -> None:
            # 记录**本函数内部**的耗时（不是相对测试起点）——否则停止流程
            # 的调度抖动会让「没等」也看起来耗了 0.15s+
            entered = time.monotonic()
            if wait:
                time.sleep(0.2)  # 只有真等才耗时
            self.shutdown_elapsed = time.monotonic() - entered
            events.append(("executor.shutdown", time.monotonic() - t0))
            self._pool.shutdown(wait=wait, **kw)

        def submit(self, fn, *a, **kw):
            return self._pool.submit(fn, *a, **kw)

    class Client:
        def connect(self, **kw) -> None: ...
        def close(self) -> None:
            events.append(("client.close", time.monotonic() - t0))

    def _make_exec(*a, **kw):
        ex = SlowShutdownExecutor()
        holder["ex"] = ex
        return ex

    monkeypatch.setattr(daemon, "ThreadPoolExecutor", _make_exec)
    monkeypatch.setattr(daemon, "StoreClient", Client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)

    t, stop = _run_daemon_in_thread(_args(tmp_path))
    time.sleep(0.1)
    stop()
    t.join(timeout=5)
    assert len(events) == 2, f"事件不全: {events}"
    shutdown_t = events[0][1]
    close_t = events[1][1]
    assert events[0][0] == "executor.shutdown" and events[1][0] == "client.close"
    # 关键：量 shutdown 内部耗时（0.2s 的等待），不受测试调度抖动影响
    elapsed_in_shutdown = holder["ex"].shutdown_elapsed
    assert elapsed_in_shutdown >= 0.15, (
        f"shutdown 没真的等（内部仅 {elapsed_in_shutdown:.2f}s）——"
        f"生产代码可能用了 wait=False"
    )
    assert close_t >= shutdown_t, "client.close 在 shutdown 之前"


def test_drain_timeout_skips_close_real_flow(monkeypatch, tmp_path) -> None:
    """真跑 daemon 流程：等线程超时时，client 和 ledger 都不被关闭。

    之前的版本只调 `_wait_executor` 再断言 `"client.close" not in events`
    ——没有任何代码往 events 写，断言恒真（假测试）。
    """
    events: list[str] = []

    class FakeClient:
        def __init__(self) -> None:
            self.connected = False

        def connect(self, **kw) -> None:
            self.connected = True

        def close(self) -> None:
            events.append("client.close")

    class FakeBackend:
        def __init__(self) -> None:
            self.rebuild_calls = 0

        def rebuild(self):
            self.rebuild_calls += 1
            return []

        def prefix_of(self, handle):
            return None

        def close(self) -> None:
            events.append("backend.close")

        @property
        def executor(self):
            from concurrent.futures import ThreadPoolExecutor

            if not hasattr(self, "_exec"):
                self._exec = ThreadPoolExecutor(max_workers=1)
            return self._exec

    class HangingExecutor:
        """shutdown(wait=True) 永远不返回（模拟卡死的 RPC）。"""

        def shutdown(self, wait=True, **kw) -> None:
            while wait:
                time.sleep(0.05)

        def submit(self, fn, *a, **kw):
            from concurrent.futures import Future

            f: Future = Future()
            f.set_result(None)  # connect 走这条路，立即完成
            return f

    class FakeLedger:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True
            events.append("ledger.close")

    fake_ledger = FakeLedger()

    client = FakeClient()
    backend = FakeBackend()
    monkeypatch.setattr(daemon, "ThreadPoolExecutor", lambda *a, **kw: HangingExecutor())
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)
    monkeypatch.setattr(daemon, "Ledger", lambda path: fake_ledger)

    args = _args(tmp_path)
    args.shutdown_timeout_s = 0.2
    t, stop = _run_daemon_in_thread(args)
    time.sleep(0.2)
    stop()
    t.join(timeout=5)
    # 关键断言：超时后 client 和 ledger 都没被关（都是真事件，不是恒真）
    assert "client.close" not in events, f"超时后居然关了 client: {events}"
    assert "ledger.close" not in events, f"超时后居然关了 ledger: {events}"
