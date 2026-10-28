"""TaskEngine：intent 队列 + 协程并发执行（E11 核心，L2 支撑组件）。

形态（code-architecture.md「运行时结构」定稿）：

    intent 入队（asyncio.Queue）
        → 协程消费者出队
        → 过传输闸（Semaphore，限同时在后端传输的任务数）
        → 执行 PrefetchFlow.run
        → 终态记账 + READY 后注册保活

无优先级、不分轻重通道——轻任务本来就不经此队列（保活走
KeepaliveLoop，查询由各自调用方直接 await）。

**为什么是协程而非线程池**：一个 copy 任务 30 秒里 RPC 执行合计约 1
秒，其余在等（轮询 sleep、等 RPC 返回）。线程池下等待也把线程占死；
协程下等待只是挂起——100 个任务挂着也只占 RPC executor 那几个线程。

**传输闸的语义**：只在确认要发后台传输时拿名额（copy 提交前 acquire，
终态 release）——保护 mooncake 与 RDMA 带宽。毫秒级操作（exists 探测 /
查询 / 副本核验）不过闸，其瞬时并发由 RPC executor 天然限制。

**线程边界**：`submit_intent` / `intent_status` 是**同步方法**，供 gRPC
servicer（在其线程池线程里）调用——入队经 `call_soon_threadsafe` 投递
到事件循环，不阻塞调用线程。其余全是协程。

并发纪律：backend 共享态（`_plans`/`_watch`）无锁——事件循环单线程，
协程只在 await 点切换；禁止在读写这些字典之间 await。Ledger 自己串行
（专用单线程 executor）。
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from typing import Callable

from kvcm.flows.prefetch import PrefetchError, PrefetchFlow
from kvcm.ledger import prefix_key
from kvcm.loops import KeepaliveLoop
from kvcm.ops.base import KvOps, QueryError, TaskHandle, TaskStatus, VerifyError
from kvcm.ops.types import InstanceId, Prefix

# 限速器钩子：() -> bool，False = 暂缓发起（E15 接入点，提交前询问）
RateLimiter = Callable[[], bool]


class IntentStatus(enum.Enum):
    """intent 观测态（E12 回执通道）。不另建状态存储——进程内记录，
    重启清零（调用方重新提交）。"""

    PENDING = 1  # 排队或执行中
    READY = 2
    FAILED = 3


def intent_id_of(prefix: Prefix, target: InstanceId) -> str:
    """intent 幂等键（D1 §4：前缀 × 目标实例）。"""
    return f"{prefix_key(prefix)}|{target}"


@dataclass
class _IntentRecord:
    """一次 intent 的执行记录（进程内）。"""

    intent_id: str
    prefix: Prefix
    target: InstanceId
    poll_s: float
    timeout_s: float
    status: IntentStatus = IntentStatus.PENDING
    detail: str = ""
    done: asyncio.Event = field(default_factory=asyncio.Event)


class TaskEngine:
    """预热 intent 的协程执行引擎。"""

    def __init__(
        self,
        ops: KvOps,
        *,
        keepalive: KeepaliveLoop | None = None,
        max_concurrent: int = 4,
        limiter: RateLimiter | None = None,
        queue_size: int = 0,
        consumer_count: int | None = None,
    ) -> None:
        """
        max_concurrent：**传输闸名额数**（同时在后端传输的任务数，保护
        mooncake 与带宽），不是线程数。
        limiter：限速器钩子（E15 接入点）；False 时等待后重试。
        queue_size：队列容量；0 = 不限（排队是协程挂起，不占资源；限容量
        是给入口做背压用的——满了 `submit_intent` 会阻塞 gRPC 线程，这
        正是想要的背压效果）。
        consumer_count：消费者协程数，默认 = max_concurrent（它们几乎总
        在挂起等待，多开无益）。
        """
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self._ops = ops
        self._flow = PrefetchFlow(ops)
        self._keepalive = keepalive
        self._limiter = limiter
        self._queue: asyncio.Queue[_IntentRecord] = asyncio.Queue(maxsize=queue_size)
        self._gate = asyncio.Semaphore(max_concurrent)
        self._records: dict[str, _IntentRecord] = {}
        self._accepting = True
        self._consumers: list[asyncio.Task] = []
        self._recover_tasks: list[asyncio.Task] = []
        # 消费者数默认远大于闸名额：出队后先做 exists（闸外），再在
        # 闸上挂起。若等于名额数，第 N+1 个 intent 连队列都出不来，
        # 它的 exists 会被在途传输堵住（定稿要避免的）
        self._consumer_count = consumer_count or max(16, max_concurrent * 4)
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # 生命周期（由 daemon 在事件循环内调用）
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动协程消费者。必须在事件循环内调用（daemon 的 async main）。"""
        self._loop = asyncio.get_running_loop()
        for i in range(self._consumer_count):
            self._consumers.append(
                asyncio.create_task(self._consume_loop(), name=f"kvcm-consumer-{i}")
            )

    async def shutdown(self, *, timeout_s: float = 5.0) -> None:
        """优雅退出：停收 → 取消协程 → 有界等待协程退出。

        两步，缺一不可：

        1. **取消协程**：在途 intent 收到 `CancelledError`，flow 收口
           （Ledger 记 abandoned）。取消**不是**取消后端任务（后端无此
           能力），只断开跟踪；副本清理由 remove 负责。
        等 RPC 线程真正结束**不在这里**：池由 daemon 创建并注入，等待写在
        daemon 的退出收尾（`_wait_executor`）——因为 `backend.close()` 对
        注入的池会直接 return，等不到。本方法只管协程侧（取消 + 有界等待）。
        分成两处的原因：协程的取消与线程的结束是两件事，
        `run_in_executor` 取消时不会结束线程。
        """
        self._accepting = False
        # ① 取消范围：消费者 + 恢复跟踪任务（后者由 daemon 经
        #    track_in_background 起）
        targets = list(self._consumers) + list(self._recover_tasks)
        for t in targets:
            t.cancel()
        if targets:
            await asyncio.wait(targets, timeout=timeout_s)
        self._consumers.clear()
        self._recover_tasks.clear()
        # ② 注意：取消协程**不会**结束线程里的 RPC（run_in_executor 的取消
        #    只是不再等结果）。等线程的事归 daemon 的 `_wait_executor`——
        #    池由 daemon 创建并注入，`backend.close()` 对注入的池会直接
        #    return（`_owns_executor` 为假），等不到。这里不做。
        # 队列里还没被取走的：标记失败，别让调用方永远等一个 PENDING
        while not self._queue.empty():
            rec = self._queue.get_nowait()
            rec.status = IntentStatus.FAILED
            rec.detail = "cancelled (shutdown, never started)"
            rec.done.set()

    # ------------------------------------------------------------------
    # intent 入口（同步，供 gRPC servicer 线程调用）
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
        `intent_status` 查询（本服务不在请求放行链路上）。

        线程安全：可从任意线程调用（经 `call_soon_threadsafe` 入队）。
        """
        # 停收检查放这里（同步、立即失败）：`_accepting` 是简单布尔读，
        # 跨线程读安全。最坏情况是关闭瞬间放行一个 intent——`_admit` 在
        # 循环线程里还会再判一次，双重保险。让调用方明确收到 UNAVAILABLE
        # 比「拿到的 key 永远查不到状态」好得多。
        if not self._accepting:
            raise RuntimeError("task engine is shutting down")
        loop = self._loop
        if loop is None:
            raise RuntimeError("task engine not started (call start() first)")
        key = intent_id_of(prefix, target)
        # 去重与入队都投递到事件循环线程内完成——gRPC 用多线程接请求，
        # 「先查 _records 再写」在这里跨线程会重复执行（两个线程同时看
        # 到无记录，各入队一次）
        loop.call_soon_threadsafe(
            self._admit, key, prefix, target, poll_s, timeout_s
        )
        return key

    def _admit(
        self,
        key: str,
        prefix: Prefix,
        target: InstanceId,
        poll_s: float,
        timeout_s: float,
    ) -> None:
        """在事件循环线程内做去重判定与入队（submit_intent 的投递目标）。"""
        if not self._accepting:
            # 停收：已有记录时**不能覆盖**（正在跑的那条还在改旧对象，
            # 覆盖会让 GetTask 看到 "rejected" 而实际还在执行）
            if key in self._records:
                return
            # 无记录：记一条 FAILED 而不是丢弃——调用方已经拿到 key
            # （同步检查与这里之间有空隙），丢弃会让它的 GetTask 永远 UNKNOWN
            rec = _IntentRecord(
                intent_id=key,
                prefix=prefix,
                target=target,
                poll_s=poll_s,
                timeout_s=timeout_s,
            )
            rec.status = IntentStatus.FAILED
            rec.detail = "rejected (shutting down)"
            rec.done.set()
            self._records[key] = rec
            return
        existing = self._records.get(key)
        if existing is not None and existing.status is IntentStatus.PENDING:
            return  # 在途去重
        rec = _IntentRecord(
            intent_id=key,
            prefix=prefix,
            target=target,
            poll_s=poll_s,
            timeout_s=timeout_s,
        )
        self._records[key] = rec
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            rec.status = IntentStatus.FAILED
            rec.detail = "rejected (queue full)"
            rec.done.set()

    def intent_status(self, intent_id: str) -> IntentStatus | None:
        """查询 intent 执行状态（回执通道）。None = 未知 intent_id。"""
        rec = self._records.get(intent_id)
        return rec.status if rec is not None else None

    def intent_detail(self, intent_id: str) -> str:
        """失败原因（回执 detail）：终态 FAILED 时返回异常文本（截断），
        其余情形空串。失败不带原因的回执等于没有回执。"""
        rec = self._records.get(intent_id)
        return rec.detail if rec is not None else ""

    # ------------------------------------------------------------------
    # 直接调用（脚本 / 调试 / 测试）
    # ------------------------------------------------------------------

    async def run_once(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 120.0,
    ) -> None:
        """直接执行一次预热（不排队、不记账；脚本与测试用）。"""
        await self._run_prefetch(prefix, target, poll_s, timeout_s)

    async def track(
        self,
        handle: TaskHandle,
        *,
        poll_s: float = 1.0,
        timeout_s: float = 300.0,
    ) -> None:
        """rebuild 恢复的 handle 收编：只轮询到终态（不重跑流程）。"""
        await self._track(handle, poll_s, timeout_s)

    def track_in_background(
        self, handle: TaskHandle, *, poll_s: float = 1.0, timeout_s: float = 300.0
    ) -> None:
        """起一个后台 task 收编恢复的 handle，并**登记**它——shutdown 要
        能取消并等待它（否则退出时它可能还停在 query_task 里，调用方随
        即关 client）。"""
        t = asyncio.create_task(
            self._track(handle, poll_s, timeout_s), name="kvcm-recover"
        )
        self._recover_tasks.append(t)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        """消费者：出队 → 过闸 → 执行 → 记终态。"""
        while True:
            rec = await self._queue.get()
            try:
                await self._run_prefetch(
                    rec.prefix, rec.target, rec.poll_s, rec.timeout_s
                )
                rec.status = IntentStatus.READY
            except asyncio.CancelledError:
                rec.status = IntentStatus.FAILED
                rec.detail = "cancelled (shutdown)"
                rec.done.set()
                raise
            except PrefetchError as e:
                rec.status = IntentStatus.FAILED
                rec.detail = f"{type(e).__name__}: {e}"[:500]
            except QueryError as e:
                # exists 的查询失败（不包成 PrefetchError）只该判定这次
                # intent 失败——**不能向上抛**，否则消费者协程退出，几次
                # 抖动后全部消费者死光、新 intent 永远 PENDING
                rec.status = IntentStatus.FAILED
                rec.detail = f"{type(e).__name__}: {e}"[:500]
            except Exception as e:  # 编程错误也留原因，再往上抛（不吞）
                rec.status = IntentStatus.FAILED
                rec.detail = f"{type(e).__name__}: {e}"[:500]
                raise
            finally:
                rec.done.set()
                self._queue.task_done()

    async def _run_prefetch(
        self,
        prefix: Prefix,
        target: InstanceId,
        poll_s: float,
        timeout_s: float,
    ) -> None:
        """跑一次预热。

        传输闸的范围（定稿）：**exists 探测在闸外**（毫秒级操作不该被
        传输堵），只有「提交后台传输 → 等到终态」这一段占名额——名额
        的语义是「后端还在为这个前缀搬数据」，轮询期间确实如此。
        """
        await self._throttle()
        # 闸外：存在性检查（毫秒级 RPC，读即续租）
        if not await self._ops.exists(prefix):
            raise PrefetchError(
                "prefix not fully in pool — warm 未覆盖该前缀、key 口径与引擎漂移，"
                "或异构前提未满足（源引擎未开 tp_mismatch / PEER_TP_SIZE 两端错位，"
                "则 effective 空间 key 全 miss）"
            )
        async with self._gate:
            await self._flow.run_after_exists(
                prefix, target, poll_s=poll_s, timeout_s=timeout_s
            )
        # READY 接线：副本已核验到位 → 注册保活（窗口策略归 Loop）
        if self._keepalive is not None:
            self._keepalive.register(prefix)

    async def _track(self, handle: TaskHandle, poll_s: float, timeout_s: float) -> None:
        """轮询恢复的句柄到终态。"""
        # 轮询前取 prefix：终态即删核验登记，READY 后再取就是 None
        prefix = self._ops.prefix_of(handle)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        try:
            await self._track_loop(handle, prefix, loop, deadline, poll_s)
        except asyncio.CancelledError:
            # 进程退出取消：断开跟踪并记账（否则账本留 running，重启又
            # 被 rebuild 捞回来跟）。后端任务不取消（无此能力）。
            try:
                await self._ops.abandon(handle)
            except KeyError:
                pass
            raise

    async def _track_loop(self, handle, prefix, loop, deadline, poll_s):
        """轮询恢复的句柄到终态。

        三种「非终态」的处理各有明确语义：
        - `QueryError` → 本轮无结论（可重试），继续轮询到 deadline
        - `VerifyError` → 终态（核验失败已由 backend 记账），停止跟踪
        - `KeyError` → 句柄已被 abandon（账本已记 abandoned），停止跟踪
        """
        while True:
            try:
                status = await self._ops.query_task(handle)
            except QueryError:
                status = None  # 本轮无结论：继续轮询（不是终态）
            except VerifyError:
                return  # 终态（核验失败已记账），不再跟踪
            except KeyError:
                return  # 句柄已被收口（shutdown abandon），预期内
            if status in (TaskStatus.READY, TaskStatus.FAILED):
                # READY 接线（恢复侧）：重启前没来得及注册的保活在这里补上
                if status is TaskStatus.READY and self._keepalive is not None:
                    if prefix is not None:
                        self._keepalive.register(prefix)
                return
            if loop.time() >= deadline:
                await self._ops.abandon(handle)  # 超时收口（与流程同语义）
                return
            await asyncio.sleep(poll_s)

    async def _throttle(self) -> None:
        """限速器钩子（E15 接入点）：False = 等下一轮再试。"""
        while self._limiter is not None and not self._limiter():
            await asyncio.sleep(0.5)
