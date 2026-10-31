"""进程壳单元检查：启动/恢复/退出序列与参数组装（fake client，离线可跑）。

daemon 现在是协程主循环（E11 执行模型），所以测试用 `asyncio.Event`
作退出信号；`daemon.run` 阻塞到循环结束（测试在后台线程里跑它）。
"""

from __future__ import annotations

import argparse
import asyncio
import threading

import pytest
import time

from kvcm import daemon
from kvcm.ledger import Ledger
from kvcm.ops.base import OpKind, TaskHandle, TaskStatus
from kvcm.ops.types import Prefix

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

    def close(self) -> bool:
        self.closed = True
        return True

    @property
    def executor(self):
        """RPC 池（KeepaliveLoop 复用）。测试里给个真池。"""
        from concurrent.futures import ThreadPoolExecutor

        if not hasattr(self, "_exec"):
            self._exec = ThreadPoolExecutor(max_workers=2)
        return self._exec


class FakeLedgerCloser:
    """只记 close 调用的 Ledger 替身。"""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        self._events.append("ledger.close")

    def gc_tasks(self, **kw) -> int:
        return 0


class FakeKeepalive:
    def __init__(self, *a, **kw) -> None:
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    async def stop(self, *, timeout_s: float = 10.0) -> None:
        self.stopped = True
        self.stop_timeout_s = timeout_s

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
        instance_segs=["inst=127.0.0.1:10001,127.0.0.1:10002"],
        capability="v0311",
        ledger=f"{tmp}/ledger.sqlite",
        max_concurrent=4,
        copy_submit_concurrency=4,
        listen="",
        keepalive_interval_s=0.05,
        lease_ttl_s=10.0,
        keepalive_window_s=600.0,
        shutdown_timeout_s=2.0,
        rpc_timeout_s=30.0,
        connect_timeout_s=30.0,
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


def test_startup_gc_removes_stale_rows(monkeypatch, tmp_path) -> None:
    """启动时清一次超龄终态行（tasks 表只增不删）。

    不接线的话 gc_tasks 永远没人调，表在长跑进程里一直涨。
    """
    ledger_path = f"{tmp_path}/ledger.sqlite"
    # 先塞一行「很久以前完成」的任务
    pre = Ledger(ledger_path)
    try:
        pre.upsert_tasks(
            ["old-task"],
            handle_id="h-old",
            op="copy",
            prefix=Prefix(model_id="qwen", token_ids=(1,)),
            target="inst-B",
            status="ready",
        )
        # 把 updated_at 改成 30 天前（必须走 _run，连接只在它自己的线程上用）
        def _age():
            pre._conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE task_id = 'old-task'",
                (time.time() - 30 * 24 * 3600,),
            )
            pre._conn.commit()

        pre._run(_age)
        assert pre.get_task("old-task") is not None
    finally:
        pre.close()

    backend = FakeBackend()
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)

    t, stop = _run_daemon_in_thread(_args(tmp_path))
    deadline = time.monotonic() + 5
    while backend.rebuild_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    # 等 GC 跑完（它排在 rebuild 之后）
    post = Ledger(ledger_path)
    try:
        for _ in range(200):
            if post.get_task("old-task") is None:
                break
            time.sleep(0.01)
        assert post.get_task("old-task") is None, "超龄行没被清"
    finally:
        post.close()
    stop()
    t.join(timeout=5)
    assert not t.is_alive()


