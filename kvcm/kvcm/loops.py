"""常驻循环（E11 进程化的支撑组件，L2）。

KeepaliveLoop 本期落地。ReleaseLoop / RetainLoop 只留骨架，不调用
已落地的 move / remove / demote，也不实现 retain。续租仍是本循环的 exists。

形态：**协程定时器**（事件循环内 `asyncio.sleep` 周期触发），不是独立
线程——E11 执行模型。

线程边界：`register` / `unregister` 是同步方法（可从任意线程调用，例如
TaskEngine 的协程里、或 daemon 启动期）——内部把 Ledger 写入经
`call_soon_threadsafe` 交给事件循环执行。`sweep_once` 是协程，只在事件
循环内跑。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
import time

from kvcm.ledger import Ledger
from kvcm.ops.base import KvOps, QueryError
from kvcm.ops.types import Prefix

logger = logging.getLogger(__name__)


class KeepaliveLoop:
    """保活注册表驱动的周期续租循环。

    机制（D1 F15 / D2 F9）：`batch_is_exist` 顺带续租（`ProbeKey` 不
    续），lease TTL 是 master 配置（测试床实配 11s）——周期调
    `ops.exists(prefix)` 即续租，间隔必须显著小于 TTL。

    策略（D3 未定，E11 占位 = **固定窗口**）：注册后续 `window_s`
    （默认 10 min），到期停租注销。窗口按 Ledger 的 `registered_at`
    起算——重启恢复后窗口自然缩短、已过期的首扫即注销。保活必须有界：
    无限续租事实上等于 pin（D3 约束）。`exists` 返回 False（副本已不在
    池）即注销——续租没有对象，重新预热由 intent 驱动并重新注册。D3
    正式定型时替换策略实现，Loop 机制不变。
    """

    def __init__(
        self,
        ops: KvOps,
        ledger: Ledger,
        *,
        interval_s: float = 5.0,
        window_s: float = 600.0,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self._ops = ops
        self._ledger = ledger
        self._interval = interval_s
        self._window = window_s
        self._task: asyncio.Task | None = None
        self._stop = False
        self._register_failures = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # 注册（同步，任意线程可调）
    # ------------------------------------------------------------------

    def register(self, prefix: Prefix) -> None:
        """登记保活。

        调用场景：TaskEngine 在副本 READY 后注册。终态后再提交会再次
        注册。行已存在时保留原来的 ``registered_at``，不把窗口拨回现在。

        **不抛异常**：保活失败只意味着副本可能提前变冷，不是预热失败。
        这里吞掉并计数（`register_failures`），避免把已经 READY 的 intent
        误记成 FAILED。
        """
        self._submit_ledger(self._ledger.register_keepalive, prefix)

    def unregister(self, prefix: Prefix) -> None:
        self._submit_ledger(self._ledger.unregister_keepalive, prefix)

    @property
    def register_failures(self) -> int:
        """保活写入失败累计数（监控用；>0 说明副本可能提前变冷）。"""
        return self._register_failures

    def _submit_ledger(self, fn, *args) -> None:
        """把 Ledger 写入交给 executor 执行（不阻塞调用方）。

        三条避坑（都是并发/退出期会踩到的）：

        1. **绝不抛到调用方**：调用方是 `_run_prefetch` 的末尾——那里抛
           异常会把已经 READY 的 intent 记成 FAILED。失败只计数。
        2. **不在调用线程直接跑 sqlite**：调用方可能就是事件循环线程
           （协程里调 register），就地执行会卡住整个循环。
        3. **持有 task 并观察结果**：`ensure_future` 的返回值丢弃的话，
           写入失败会变成「exception was never retrieved」，注册方看不到。
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            # 事件循环未起：交给 Ledger 自己的 executor，不在调用线程做 sqlite。
            # Future 必须观察，否则写入失败变成「exception was never retrieved」。
            try:
                fut = self._ledger.executor.submit(fn, *args)
            except RuntimeError:
                self._note_failure()  # 池已关（退出期）
                return
            fut.add_done_callback(self._observe_ledger_future)
            return
        try:
            loop.call_soon_threadsafe(self._schedule_ledger_write, fn, args)
        except RuntimeError:
            # 循环正在关闭（is_running() 为真但已不可投递）
            self._note_failure()

    def _schedule_ledger_write(self, fn, args) -> None:
        """在事件循环线程内建 task 并挂观察者（由 call_soon_threadsafe 投递）。"""
        task = asyncio.ensure_future(self._ledger_call(fn, *args))
        task.add_done_callback(self._on_ledger_write_done)

    def _on_ledger_write_done(self, task: asyncio.Task) -> None:
        self._observe_ledger_future(task)

    def _observe_ledger_future(self, fut) -> None:
        if fut.cancelled():
            return
        try:
            exc = fut.exception()
        except Exception as e:
            self._note_failure(e)
            return
        if exc is not None:
            self._note_failure(exc)

    def _note_failure(self, exc: BaseException | None = None) -> None:
        self._register_failures += 1
        if exc is not None:
            # 登记写入失败：这条前缀**没有**保活行，副本会按租约自然变冷。
            # 不打印的话外部看不到任何信号（`register_failures` 没有读者）。
            print(
                f"[kvcm] keepalive register failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            sys.stderr.flush()

    async def _ledger_call(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._ledger.executor, functools.partial(fn, *args)
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动协程定时器（必须在事件循环内调用）。幂等。"""
        self._loop = asyncio.get_running_loop()
        if self._task is not None and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="kvcm-keepalive")

    async def stop(self, *, timeout_s: float = 10.0) -> None:
        """停循环（优雅退出）。不撤销注册——保活行留在 Ledger，重启后续
        租或到期注销；lease TTL 到期自然失效，不残留。"""
        self._stop = True
        if self._task is not None:
            try:
                await asyncio.wait([self._task], timeout=timeout_s)
            finally:
                if not self._task.done():
                    self._task.cancel()
                self._task = None

    def close(self) -> None:
        """不关 Ledger 的 executor。那是 Ledger 自己的池。"""

    async def _run(self) -> None:
        while not self._stop:
            await asyncio.sleep(self._interval)
            if self._stop:
                return
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 单轮失败不杀循环，但必须留痕：静默时租约会悄悄过期
                print(
                    f"[kvcm] keepalive sweep failed: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                continue

    # ------------------------------------------------------------------
    # 一轮续租（独立成方法便于测试直调）
    # ------------------------------------------------------------------

    async def sweep_once(self) -> None:
        started = time.monotonic()
        now = time.time()
        records = await self._ledger_call(self._ledger.list_keepalive_records)
        for rec in records:
            if now - rec.registered_at >= self._window:
                # 窗口到：停租注销（占位策略，D3 定型时替换）
                await self._ledger_call(
                    self._ledger.unregister_keepalive, rec.prefix
                )
                continue
            if not await self._ledger_call(self._ledger.has_keepalive, rec.prefix):
                # 快照之后 stop_keepalive 已删行。再 exists 会把刚停的租约授满。
                continue
            try:
                present = await self._ops.exists(rec.prefix)
            except QueryError as e:
                # 查询失败跳过本轮（lease TTL 内有多个周期容错），但要留痕
                print(
                    f"[kvcm] keepalive exists failed: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                continue
            if not present:
                # 副本已不在池：续租没有对象，注销
                await self._ledger_call(
                    self._ledger.unregister_keepalive, rec.prefix
                )
        elapsed = time.monotonic() - started
        if elapsed > self._interval:
            logger.warning(
                "keepalive sweep slower than interval: prefixes=%d elapsed=%.3fs interval=%.3fs",
                len(records),
                elapsed,
                self._interval,
            )


class ReleaseLoop:
    """软释放 / demote / 主动 Remove 三级（D1 §6）。骨架——不调用
    已落地的 remove / demote。"""


class RetainLoop:
    """retain 与计划下线编排（D2 §4）。骨架——不调用 move，也不实现 retain。
    1fc27b6 没有 AddReplicaForRetainedClient。kRetained 是外部 buffer 引用计数。"""
