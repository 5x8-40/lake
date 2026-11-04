"""PromoteFlow：触发提回并轮询到目标出现内存副本。

promote 原子操作只负责打 N 次访问触发 mooncake 内部 promotion；
是否完成由本流程用 ``check_promote`` 轮询判定。
"""

from __future__ import annotations

import asyncio
import logging
import time

from kvcm.ops.base import KvOps, QueryError
from kvcm.ops.types import InstanceId, Prefix

logger = logging.getLogger(__name__)


class PromoteError(RuntimeError):
    """提回失败（前缀不在池 / 超时）。"""


class PromoteFlow:
    """提回流程。无状态，全部操作经注入的原子操作接口。"""

    def __init__(self, ops: KvOps) -> None:
        self._ops = ops

    async def run(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 30.0,
    ) -> None:
        """触发并等到 target 上出现全部 key 的 memory replica。

        ``poll_s`` 是轮询间隔，``timeout_s`` 是总等待预算；等待期间
        只挂起协程，不占线程。查询失败按 QueryError 重试到 deadline。
        """
        if poll_s <= 0:
            raise ValueError("poll_s must be positive")
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        if not await self._ops.exists(prefix):
            raise PromoteError(
                "prefix not fully in pool — 没有可提回的副本"
            )
        await self._ops.promote(prefix, target)
        deadline = time.monotonic() + timeout_s
        last_error: QueryError | None = None
        while True:
            try:
                if await self._ops.check_promote(prefix, target):
                    return
            except QueryError as e:
                if last_error is None:
                    logger.warning(
                        "check_promote failed; retrying (prefix=%s target=%s): %s",
                        prefix,
                        target,
                        e,
                    )
                last_error = e
            if time.monotonic() >= deadline:
                detail = f"; last query error: {last_error}" if last_error else ""
                raise PromoteError(
                    f"promote timeout after {timeout_s}s{detail}"
                )
            await asyncio.sleep(poll_s)