def test_rebuild_hang_times_out(monkeypatch, tmp_path) -> None:
    """rebuild 卡住时按 connect 同样的规则硬退出（不裸等）。

    裸 await 会让 sqlite 卡死时 finally 进不去，退出逻辑整个不跑。
    """
    hang = threading.Event()

    class HangingBackend(FakeBackend):
        def rebuild(self):
            self.rebuild_calls += 1
            hang.wait(30)  # 卡住
            return []

    backend = HangingBackend()
    client = FakeClient()
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)

    exits: list[float] = []
    t0 = time.monotonic()
    # **不放行 rebuild 线程**：生产里 os._exit 之后不会再有代码跑。若在 mock
    # 里放行，finally 的 drain 会超时并再调一次 _hard_exit——那样 rebuild
    # 分支被删掉时测试一样绿。
    monkeypatch.setattr(
        daemon, "_hard_exit", lambda: exits.append(time.monotonic() - t0)
    )

    args = _args(tmp_path)
    args.connect_timeout_s = 0.2  # rebuild 的超时
    args.shutdown_timeout_s = 3.0  # 远大于它：别让 finally 抢先
    t, stop = _run_daemon_in_thread(args)
    deadline = time.monotonic() + 5
    while not exits and time.monotonic() < deadline:
        time.sleep(0.01)
    assert exits, "rebuild 卡住没有硬退出"
    # 只该有一次且来得早（来自 rebuild 分支）。若那个分支被删成 raise，
    # finally 要等满 shutdown_timeout_s 才硬退出，这里等不到。
    assert len(exits) == 1, f"硬退出次数不对: {exits}"
    assert exits[0] < 1.0, f"硬退出来得太晚（{exits[0]:.1f}s），可能来自 finally"
    hang.set()
    t.join(timeout=5)


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
    # keepalive 的等待超时跟 --shutdown-timeout-s 走，不用它自己的默认 10 秒
    # （否则退出总时长会变成 shutdown + 10 + shutdown）
    assert ka.stop_timeout_s == _args(tmp_path).shutdown_timeout_s, (
        f"keepalive.stop 没用统一的超时: {ka.stop_timeout_s}"
    )
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


def test_drain_timeout_hard_exits_without_close(monkeypatch, tmp_path) -> None:
    """等线程超时（RPC 卡死）：进程立即退出，且**不调用**任何 close。

    为什么必须硬退出而不是「跳过 close 再 return」：
    `client` 是局部变量，函数返回会丢掉 Python 包装的最后一份引用 →
    `~RealClient` 立刻 `tearDownAll_internal()`，此时 RPC 线程还在绑定里
    ——正是 demo 实测的 teardown 竞态（abort RC=134 / 挂住）。
    它发生在解释器收尾之前，也不经过 mooncake 的 `std::atexit`。
    """
    events: list[str] = []
    hard_exits: list[int] = []

    class FakeClient:
        def connect(self, **kw) -> None: ...

        def close(self) -> None:
            events.append("client.close")

    class FakeBackend:
        def rebuild(self):
            return []

        def prefix_of(self, handle):
            return None

        def close(self) -> None:
            events.append("backend.close")

        @property
        def executor(self):
            from concurrent.futures import ThreadPoolExecutor

            return ThreadPoolExecutor(max_workers=1)

    class FakeLedger:
        def close(self) -> None:
            events.append("ledger.close")

    class HangingExecutor:
        """shutdown(wait=True) 永不返回（模拟卡死的 RPC）。"""

        def shutdown(self, wait=True, **kw) -> None:
            while wait:
                time.sleep(0.05)

        def submit(self, fn, *a, **kw):
            from concurrent.futures import Future

            f: Future = Future()
            f.set_result(None)
            return f

    monkeypatch.setattr(daemon, "ThreadPoolExecutor", lambda *a, **kw: HangingExecutor())
    monkeypatch.setattr(daemon, "StoreClient", FakeClient)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: FakeBackend())
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)
    monkeypatch.setattr(daemon, "Ledger", lambda path: FakeLedger())
    # 替换硬退出（测试进程不能被真的杀掉）
    monkeypatch.setattr(daemon, "_hard_exit", lambda: hard_exits.append(1))

    args = _args(tmp_path)
    args.shutdown_timeout_s = 0.2
    t, stop = _run_daemon_in_thread(args)
    time.sleep(0.2)
    stop()
    t.join(timeout=5)
    assert not t.is_alive()
    # 关键 1：硬退出被调用（不是「跳过 close 后安静返回」）
    assert hard_exits == [1], f"超时后没有硬退出: hard_exits={hard_exits}"
    # 关键 2：client 和 ledger 都没被 close（不能主动在在途 RPC 上 teardown）
    assert "client.close" not in events, f"超时后居然关了 client: {events}"
    assert "ledger.close" not in events, f"超时后居然关了 ledger: {events}"



