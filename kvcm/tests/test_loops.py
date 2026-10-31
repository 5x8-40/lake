"""KeepaliveLoop 检查：固定窗口保活、到期注销、重启恢复、协程生命周期。

fake KvOps（离线可跑）：脚本化 exists。
"""

from __future__ import annotations

import asyncio
import tempfile
import time

import pytest

from conftest import run
from kvcm.ledger import Ledger
from kvcm.loops import KeepaliveLoop
from kvcm.ops.base import KvOps, QueryError
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import Prefix

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))


class FakeOps(KvOps):
    def __init__(self, *, present: bool = True, error: Exception | None = None):
        self._present = present
        self._error = error
        self.calls: list[Prefix] = []

    def capability(self) -> BackendCapability:
        raise NotImplementedError

    async def exists(self, prefix) -> bool:
        self.calls.append(prefix)
        if self._error is not None:
            raise self._error
        return self._present

    # KeepaliveLoop 不触及以下操作；抽象方法必须实现（abc 要求）
    async def copy(self, prefix, target):
        raise NotImplementedError

    async def query_task(self, handle):
        raise NotImplementedError

    async def abandon(self, handle):
        raise NotImplementedError


def _ledger():
    f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    path = f.name
    f.close()
    return Ledger(path), path


def test_register_then_sweep_renews_via_exists() -> None:
    led, path = _ledger()
    try:
        ops = FakeOps(present=True)
        loop = KeepaliveLoop(ops, led, interval_s=999, window_s=600)
        loop.register(_P)
        run(loop.sweep_once())
        assert ops.calls == [_P]  # 一轮续租 = 一次 exists（读即续租）
        assert led.list_keepalive() == [_P]  # 仍在保
        loop.close()
    finally:
        led.close()


def test_window_expiry_unregisters() -> None:
    """窗口到：停租注销，不再调 exists（保活必须有界）。"""
    led, path = _ledger()
    try:
        ops = FakeOps(present=True)
        loop = KeepaliveLoop(ops, led, interval_s=999, window_s=10)
        loop.register(_P)
        led._backdate_keepalive(seconds=11)  # 拨到窗口外
        run(loop.sweep_once())
        assert ops.calls == []  # 到期行不再续租
        assert led.list_keepalive() == []
        loop.close()
    finally:
        led.close()


def test_present_false_unregisters() -> None:
    """exists 返回 False（副本已不在池）：注销——续租没有对象。"""
    led, path = _ledger()
    try:
        loop = KeepaliveLoop(FakeOps(present=False), led, interval_s=999, window_s=600)
        loop.register(_P)
        run(loop.sweep_once())
        assert led.list_keepalive() == []
        loop.close()
    finally:
        led.close()


def test_query_error_skips_round_keeps_registration() -> None:
    """查询失败跳过本轮，注册保留（lease TTL 内有多个周期容错）。"""
    led, path = _ledger()
    try:
        loop = KeepaliveLoop(
            FakeOps(error=QueryError("rpc boom")), led, interval_s=999, window_s=600
        )
        loop.register(_P)
        run(loop.sweep_once())
        assert led.list_keepalive() == [_P]  # 仍保着，下轮重试
        loop.close()
    finally:
        led.close()


def test_reregister_keeps_registered_at() -> None:
    """再次登记不把窗口拨回现在。"""
    led, path = _ledger()
    try:
        led.register_keepalive(_P)
        first = led.list_keepalive_records()[0].registered_at
        time.sleep(0.02)
        led.register_keepalive(_P)
        rows = led.list_keepalive_records()
        assert len(rows) == 1
        assert rows[0].registered_at == first
    finally:
        led.close()
        import os

        if os.path.exists(path):
            os.unlink(path)


def test_restart_recovery_needs_no_special_casing() -> None:
    """重启：注册行在 Ledger 里，新 Loop 首扫自然接管（窗口按原
    registered_at 连续起算）。"""
    led, path = _ledger()
    try:
        KeepaliveLoop(FakeOps(), led, interval_s=999, window_s=600).register(_P)
        led.close()

        led2 = Ledger(path)
        try:
            ops2 = FakeOps(present=True)
            loop2 = KeepaliveLoop(ops2, led2, interval_s=999, window_s=600)
            run(loop2.sweep_once())  # 不需要重新注册
            assert ops2.calls == [_P]
            loop2.close()
        finally:
            led2.close()
    finally:
        import os

        if os.path.exists(path):
            os.unlink(path)


