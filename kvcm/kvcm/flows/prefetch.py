"""PrefetchFlow：预热编排——把已有前缀的 KV 预复制到目标实例本机层级。

编排步骤（只调原子操作层）：
1. 前置检查：前缀完整在池（warm 未覆盖或 key 口径漂移则 fail fast，
   复制无意义）
2. copy 发起（返回任务句柄）
3. query_task 轮询到 READY
4. 保活注册与 READY 事件补发随后续接线（保活落地后注册；补发归接口
   层 EventPublisher）

任务状态同步轮询是本步形态；进程化后由 TaskEngine 跟踪（并发上限 +
限速器），流程本体不变。失败处理：copy 失败即抛——重发 / 弃置清副本 /
源掉线衔接的策略归 TaskEngine。
"""

from __future__ import annotations

import time

from kvcm.ledger import Ledger
from kvcm.ops.base import KvOps, TaskStatus
from kvcm.ops.types import InstanceId, Prefix


class PrefetchError(RuntimeError):
    """预热失败（前置检查不过 / 任务失败 / 超时）。"""


class PrefetchFlow:
    """预热流程。无状态，全部操作经注入的原子操作接口。"""

    def __init__(self, ops: KvOps, ledger: Ledger | None = None) -> None:
        self._ops = ops
        self._ledger = ledger

    def run(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 120.0,
    ) -> None:
        """同步执行到 READY；失败抛 PrefetchError。"""
        if not self._ops.exists(prefix):
            raise PrefetchError(
                "prefix not fully in pool — warm 未覆盖该前缀，或 key 口径与引擎漂移"
            )
        handle = self._ops.copy(prefix, target)
        if self._ledger is not None:
            for task_id in handle.task_ids:
                self._ledger.upsert_task(
                    task_id, op=handle.op, prefix=prefix, target=str(target), status="running"
                )
        deadline = time.monotonic() + timeout_s
        while True:
            status = self._ops.query_task(handle)
            if status is TaskStatus.READY:
                if self._ledger is not None:
                    for task_id in handle.task_ids:
                        self._ledger.upsert_task(
                            task_id, op=handle.op, prefix=prefix, target=str(target), status="ready"
                        )
                return
            if status is TaskStatus.FAILED:
                if self._ledger is not None:
                    for task_id in handle.task_ids:
                        self._ledger.upsert_task(
                            task_id, op=handle.op, prefix=prefix, target=str(target), status="failed"
                        )
                raise PrefetchError(f"copy task failed (handle={handle})")
            if time.monotonic() >= deadline:
                raise PrefetchError(
                    f"copy not READY after {timeout_s}s (handle={handle})"
                )
            time.sleep(poll_s)