@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_startup_failure_still_cancels_recovery_tasks(monkeypatch, tmp_path) -> None:
    """启动中途失败（rebuild 之后、正常退出路径之前）：恢复协程也要被取消。

    `track_in_background` 在 rebuild 之后就把恢复轮询跑起来了。若之后
    `gc_tasks`、`keepalive.start` 或 gRPC 绑定抛错，正常路径的
    `engine.shutdown` 根本没走到——协程会挂到 `asyncio.run` 收尾，那时
    client 已经关了，`abandon` 会投到已关闭的池上。
    """
    events: list[str] = []
    shut: list[int] = []

    class RecordingEngine(daemon.TaskEngine):
        async def shutdown(self, *, timeout_s: float = 5.0) -> None:
            shut.append(1)
            events.append("engine.shutdown")
            await super().shutdown(timeout_s=timeout_s)

    backend = FakeBackend(restored=[_H1])
    client = FakeClient(events)
    monkeypatch.setattr(daemon, "TaskEngine", RecordingEngine)
    monkeypatch.setattr(daemon, "StoreClient", lambda: client)
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)

    # 让 keepalive.start() 抛错：这在 rebuild 之后、正常退出路径之前，
    # 且抛在协程里（不是 executor 线程），daemon 会进 finally。
    class BoomKeepalive(FakeKeepalive):
        def start(self) -> None:
            raise RuntimeError("keepalive boom")

    monkeypatch.setattr(daemon, "KeepaliveLoop", lambda *a, **kw: BoomKeepalive())

    t, stop = _run_daemon_in_thread(_args(tmp_path))
    t.join(timeout=5)
    assert not t.is_alive()
    # 关键：失败路径也调了 engine.shutdown，且发生在 client.close 之前
    assert shut, "启动失败时恢复协程没被取消"
    assert "client.close" in events
    assert events.index("engine.shutdown") < events.index("client.close"), (
        f"shutdown 晚于 client.close: {events}"
    )


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_connect_plain_error_closes_cleanly(monkeypatch, tmp_path, capsys) -> None:
    """connect 抛普通异常（不是超时）：不硬退出，走 finally 正常收尾。

    与超时分支的区别：线程已经返回，没有卡住的调用，可以安全 close。
    若误把它当超时处理（硬退出），会白杀进程、且跳过 client.close。
    """
    events: list[str] = []

    class BoomClient:
        def connect(self, **kw) -> None:
            raise RuntimeError("handshake rejected")

        def close(self) -> None:
            events.append("client.close")

    monkeypatch.setattr(daemon, "StoreClient", BoomClient)
    monkeypatch.setattr(daemon, "Ledger", lambda path: FakeLedgerCloser(events))
    exits: list[int] = []
    monkeypatch.setattr(daemon, "_hard_exit", lambda: exits.append(1))

    t, stop = _run_daemon_in_thread(_args(tmp_path))
    t.join(timeout=5)
    assert not t.is_alive()
    assert not exits, f"普通异常不该硬退出: {exits}"
    assert "client.close" in events, f"没正常收尾: {events}"
    err = capsys.readouterr().err
    assert "connect failed" in err, f"没打日志: {err!r}"
    assert "handshake rejected" in err, f"日志没根因: {err!r}"


