"""PrefetchFlow：预热编排——把已有前缀的 KV 预复制到目标实例本机层级。

编排步骤（只调原子操作层）：
1. 前置检查：前缀完整在池（warm 未覆盖或 key 口径漂移则 fail fast，
   复制无意义）
2. copy 发起（返回任务句柄）
3. query_task 轮询到 READY
4. 保活注册与 READY 事件补发归上层：保活由 `TaskEngine` 在本流程返回
   后注册；补发归接口层 EventPublisher（未接通）

失败分类（统一出口 = PrefetchError；编程错误除外，直接传播）：
- 前置检查不过（前缀确实不在池）/ copy 提交失败（SubmitError）——
  已落账的句柄先 abandon，再终态上抛；池里的传输不取消
- 前置检查查询失败（QueryError：RPC/单 key 查询错误）——**不经
  PrefetchError 包装，直接传播**：「查不出来」可重试，「不存在」
  才终态，两者不得混
- 核验失败（VerifyError：任务完成但副本不到位）——终态，带明细上抛
- 任务状态查询失败（QueryError：RPC 错误）——可重试，继续轮询到
  超时兜底
- 任务 FAILED / 超时——终态上抛；超时先 abandon 收口（删登记 + 记
  abandoned），再抛

轮询在本流程内（E11 进程化后由 TaskEngine 管并发与限速，流程本体不变）；
`asyncio.sleep` 让等待不占线程。重发 / 弃置清副本 / 源掉线衔接的策略归
TaskEngine。

任务状态记录（Ledger）由后端实现侧在 copy / query_task / abandon 时
登记迁移，本流程不直接写——记账需要任务号的持久化编码与状态迁移
的时机，两样都只在实现侧手里。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from kvcm.ops.base import (
    KvOps,
    QueryError,
    SubmitError,
    TaskHandle,
    TaskStatus,
    VerifyError,
)
from kvcm.ops.types import InstanceId, Prefix

logger = logging.getLogger(__name__)

# 退出时 Ledger 线程池已关，`_in_ledger` 抛的是这两句之一：
# `cannot schedule new futures after shutdown`，或解释器退出时的
# `cannot schedule new futures after interpreter shutdown`。
# 其它 RuntimeError 是编程错误，不能跟这两句混在一起吞掉。
_LEDGER_SHUTDOWN = "cannot schedule new futures after"


class PrefetchError(RuntimeError):
    """预热失败（前置检查不过 / 任务失败 / 核验失败 / 超时）。"""


class PrefetchFlow:
    """预热流程。无状态，全部操作经注入的原子操作接口。"""

    def __init__(self, ops: KvOps) -> None:
        self._ops = ops

    async def run(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 120.0,
        handle_sink=None,
    ) -> None:
        """执行到 READY；失败抛 PrefetchError（协程：等待期间不占线程）。

        timeout_s 覆盖本次调用里的提交和轮询。等目标段空间在 TaskEngine
        闸外，用的是同一段预算的剩余时间，不是再加一段。大前缀的提交本身
        可达几十秒，不计入则超时预算失真。

        handle_sink：可选回调，copy 提交成功即带上句柄调用一次。**退出
        收口不靠它**——取消（`CancelledError`）由 `run_after_exists` 自
        己 abandon，见那里的说明。保留此参数是为了让调用方能观测到在途
        句柄（调试、脚本），不改变流程语义。
        """
        if not await self._ops.exists(prefix):
            raise PrefetchError(
                "prefix not fully in pool — warm 未覆盖该前缀、key 口径与引擎漂移，"
                "或异构前提未满足（源引擎未开 tp_mismatch / PEER_TP_SIZE 两端错位，"
                "则 effective 空间 key 全 miss）"
            )
        await self.run_after_exists(
            prefix, target, poll_s=poll_s, timeout_s=timeout_s, handle_sink=handle_sink
        )

    async def run_after_exists(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 120.0,
        handle_sink=None,
    ) -> None:
        """存在性检查已通过之后的步骤：copy 提交 → 轮询到终态。

        单独成方法是为了让编排层决定**传输闸的范围**：exists 和等空间
        在闸外；这段「提交 → 等终态」占一个前缀名额，直到终态才还。
        见 `TaskEngine._run_prefetch`。

        timeout_s 是调用方给的剩余预算（从等空间开始的那一段里还剩多少）。
        copy 还没返回时到点：Python 3.12 的 `wait_for` 超时取消的是当前
        这次等待，copy 协程还在；真正取消 copy 的是后面的 `_drain_copy`。
        若那时已经有任务号，copy 把句柄交回来（账本先记 running），这里再
        `abandon`（账本记 abandoned），然后抛 PrefetchError。有句柄的文案
        与轮询超时相同：`copy not READY after {timeout_s}s (handle=...)`。
        没有任务号就不落账，也不 abandon，文案是 `cancelled before handle`。
        直接提交失败，以及排空里等到的 SubmitError：异常上已落账的句柄
        同样先 abandon，再抛 PrefetchError。池里的传输不取消。
        放不下（AdmissionWait）和段不存在（AdmissionStop）原样抛出，由闸外
        决定再等还是停止。
        进程退出（外层 CancelledError）若已经拿到这个句柄，先 abandon，
        再把取消抛出去，不改成 PrefetchError。
        """
        deadline = time.monotonic() + timeout_s
        copy_task = asyncio.create_task(self._ops.copy(prefix, target))
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            # shield：超时取消的是这次等待，不是 copy。copy 若在这里被
            # 取消并交回句柄，wait_for 会把句柄当成提交成功。真正取消
            # copy 的是下面的 _drain_copy。不 shield 在飞的建链 RPC。
            handle = await asyncio.wait_for(
                asyncio.shield(copy_task), timeout=remaining
            )
        except SubmitError as e:
            await self._abandon(e.handle)
            raise PrefetchError(f"copy submit failed: {e}") from e
        except TimeoutError:
            handle = None
            try:
                handle = await _drain_copy(copy_task)
            except SubmitError as e:
                await self._abandon(e.handle)
                raise PrefetchError(f"copy submit failed: {e}") from e
            except asyncio.CancelledError:
                await self._abandon_if_handed(copy_task, handle)
                raise
            await self._abandon(handle)
            if handle is not None:
                raise PrefetchError(
                    f"copy not READY after {timeout_s}s (handle={handle})"
                )
            raise PrefetchError(
                f"copy not READY after {timeout_s}s (cancelled before handle)"
            )
        except asyncio.CancelledError:
            # 进程退出。句柄交回来之后先 abandon，再把取消抛出去。
            handle = None
            try:
                handle = await _drain_copy(copy_task)
            except asyncio.CancelledError:
                await self._abandon_if_handed(copy_task, handle)
                raise
            await self._abandon(handle)
            raise
        if handle_sink is not None:
            handle_sink(handle)
        try:
            while True:
                try:
                    status = await self._ops.query_task(handle)
                except VerifyError as e:
                    # 任务完成但副本不到位：终态，带明细
                    raise PrefetchError(f"replica verify failed: {e}") from e
                except QueryError:
                    # 查询 RPC 失败：可重试，超时兜底。只捕获 QueryError——
                    # 编程错误（KeyError/TypeError 等）直接传播，不得吞成超时。
                    status = None
                if status is TaskStatus.READY:
                    return
                if status is TaskStatus.FAILED:
                    raise PrefetchError(f"copy task failed (handle={handle})")
                if time.monotonic() >= deadline:
                    # 超时收口：放弃跟踪（删登记 + 记 abandoned）；后端任务
                    # 可能仍在进行，副本清理由 remove 负责。
                    await self._abandon(handle)
                    raise PrefetchError(
                        f"copy not READY after {timeout_s}s (handle={handle})"
                    )
                await asyncio.sleep(poll_s)
        except asyncio.CancelledError:
            # 进程退出取消：断开跟踪并记账（Ledger 记 abandoned）。
            # 不这样做的话账本会留 running，重启又被 rebuild 捞回来跟。
            # 后端任务本身不取消（无此能力），副本清理由 remove 负责。
            await self._abandon(handle)
            raise

    async def _abandon(self, handle: TaskHandle | None) -> None:
        """放弃跟踪。池里的传输不取消。

        句柄已经不在（KeyError）忽略。账本写失败（sqlite，或退出时
        线程池已关）记日志后返回，调用方照旧抛自己的结局。编程错误
        继续抛。
        """
        if handle is None:
            return
        try:
            await self._ops.abandon(handle)
        except asyncio.CancelledError:
            raise
        except KeyError:
            pass
        except sqlite3.Error:
            logger.exception("abandon ledger write failed (handle=%s)", handle)
        except RuntimeError as e:
            if _LEDGER_SHUTDOWN not in str(e):
                raise
            logger.exception("abandon ledger write failed (handle=%s)", handle)

    async def _abandon_if_handed(
        self, copy_task: asyncio.Task, handle: TaskHandle | None
    ) -> None:
        """关闭信号打断排空时，已经交回来的句柄先 abandon。"""
        if handle is None and copy_task.done() and not copy_task.cancelled():
            if copy_task.exception() is None:
                result = copy_task.result()
                if isinstance(result, TaskHandle):
                    handle = result
        await self._abandon(handle)


async def _drain_copy(copy_task: asyncio.Task) -> TaskHandle | None:
    """取消还在跑的 copy，并把收尾结果交给调用方。

    有任务号时 copy 返回句柄；没有任务号时它以 CancelledError 结束。
    只有「子任务自己以 CancelledError 结束」且当前任务
    `cancelling() == 0` 时才吞掉。`cancelling() > 0`（进程正在关闭）
    继续抛出，不把这次等待罩进最长 30 秒的 RPC。
    """
    # 已经在取消中就不要再 cancel 一次：第二次会把「return 句柄」
    # 丢掉，调用方拿不到、也就 abandon 不了。
    if not copy_task.done() and copy_task.cancelling() == 0:
        copy_task.cancel()
    try:
        return await copy_task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling() > 0:
            raise
        return None
