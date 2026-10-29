"""TaskEngine：intent 队列 + 协程并发执行（E11 核心，L2 支撑组件）。

形态（code-architecture.md「运行时结构」定稿）：

    intent 入队（asyncio.Queue）
        → 协程消费者出队
        → 过传输闸（Semaphore，限同时在后端传输的任务数）
        → 执行 PrefetchFlow.run
        → 终态记账 + READY 后注册保活

无优先级、不分轻重通道——轻任务本来就不经此队列（保活走
KeepaliveLoop，查询由各自调用方直接 await）。

**为什么是协程而非线程池**：一个 copy 任务的生命周期以十秒计，其中 RPC
真正在执行的时间只占很小一部分，其余在等（轮询 sleep、等 RPC 返回）。
线程池下等待也把线程占死；协程下等待只是挂起——100 个任务挂着也只占
RPC executor 那几个线程。

**传输闸的语义**：只在确认要发后台传输时拿名额（copy 提交前 acquire，
终态 release）——保护 mooncake 与 RDMA 带宽。闸外只有 exists 探测
（预热的前置检查，毫秒级 RPC）；`run_after_exists` 整段（提交、轮询、
副本核验）在闸内，因为它们同属「后端还在为这个前缀搬数据」这一件事。

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
from collections import OrderedDict
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
    """一次 intent 的执行记录（进程内，只在途用它）。

    进终态后会被换成 `_Finished`（只剩 intent_id / status / detail），
    大字段（`prefix` 里的 token 序列）随之释放。
    """

    intent_id: str
    prefix: Prefix
    target: InstanceId
    poll_s: float
    timeout_s: float
    status: IntentStatus = IntentStatus.PENDING
    detail: str = ""
    # 是否已投进队列（区分 submit_intent 刚建的占位与已在途）
    queued: bool = False


@dataclass(frozen=True)
class _Finished:
    """终态回执（瘦记录）。"""

    intent_id: str
    status: IntentStatus
    detail: str = ""


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
        finished_max: int = 25600,
    ) -> None:
        """
        max_concurrent：**传输闸名额数**（同时在后端传输的任务数，保护
        mooncake 与带宽），不是线程数。
        limiter：限速器钩子（E15 接入点）；False 时等待后重试。
        queue_size：队列容量；0 = 不限（排队是协程挂起，不占资源）。限容量
        时满了**不阻塞**——`_admit` 用 `put_nowait`，`QueueFull` 直接把这条
        intent 记成 FAILED（`rejected (queue full)`）；调用方已经拿到 id，
        状态经 `GetTask` 查。若要做入口背压，应由 gateway 侧限并发，不在
        这里阻塞 gRPC 线程。
        consumer_count：消费者协程数，默认 `max(16, max_concurrent × 4)`
        ——**必须远大于名额数**：消费者出队后先在闸外做存在性检查，再去
        等名额。若等于名额数，第 N+1 个请求连队列都出不来，它的检查会被
        在途传输堵住。
        finished_max：终态回执保留条数。`_records` 只存在途；进终态后
        换成瘦记录（只剩 intent_id / status / detail）放进定长环，超出
        按插入顺序丢最旧的。默认 25600 ≈ 16 MB 量级（空 detail 单条约
        650 B）——**不是内存上限**：失败路径的 detail 截到 500 字后单条
        约 1 KB，环实际可以明显大于 16 MB。
        """
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self._ops = ops
        self._flow = PrefetchFlow(ops)
        self._keepalive = keepalive
        self._limiter = limiter
        self._queue: asyncio.Queue[_IntentRecord] = asyncio.Queue(maxsize=queue_size)
        self._gate = asyncio.Semaphore(max_concurrent)
        # 只存在途（PENDING）。这是去重表：同键有 PENDING 记录就返回既有
        # id 不重复执行——**淘汰 PENDING 会让同键执行两次**，所以这里
        # 永不淘汰，终态即移走。
        self._records: dict[str, _IntentRecord] = {}
        # 终态回执：有界表（按插入顺序淘汰最旧的）。查表与淘汰都是 O(1)。
        # 只有 GetTask 读，观测面用。
        self._finished: OrderedDict[str, _Finished] = OrderedDict()
        self._finished_max = max(1, finished_max)
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
        # 队列里还没被取走的：标记失败，别让调用方永远等一个 PENDING。
        # `get_nowait` 与 `task_done` 必须配对，否则 `_unfinished_tasks`
        # 泄漏（将来有人 `await queue.join()` 会永久挂住）。
        while True:
            try:
                rec = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                self._finish(
                    rec, IntentStatus.FAILED, "cancelled (shutdown, never started)"
                )
            finally:
                self._queue.task_done()
        # 已建占位但 _admit 还没跑到的（不在队列里）：同样标失败。
        # 必须先用 `list(...)` 快照——`_finish` 会从 `_records` 删条目，
        # 边迭代边删会抛 `RuntimeError: dictionary changed size`。
        for rec in list(self._records.values()):
            if rec.status is IntentStatus.PENDING and not rec.queued:
                self._finish(
                    rec, IntentStatus.FAILED, "cancelled (shutdown, never started)"
                )

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
        rec = _IntentRecord(
            intent_id=key,
            prefix=prefix,
            target=target,
            poll_s=poll_s,
            timeout_s=timeout_s,
        )
        # `setdefault` 是一次原子插入：槽里已有记录时返回它，自己的写不生效。
        # 「先 get 再赋值」之间有空隙——两个 gRPC 线程同时提交同一键时，输家
        # 会把赢家已入队的 PENDING 覆盖成自己的新记录，两个 `_admit` 各入队
        # 一次，同一前缀执行两遍。这里让插入本身决定归属，输家直接返回。
        #
        # `_records` 里只有 PENDING（终态已移到 `_finished`），所以「槽非空」
        # 等价于「在途」；终态同键再提交时槽是空的，正常走新意图。
        if self._records.setdefault(key, rec) is not rec:
            return key
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
            rec = self._records.get(key)
            if rec is None:
                rec = _IntentRecord(
                    intent_id=key,
                    prefix=prefix,
                    target=target,
                    poll_s=poll_s,
                    timeout_s=timeout_s,
                )
                # 同样不覆盖：槽已被占说明别人刚提交，交给 shutdown 收口
                if self._records.setdefault(key, rec) is not rec:
                    return
            if rec.queued or rec.status is not IntentStatus.PENDING:
                return  # 已在途：交给 shutdown 的取消/清队收口，不覆盖
            self._finish(
                rec, IntentStatus.FAILED, "cancelled (shutdown, never started)"
            )
            return
        rec = self._records.get(key)
        if rec is None:
            # 正常走不到：占位由 `submit_intent` 的 setdefault 装好才投递。
            # 只有测试会直接调 `_admit`。补一条，但同样不覆盖已有记录。
            rec = _IntentRecord(
                intent_id=key,
                prefix=prefix,
                target=target,
                poll_s=poll_s,
                timeout_s=timeout_s,
            )
            if self._records.setdefault(key, rec) is not rec:
                return
            rec.queued = True
        elif rec.queued:
            return  # 在途去重
        else:
            rec.queued = True  # submit_intent 建的占位：入队
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            self._finish(rec, IntentStatus.FAILED, "rejected (queue full)")

    def _finish(
        self, rec: _IntentRecord, status: IntentStatus, detail: str = ""
    ) -> None:
        """置终态：从在途表移走，换成瘦记录放进终态环。

        两道判据缺一不可：
        - **身份**：只有当前槽主能收口。后一代已顶替时，本次是迟到的收口，
          直接丢弃（否则会把新一代的 PENDING 删掉）。
        - **状态**：已终态的幂等——drain 与 sweep 可能同时收口同一条，第二次
          不能把 READY 改写成 FAILED。

        写入顺序是承重的：先改记录状态，再写回执，最后才移出槽。读侧先查
        `_records` 再查 `_finished`，按此顺序任意交错都读到真值，不会出现
        「两边都空」的 UNKNOWN 窗口。
        """
        if self._records.get(rec.intent_id) is not rec:
            return
        if rec.status is not IntentStatus.PENDING:
            return
        rec.status = status
        rec.detail = detail
        # 同键重提会覆盖旧代，并把这条移到最新（不占两个位置）
        self._finished[rec.intent_id] = _Finished(
            intent_id=rec.intent_id, status=status, detail=detail
        )
        self._finished.move_to_end(rec.intent_id)
        while len(self._finished) > self._finished_max:
            self._finished.popitem(last=False)
        del self._records[rec.intent_id]

    def intent_status(self, intent_id: str) -> IntentStatus | None:
        """查询 intent 执行状态（回执通道）。None = 未知 intent_id。

        两处查：在途表（`_records`）→ 终态环（`_finished`）。都没有说明
        这条回执已随终态环淘汰、或进程重启过——两者同义（观测面可丢）。
        """
        rec = self._records.get(intent_id)
        if rec is not None:
            return rec.status
        fin = self._finished.get(intent_id)
        return fin.status if fin is not None else None

    def intent_detail(self, intent_id: str) -> str:
        """失败原因（回执 detail）：终态 FAILED 时返回异常文本，其余空串。

        失败不带原因的回执等于没有回执。
        """
        rec = self._records.get(intent_id)
        if rec is not None:
            return rec.detail
        fin = self._finished.get(intent_id)
        return fin.detail if fin is not None else ""

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
                self._finish(rec, IntentStatus.READY)
            except asyncio.CancelledError:
                self._finish(rec, IntentStatus.FAILED, "cancelled (shutdown)")
                raise
            except PrefetchError as e:
                self._finish(rec, IntentStatus.FAILED, f"{type(e).__name__}: {e}"[:500])
            except QueryError as e:
                # exists 的查询失败（不包成 PrefetchError）只该判定这次
                # intent 失败——**不能向上抛**，否则消费者协程退出，几次
                # 抖动后全部消费者死光、新 intent 永远 PENDING
                self._finish(rec, IntentStatus.FAILED, f"{type(e).__name__}: {e}"[:500])
            except Exception as e:  # 编程错误也留原因，再往上抛（不吞）
                self._finish(rec, IntentStatus.FAILED, f"{type(e).__name__}: {e}"[:500])
                raise
            finally:
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

        三种异常各有明确结局（前一种是「继续」，后两种是「停止」）：
        - `QueryError` → 本轮无结论（可重试），**继续轮询**到 deadline
        - `VerifyError` → **终态**（核验失败已由 backend 记账），停止跟踪
        - `KeyError` → **终态**（句柄已被 abandon，账本已记 abandoned），停止跟踪
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