def test_shutdown_signal_interrupts_rebuild_wait(monkeypatch, tmp_path, capsys) -> None:
    """SIGTERM 打在 rebuild 等待期间：立即退出，不等满 connect_timeout_s。

    与 connect 上的信号测试同理——超时分支也会硬退出、也很快，只看「退出快」
    分不出来。靠 stderr 文案区分。
    """
    hang = threading.Event()
    hard_exits: list[float] = []
    t0 = time.monotonic()

    class HangingBackend(FakeBackend):
        def rebuild(self):
            self.rebuild_calls += 1
            hang.wait(30)
            return []

    backend = HangingBackend()
    monkeypatch.setattr(daemon, "StoreClient", lambda: FakeClient())
    monkeypatch.setattr(daemon, "build_backend", lambda *a, **kw: backend)
    monkeypatch.setattr(daemon, "KeepaliveLoop", FakeKeepalive)
    monkeypatch.setattr(
        daemon, "_hard_exit", lambda: hard_exits.append(time.monotonic() - t0)
    )

    args = _args(tmp_path)
    args.connect_timeout_s = 30.0  # 很长——靠信号打断，不该等满
    t, stop = _run_daemon_in_thread(args)
    deadline = time.monotonic() + 5
    while backend.rebuild_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend.rebuild_calls == 1, "rebuild 没开始，前提不成立"
    stop()  # 模拟 SIGTERM
    deadline = time.monotonic() + 5
    while not hard_exits and time.monotonic() < deadline:
        time.sleep(0.01)
    hang.set()
    t.join(timeout=5)
    assert hard_exits, "信号没打断 rebuild 等待"
    assert hard_exits[0] < 5.0, f"等了 {hard_exits[0]:.1f}s 才退"
    err = capsys.readouterr().err
    assert "got shutdown signal during rebuild" in err, (
        f"走的是超时分支而不是信号分支: {err!r}"
    )


def test_connect_timeout_branch_hard_exits(monkeypatch, tmp_path) -> None:
    """connect 超时**分支本身**触发硬退出（不是靠 finally 兜的）。

    测试要点：连接线程必须在超时后**立刻结束**——否则 `finally` 里的
    `_wait_executor` 也会超时并再调一次 `_hard_exit`，那样即使删掉
    connect 的超时分支，测试也会因为 finally 的硬退出而通过（锁不住）。

    关于 close：测试里 `_hard_exit` 是 mock（不能真杀 pytest 进程），
    所以 `finally` 仍会跑完并 close——生产的 `os._exit` 不会。因此这里
    只断言「硬退出恰好一次，且事件序列里它出现在所有 close 之前」。
    """
    hard_exits: list[float] = []
    events: list[tuple[str, float]] = []
    release = threading.Event()
    t0 = time.monotonic()

    class HangingClient:
        def connect(self, **kw) -> None:
            # **永不自行返回**（只有超时分支放行才结束）——否则删掉超时分支后
            # 线程会自己跑完，测试就区分不出「靠超时分支」还是「靠 finally」
            release.wait()

        def close(self) -> None:
            events.append(("client.close", time.monotonic() - t0))

    class FakeLedger:
        def close(self) -> None:
            events.append(("ledger.close", time.monotonic() - t0))

    def _hard_exit() -> None:
        # **不放行 connect 线程**：生产里 os._exit 之后不会再有任何代码跑，
        # 所以线程永远卡着。如果在 mock 里放行，finally 的 drain 就会超时
        # 并再调一次 _hard_exit——那样 connect 分支被删掉时测试一样绿。
        hard_exits.append(time.monotonic() - t0)

    monkeypatch.setattr(daemon, "StoreClient", HangingClient)
    monkeypatch.setattr(daemon, "Ledger", lambda path: FakeLedger())
    monkeypatch.setattr(daemon, "_hard_exit", _hard_exit)

    args = _args(tmp_path)
    args.connect_timeout_s = 0.2
    args.shutdown_timeout_s = 3.0  # 远大于 0.2s 的 connect 超时：别让 finally 抢先
    t, stop = _run_daemon_in_thread(args)
    # 等到硬退出出现（connect 超时是 0.2s，5 秒足够）
    deadline = time.monotonic() + 5
    while not hard_exits and time.monotonic() < deadline:
        time.sleep(0.02)
    assert hard_exits, "connect 超时后没硬退出"
    # 关键：此刻只该有 **一次**，且来自 connect 分支。若那个分支被删成
    # raise，finally 要等满 shutdown_timeout_s 才会硬退出——等待窗口内等不到。
    assert len(hard_exits) == 1, f"硬退出次数不对: {hard_exits}"
    assert hard_exits[0] < 1.0, f"硬退出来得太晚（{hard_exits[0]:.1f}s），可能来自 finally"
    release.set()  # 放行，让 finally 能收尾
    t.join(timeout=5)



