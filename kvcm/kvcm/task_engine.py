"""TaskEngine：intent 队列 + 并发执行（E11 核心，L2 支撑组件）。

形态（design-record 决策二）：intent 队列 + 并发上限（默认 4）+ 工作
线程池执行 `PrefetchFlow.run`——流程本体不改（同步 run 直接进池，
不拆 submit/poll/收口三段）。状态机 = Future 状态（排队/进行/完成）
+ Ledger running 迁移的组合，不另建状态存储。

并发纪律（决策一）：Ledger 锁在其内部；backend 共享态（_plans/_watch）
不加锁，靠「每个 handle 同一时刻只归一个工作线程」——handle 由单次
run/track 唯一产生，dict 操作原子性靠 GIL；track（恢复跟踪）与
submit（预热）的 handle 不相交。违反即 bug。
"""

from __future__ import annotations

import enum
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from kvcm.flows.prefetch import PrefetchFlow
from kvcm.ledger import prefix_key
from kvcm.loops import KeepaliveLoop
from kvcm.ops.base import (
    KvOps,
    QueryError,
    TaskHandle,
    TaskStatus,
    VerifyError,
)
from kvcm.ops.types import InstanceId, Prefix

# 限速器钩子：() -> bool，False = 暂缓发起（E15 接入点，提交前询问）
RateLimiter = Callable[[], bool]


class IntentStatus(enum.Enum):
    """intent 观测态（E12 回执通道）：Future 状态 + 异常与否的映射，
    不另建状态存储。UNKNOWN 由调用方对缺失键自行表达（None）。"""

    PENDING = 1  # 排队或执行中
    READY = 2
    FAILED = 3


def intent_id_of(prefix: Prefix, target: InstanceId) -> str:
    """intent 幂等键（D1 §4：前缀 × 目标实例）。"""
    return f"{prefix_key(prefix)}|{target}"