def test_start_stop_lifecycle() -> None:
    """start 起协程定时器，stop 停掉；stop 不撤销注册（账本行保留）。"""

    async def scenario():
        led, path = _ledger()
        try:
            ops = FakeOps(present=True)
            loop = KeepaliveLoop(ops, led, interval_s=0.02, window_s=600)
            loop.register(_P)
            loop.start()
            await asyncio.sleep(0.07)  # 让定时器跑几轮
            assert len(ops.calls) >= 1
            await loop.stop()
            assert led.list_keepalive() == [_P]  # 注册不撤销
            loop.close()
        finally:
            led.close()
            import os

            if os.path.exists(path):
                os.unlink(path)

    run(scenario())


def test_sweep_exception_does_not_kill_loop() -> None:
    """单轮失败不该杀掉循环（下一轮继续）。"""

    class ExplodingOps(FakeOps):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def exists(self, prefix):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("unexpected boom")
            self.calls.append(prefix)
            return True

    async def scenario():
        led, path = _ledger()
        try:
            ops = ExplodingOps()
            loop = KeepaliveLoop(ops, led, interval_s=0.02, window_s=600)
            loop.register(_P)
            loop.start()
            await asyncio.sleep(0.12)
            await loop.stop()
            assert ops.n >= 2, f"循环在第一轮异常后就停了: n={ops.n}"
            loop.close()
        finally:
            led.close()
            import os

            if os.path.exists(path):
                os.unlink(path)

    run(scenario())


def test_sweep_failure_is_logged(capsys) -> None:
    """续租失败必须留痕。

    静默的后果：每轮都抛的故障（master 不可达）会让租约到期、副本提前变冷，
    而外部看不到任何信号。原实现 `except Exception: continue` 什么都不留。
    """

    class ExplodingOps(FakeOps):
        async def exists(self, prefix):
            raise RuntimeError("backend gone")

    async def scenario():
        led, path = _ledger()
        try:
            loop = KeepaliveLoop(ExplodingOps(), led, interval_s=0.02, window_s=600)
            loop.register(_P)
            loop.start()
            await asyncio.sleep(0.1)
            await loop.stop()
            loop.close()
        finally:
            led.close()
            import os

            if os.path.exists(path):
                os.unlink(path)

    run(scenario())
    err = capsys.readouterr().err
    assert "keepalive sweep failed" in err, f"失败没打日志: {err!r}"
    assert "backend gone" in err, f"日志里没有根因: {err!r}"


def test_exists_query_error_is_logged(capsys) -> None:
    """`exists` 抛 QueryError（RPC 失败）同样要留痕，不能静默跳过。"""

    class QueryFailingOps(FakeOps):
        async def exists(self, prefix):
            raise QueryError("rpc timeout")

    async def scenario():
        led, path = _ledger()
        try:
            loop = KeepaliveLoop(QueryFailingOps(), led, interval_s=0.02, window_s=600)
            loop.register(_P)
            loop.start()
            await asyncio.sleep(0.1)
            await loop.stop()
            loop.close()
        finally:
            led.close()
            import os

            if os.path.exists(path):
                os.unlink(path)

    run(scenario())
    err = capsys.readouterr().err
    assert "keepalive exists failed" in err, f"QueryError 没打日志: {err!r}"


def test_interval_and_window_must_be_positive() -> None:
    led, _ = _ledger()
    try:
        with pytest.raises(ValueError):
            KeepaliveLoop(FakeOps(), led, interval_s=0)
        with pytest.raises(ValueError):
            KeepaliveLoop(FakeOps(), led, window_s=0)
    finally:
        led.close()


# ---------------------------------------------------------------------------
# 投递健壮性（grok 建议三条）
# ---------------------------------------------------------------------------


