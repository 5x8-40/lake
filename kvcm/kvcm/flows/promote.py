"""PromoteFlow：触发池提回，等到每 key 至少一处 memory。

L3 ``promote`` 只触发；完成看 ``check_promote``（不限实例；查询有授
lease / 命中副作用）。提回前已有 memory 则直接成功、不触发。落点由
``memory_instance`` 反查（可空；反查 ``QueryError`` 不影响成功）。

指定机器：已有 memory 直接 ``copy``；仅盘副本时先本 flow 提回盘主
memory，再 ``copy``（LOCAL_DISK 不能当 copy 源）。
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
    """提回完成。``instance`` 为 ``build_plan`` 反查到的可调度持有方；
    目录对不上或反查失败则为 None（提回仍算成功）。"""

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
        只挂起协程，不占线程。``check_promote`` 的 ``QueryError`` 重试到
        deadline；``memory_instance`` 的 ``QueryError`` 记 warning 并返回
        ``instance=None``，不重试、不超时。
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
            ready = await self._ops.check_promote(prefix)
        except QueryError as e:
            logger.warning(
                "check_promote failed before trigger (prefix=%s): %s",
                prefix,
                e,
            )
            ready = False
        if ready:
            return PromoteResult(await self._resolve_instance(prefix))
        await self._ops.promote(prefix)
        deadline = time.monotonic() + timeout_s
        last_error: QueryError | None = None
        while True:
            try:
                ready = await self._ops.check_promote(prefix)
            except QueryError as e:
                if last_error is None:
                    logger.warning(
                        "check_promote failed; retrying (prefix=%s): %s",
                        prefix,
                        e,
                    )
                last_error = e
                ready = False
            if ready:
                return PromoteResult(await self._resolve_instance(prefix))
            if time.monotonic() >= deadline:
                detail = f"; last query error: {last_error}" if last_error else ""
                raise PromoteError(
                    f"promote timeout after {timeout_s}s{detail}"
                )
            await asyncio.sleep(poll_s)

    async def _resolve_instance(self, prefix: Prefix) -> InstanceId | None:
        """反查持有方；失败不影响提回成功（返回 None）。"""
        try:
            return await self._ops.memory_instance(prefix)
        except QueryError as e:
            logger.warning(
                "memory_instance failed after promote ready (prefix=%s): %s",
                prefix,
                e,
            )
            return None
