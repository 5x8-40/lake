"""KeepaliveLoop 单元检查（fake ops 注入，离线可跑）。

关键锁定：固定窗口占位策略——续租经 ops.exists（读即续租 D1 F15）、
窗口按 registered_at 起算到期注销、exists False 即注销、QueryError
跳过本轮、重启恢复免特判（注册行在 Ledger 即接管）。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time

from kvcm.ledger import Ledger
from kvcm.loops import KeepaliveLoop
from kvcm.ops.base import QueryError
from kvcm.ops.types import Prefix

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))


class FakeOps:
    """脚本化 exists：calls 记录调用，script 控制返回/抛错。"""

    def __init__(self, *, present: bool = True, error: Exception | None = None):
        self.present = present
        self.error = error
        self.calls: list[Prefix] = []

    def exists(self, prefix: Prefix) -> bool:
        self.calls.append(prefix)
        if self.error is not None:
            raise self.error
        return self.present


def _ledger():
    f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    f.close()
    return Ledger(f.name), f.name


def test_register_then_sweep_renews_via_exists() -> None:
    led, path = _ledger()
    try:
        ops = FakeOps(present=True)
        loop = KeepaliveLoop(ops, led, interval_s=999, window_s=600)
        loop.register(_P)
        loop.sweep_once()
        assert ops.calls == [_P]  # exists 被调 = 续租发生（D1 F15）
        assert led.list_keepalive() == [_P]  # 仍在保
    finally:
        led.close()
        os.unlink(path)


def test_window_expiry_unregisters() -> None:
    led, path = _ledger()
    try:
        ops = FakeOps(present=True)
        loop = KeepaliveLoop(ops, led, interval_s=999, window_s=10)
        loop.register(_P)
        # 把 registered_at 拨到窗口外（老行 = 重启后恢复的超期注册）
        led._conn.execute("UPDATE keepalive SET registered_at = ?", (time.time() - 11,))
        led._conn.commit()
        loop.sweep_once()
        assert ops.calls == []  # 到期行不再续租
        assert led.list_keepalive() == []  # 停租注销（保活必须有界，D3 约束）
    finally:
        led.close()
        os.unlink(path)


def test_present_false_unregisters() -> None:
    """副本已不在池：续租没有对象，注销（重新预热由 intent 驱动）。"""
    led, path = _ledger()
    try:
        ops = FakeOps(present=False)
        loop = KeepaliveLoop(ops, led, interval_s=999, window_s=600)
        loop.register(_P)
        loop.sweep_once()
        assert ops.calls == [_P]
        assert led.list_keepalive() == []
    finally:
        led.close()
        os.unlink(path)


def test_query_error_skips_round_keeps_registration() -> None:
    """查询失败（可重试）跳过本轮：行保留、不注销——TTL 内多轮容错。"""
    led, path = _ledger()
    try:
        ops = FakeOps(error=QueryError("rpc boom"))
        loop = KeepaliveLoop(ops, led, interval_s=999, window_s=600)
        loop.register(_P)
        loop.sweep_once()
        assert ops.calls == [_P]  # 尝试过
        assert led.list_keepalive() == [_P]  # 但行保留
    finally:
        led.close()
        os.unlink(path)


def test_restart_recovery_needs_no_special_casing() -> None:
    """重启恢复：新 Loop + 同一 Ledger，首扫即接管在保前缀。"""
    led, path = _ledger()
    try:
        KeepaliveLoop(FakeOps(), led, interval_s=999, window_s=600).register(_P)
        led.close()
        # 「重启」：新 Ledger 连接 + 新 Loop
        led2 = Ledger(path)
        ops2 = FakeOps(present=True)
        loop2 = KeepaliveLoop(ops2, led2, interval_s=999, window_s=600)
        loop2.sweep_once()
        assert ops2.calls == [_P]  # 无需任何恢复代码即续租
        led2.close()
    finally:
        os.unlink(path)


def test_reregister_refreshes_window() -> None:
    """重复注册刷新 registered_at（窗口重算 = 再次预热即续约）。"""
    led, path = _ledger()
    try:
        loop = KeepaliveLoop(FakeOps(), led, interval_s=999, window_s=600)
        loop.register(_P)
        led._conn.execute("UPDATE keepalive SET registered_at = ?", (time.time() - 500,))
        led._conn.commit()
        loop.register(_P)  # 再次 READY → 重新注册
        rec = led.list_keepalive_records()[0]
        assert time.time() - rec.registered_at < 5  # 窗口被刷新
    finally:
        led.close()
        os.unlink(path)


def test_start_stop_thread_lifecycle() -> None:
    led, path = _ledger()
    try:
        ops = FakeOps()
        loop = KeepaliveLoop(ops, led, interval_s=0.05, window_s=600)
        loop.register(_P)
        loop.start()
        loop.start()  # 幂等
        deadline = time.monotonic() + 2
        while not ops.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        loop.stop()
        assert ops.calls  # 后台线程确实跑了 sweep
        assert loop._thread is None
    finally:
        led.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# Ledger 多线程（E11 决策一：RLock 串行化，多线程调用不炸）
# ---------------------------------------------------------------------------


def test_ledger_thread_safe_concurrent_writes() -> None:
    led, path = _ledger()
    try:
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(50):
                    p = Prefix(model_id="m", token_ids=(n, i))
                    led.register_keepalive(p)
                    led.upsert_task(
                        f"t{n}-{i}", handle_id=f"h{n}", op="copy",
                        prefix=p, target="inst", status="running",
                    )
                    led.list_keepalive()
                    led.list_running_tasks()
            except Exception as e:  # pragma: no cover - 失败即断言
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(led.list_keepalive()) == 8 * 50
        assert len(led.list_running_tasks()) == 8 * 50
    finally:
        led.close()
        os.unlink(path)