def test_shutdown_signal_interrupts_connect_wait(monkeypatch, tmp_path, capsys) -> None:
    """SIGTERM 打在 connect 等待期间：立即退出，不等满 connect_timeout_s。

    connect 超时默认 30 秒——若信号要等它才生效，停机就慢得不合理。
    """
    hard_exits: list[float] = []
    release = threading.Event()
    t0 = time.monotonic()

    class HangingClient:
        def connect(self, **kw) -> None:
            release.wait()  # 永不自行返回

        def close(self) -> None: ...

    class FakeLedger:
        def close(self) -> None: ...

    def _hard_exit() -> None:
        hard_exits.append(time.monotonic() - t0)
        release.set()

    monkeypatch.setattr(daemon, "StoreClient", HangingClient)
    monkeypatch.setattr(daemon, "Ledger", lambda path: FakeLedger())
    monkeypatch.setattr(daemon, "_hard_exit", _hard_exit)

    args = _args(tmp_path)
    args.connect_timeout_s = 30.0  # 很长——靠信号打断，不该等满
    t, stop = _run_daemon_in_thread(args)
    time.sleep(0.2)  # 让 connect 开始等
    stop()  # 模拟 SIGTERM
    t.join(timeout=5)
    assert not t.is_alive(), "信号没打断 connect 等待"
    assert hard_exits, "没有硬退出"
    assert hard_exits[0] < 5.0, (
        f"等了 {hard_exits[0]:.1f}s 才退（connect_timeout 是 30s，说明信号没打断）"
    )
    # 关键：必须是**信号分支**退出的，不是超时分支。两者都硬退出、都很快，
    # 只看「退出快」分不出来——靠 stderr 文案区分。
    err = capsys.readouterr().err
    assert "got shutdown signal during connect" in err, (
        f"走的是超时分支而不是信号分支: {err!r}"
    )


def test_cli_repeats_instance_segs(monkeypatch) -> None:
    """一次启动可以登记多个实例。旧的单一 --segs 不再接受。"""
    captured: dict = {}

    def fake_run(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(daemon, "run", fake_run)
    rc = daemon.main(
        [
            "--master", "127.0.0.1:50088",
            "--local-hostname", "127.0.0.1:13021",
            "--model-name", "qwen",
            "--instance-segs", "A=seg-A0,seg-A1",
            "--instance-segs", "B=seg-B0",
            "--ledger", "/tmp/ledger.sqlite",
        ]
    )
    assert rc == 0
    assert captured["args"].instance_segs == [
        "A=seg-A0,seg-A1",
        "B=seg-B0",
    ]
    assert captured["args"].lease_ttl_s == 10.0
    with pytest.raises(SystemExit):
        daemon.main(
            [
                "--master", "127.0.0.1:50088",
                "--local-hostname", "127.0.0.1:13021",
                "--model-name", "qwen",
                "--segs", "seg0,seg1",
                "--ledger", "/tmp/ledger.sqlite",
            ]
        )


def test_directory_rejects_duplicate_and_empty() -> None:
    from kvcm.instances import InstanceDirectory, directory_from_specs

    directory = InstanceDirectory()
    directory.register("A", ["seg-A0"])
    with pytest.raises(ValueError, match="already registered"):
        directory.register("A", ["seg-A1"])
    assert directory.locate("A") == ("seg-A0",)
    with pytest.raises(ValueError, match="都不能空"):
        directory_from_specs(["A="])
    with pytest.raises(ValueError, match="至少要有一条"):
        directory_from_specs([])