def test_register_before_start_does_not_block_caller() -> None:
    """事件循环未起时 register 不在调用线程跑 sqlite（交给 executor）。

    用**慢 Ledger** 锁住：若 register 同步执行 sqlite，它会阻塞到写完
    （≥0.3s）；异步投递则立即返回（<0.1s）。
    """
    import time as _t

    class SlowLedger:
        """register_keepalive 故意慢 0.3 秒。"""

        def __init__(self, real) -> None:
            self._real = real
            self.written = 0

        def register_keepalive(self, prefix) -> None:
            _t.sleep(0.3)
            self._real.register_keepalive(prefix)
            self.written += 1

        def unregister_keepalive(self, prefix) -> None:
            self._real.unregister_keepalive(prefix)

        def list_keepalive_records(self):
            return self._real.list_keepalive_records()

        @property
        def executor(self):
            return self._real.executor

    led, path = _ledger()
    try:
        slow = SlowLedger(led)
        loop = KeepaliveLoop(FakeOps(), slow, interval_s=999, window_s=600)
        # 未 start()：_loop 为 None
        start_t = _t.monotonic()
        loop.register(_P)
        elapsed = _t.monotonic() - start_t
        # 关键：必须立即返回（若同步跑 sqlite，会卡 0.3s）
        assert elapsed < 0.1, f"register 阻塞了 {elapsed:.2f}s（在调用线程跑了 sqlite）"
        assert slow.written == 0, "register 返回时写入不该已完成（应异步）"
        # 等投递生效
        for _ in range(100):
            if slow.written:
                break
            _t.sleep(0.01)
        assert slow.written == 1, "慢 Ledger 的写入最终没发生"
        loop.close()
    finally:
        led.close()
        import os

        if os.path.exists(path):
            os.unlink(path)


def test_register_does_not_raise_when_loop_closed() -> None:
    """事件循环已关闭时 register **不抛异常**（否则已 READY 的 intent 会被
    误记成 FAILED）。

    已关闭的循环 `is_running()` 为假 → 走「直接交 executor」分支，写入仍
    能成功——这是对的：此时不该因为事件循环死了就丢保活。
    """

    led, path = _ledger()
    try:
        loop_obj = KeepaliveLoop(FakeOps(), led, interval_s=999, window_s=600)

        # 起一个真循环、记下它、然后关掉——模拟「循环已死但 register 仍被调」
        import asyncio as _aio

        async def _start_then_stop():
            loop_obj.start()
            return _aio.get_running_loop()

        closed_loop = run(_start_then_stop())
        closed_loop.close()  # 真的关掉

        # 现在 register：闭掉循环上如果直接 call_soon_threadsafe 会抛
        # RuntimeError。断言它不抛（能吞掉并走 executor 分支）。
        loop_obj.register(_P)  # 不应抛
        # 写入仍应成功（不因循环死了就丢保活）
        for _ in range(100):
            if led.list_keepalive():
                break
            import time as _t

            _t.sleep(0.01)
        assert led.list_keepalive() == [_P], "循环关闭后保活注册丢了"
        assert loop_obj.register_failures == 0, f"不该计失败: {loop_obj.register_failures}"
        loop_obj.close()
    finally:
        led.close()
        import os

        if os.path.exists(path):
            os.unlink(path)


def test_register_failure_is_counted_and_logged(capsys) -> None:
    """Ledger 写入失败：计数 +1、不抛给调用方、并且打日志。

    计数 `register_failures` 在生产路径没有读者；不打日志的话外部完全看不到
    ——而这条前缀**没有**保活行，副本会按租约自然变冷。
    """

    async def scenario():
        led, path = _ledger()
        try:
            class BrokenLedger(type(led)):
                def register_keepalive(self, prefix):
                    raise RuntimeError("db gone")

            broken = BrokenLedger.__new__(BrokenLedger)
            broken.__dict__.update(led.__dict__)
            loop = KeepaliveLoop(FakeOps(), broken, interval_s=999, window_s=600)
            loop.start()
            loop.register(_P)  # 不抛
            for _ in range(100):
                if loop.register_failures > 0:
                    break
                await asyncio.sleep(0.01)
            assert loop.register_failures >= 1, "失败没有被计数"
            loop.close()
        finally:
            led.close()
            import os

            if os.path.exists(path):
                os.unlink(path)

    run(scenario())
    err = capsys.readouterr().err
    assert "keepalive register failed" in err, f"没打日志: {err!r}"
    assert "db gone" in err, f"日志没根因: {err!r}"
