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


def test_interval_and_window_must_be_positive() -> None:
    led, _ = _ledger()
    try:
        with pytest.raises(ValueError):
            KeepaliveLoop(FakeOps(), led, interval_s=0)
        with pytest.raises(ValueError):
            KeepaliveLoop(FakeOps(), led, window_s=0)
    finally:
        led.close()