class TaskEngine:
    """预热 intent 的并发执行引擎。"""

    def __init__(
        self,
        ops: KvOps,
        *,
        keepalive: KeepaliveLoop | None = None,
        max_concurrent: int = 4,
        limiter: RateLimiter | None = None,
    ) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self._ops = ops
        self._flow = PrefetchFlow(ops)
        self._keepalive = keepalive
        self._limiter = limiter
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="kvcm-task"
        )
        self._lock = threading.Lock()
        self._in_flight: set[TaskHandle] = set()
        self._accepting = True
        # intent 注册表（E12）：幂等键 → Future。在途去重；终态保留供
        # GetTask 查询（entry 累积的 TTL/GC 归生产化，steady-state 体量
        # ≈ 活跃前缀数，进程重启清零）
        self._intents: dict[str, Future] = {}

    # ------------------------------------------------------------------
    # intent 入口
    # ------------------------------------------------------------------

    def submit(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 120.0,
    ) -> Future:
        """异步提交预热 intent：排队 → 工作线程执行 PrefetchFlow.run。
        返回 Future（READY = 正常返回；失败 = PrefetchError）。"""
        with self._lock:
            if not self._accepting:
                raise RuntimeError("task engine is shutting down")
        return self._executor.submit(self._run_prefetch, prefix, target, poll_s, timeout_s)

    def submit_sync(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 120.0,
    ) -> None:
        """同步提交（阻塞到 READY/失败）。脚本与调试通道用。"""
        self.submit(prefix, target, poll_s=poll_s, timeout_s=timeout_s).result()

    # ------------------------------------------------------------------
    # intent 幂等入口（E12：策略侧埋钩子调用；intent ≠ command）
    # ------------------------------------------------------------------

    def submit_intent(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 120.0,
    ) -> str:
        """幂等提交预热 intent，返回幂等键（intent_id）。

        去重语义（D1 §4）：同键在途 → 返回既有 intent_id，不重复执行；
        同键已终态 → 视为新意图，重新预热。受理 ≠ 完成：执行状态经
        intent_status 查询。关闭中抛 RuntimeError（gRPC 层映射
        UNAVAILABLE）。"""
        key = intent_id_of(prefix, target)
        with self._lock:
            if not self._accepting:
                raise RuntimeError("task engine is shutting down")
            existing = self._intents.get(key)
            if existing is not None and not existing.done():
                return key  # 在途去重
            self._intents[key] = self._executor.submit(
                self._run_prefetch, prefix, target, poll_s, timeout_s
            )
            return key

    def intent_status(self, intent_id: str) -> IntentStatus | None:
        """查询 intent 执行状态（回执通道）。None = 未知 intent_id。"""
        with self._lock:
            fut = self._intents.get(intent_id)
        if fut is None:
            return None
        if not fut.done():
            return IntentStatus.PENDING
        return IntentStatus.READY if fut.exception() is None else IntentStatus.FAILED

    def track(
        self,
        handle: TaskHandle,
        *,
        poll_s: float = 1.0,
        timeout_s: float = 300.0,
    ) -> Future:
        """rebuild 恢复的 handle 收编：只轮询到终态（不重跑流程）。
        阶段 1 的 recover_poll 极简过渡由本方法替代。"""
        return self._executor.submit(self._track, handle, poll_s, timeout_s)

    # ------------------------------------------------------------------
    # 工作线程内
    # ------------------------------------------------------------------

    def _run_prefetch(
        self, prefix: Prefix, target: InstanceId, poll_s: float, timeout_s: float
    ) -> None:
        self._throttle()
        mine: list[TaskHandle] = []

        def sink(h: TaskHandle) -> None:
            mine.append(h)
            with self._lock:
                self._in_flight.add(h)

        try:
            self._flow.run(
                prefix, target, poll_s=poll_s, timeout_s=timeout_s, handle_sink=sink
            )
        except KeyError:
            # 句柄被 shutdown 收口后 flow 轮询撞 KeyError：仅退出期预期
            # 内，吞掉；运行期 KeyError 仍是编程错误，传播
            with self._lock:
                if self._accepting:
                    raise
        finally:
            with self._lock:
                for h in mine:
                    self._in_flight.discard(h)
        # READY 接线（prefetch.py「保活落地后注册」的接线点）：副本已
        # 核验到位 → 注册保活，窗口策略归 Loop（决策三）
        if self._keepalive is not None:
            self._keepalive.register(prefix)

    def _track(self, handle: TaskHandle, poll_s: float, timeout_s: float) -> None:
        with self._lock:
            self._in_flight.add(handle)
        # 轮询前取 prefix：终态即删核验登记，READY 后再取就是 None。
        # 恢复路径没有 submit 的 prefix 入参，只能靠后端登记
        prefix = self._ops.prefix_of(handle)
        try:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    status = self._ops.query_task(handle)
                except QueryError:
                    status = None  # 可重试，超时兜底
                except VerifyError:
                    return  # 终态（核验失败已记账），不再跟踪
                except KeyError:
                    return  # 句柄已被收口（shutdown abandon），预期内
                if status in (TaskStatus.READY, TaskStatus.FAILED):
                    # READY 接线（恢复侧）：重启前没来得及注册的保活在
                    # 这里补上——否则副本无续租、到期被驱逐，预热白做
                    if status is TaskStatus.READY and self._keepalive is not None:
                        if prefix is not None:
                            self._keepalive.register(prefix)
                    return  # 终态已记账
                if time.monotonic() >= deadline:
                    self._ops.abandon(handle)  # 超时收口（与流程同语义）
                    return
                time.sleep(poll_s)
        finally:
            with self._lock:
                self._in_flight.discard(handle)

    def _throttle(self) -> None:
        """限速器钩子（E15 接入点）：False = 背压留在 worker 等下一轮，
        受 flow 超时兜底。"""
        while self._limiter is not None and not self._limiter():
            time.sleep(0.5)

    # ------------------------------------------------------------------
    # 优雅退出（决策四②③）
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """停收 intent → 在途 abandon（记账不等待、不阻塞退出——后端无
        任务取消能力，副本清理归 remove）→ 线程池不收尾等待（工作线程
        内流程因句柄已收口而结束，Future 随进程退出丢弃）。"""
        with self._lock:
            self._accepting = False
            in_flight = list(self._in_flight)
        for h in in_flight:
            try:
                self._ops.abandon(h)
            except KeyError:
                pass  # 已终态被移除登记，正常
        self._executor.shutdown(wait=False)
