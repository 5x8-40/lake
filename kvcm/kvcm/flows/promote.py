"""PromoteFlow：触发池提回，等到每 key 至少一处 memory。

L3 ``promote`` 只触发；完成看 ``check_promote``（不限实例）。
提回前已有 memory 则直接成功、不触发。落点实例由 ``memory_instance``
反查，可空——指定机器用 copy。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from kvcm.ops.base import KvOps, QueryError
from kvcm.ops.types import InstanceId, Prefix

logger = logging.getLogger(__name__)


class PromoteError(RuntimeError):
    """提回失败（前缀不在池 / 超时）。"""


@dataclass(frozen=True)
class PromoteResult:
    """提回完成。``instance`` 为反查到的持有方，目录对不上则为 None。"""

    instance: InstanceId | None = None


class PromoteFlow:
    """提回流程。无状态，全部操作经注入的原子操作接口。"""

    def __init__(self, ops: KvOps) -> None:
        self._ops = ops

    async def run(
        self,
        prefix: Prefix,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 30.0,
    ) -> PromoteResult:
        """等到前缀每 key 至少一处 memory；已有则不触发。

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
        try:
            if await self._ops.check_promote(prefix):
                return PromoteResult(await self._ops.memory_instance(prefix))
        except QueryError as e:
            logger.warning(
                "check_promote failed before trigger (prefix=%s): %s",
                prefix,
                e,
            )
        await self._ops.promote(prefix)
        deadline = time.monotonic() + timeout_s
        last_error: QueryError | None = None
        while True:
            try:
                if await self._ops.check_promote(prefix):
                    return PromoteResult(
                        await self._ops.memory_instance(prefix)
                    )
            except QueryError as e:
                if last_error is None:
                    logger.warning(
                        "check_promote failed; retrying (prefix=%s): %s",
                        prefix,
                        e,
                    )
                last_error = e
            if time.monotonic() >= deadline:
                detail = f"; last query error: {last_error}" if last_error else ""
                raise PromoteError(
                    f"promote timeout after {timeout_s}s{detail}"
                )
            await asyncio.sleep(poll_s)
