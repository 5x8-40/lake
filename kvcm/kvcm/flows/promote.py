"""PromoteFlow：触发池提回；可选把 memory 落到指定实例。

L3 ``promote`` 只触发、不带 target。本 flow 的完成条件：

- 不传 ``target``：每 key 至少一处 memory；``memory_instance`` 反查
  持有方（可空；反查失败仍算提回完成，但 ``ready=False``）。
- 传入 ``target``：``check_promote(prefix, target)`` 为真。已在则返回；
  别处已有 memory 则 ``copy`` 后再校验；还没有 memory（常见于仅盘
  副本）则先 L3 提回、等到任意 memory，再 ``copy``，再校验。copy 的
  任务 READY 不够，必须以 ``check_promote(target)`` 为准。盘副本不能
  当 copy 源。

D2 READY（可调度）= ``PromoteResult.ready``：``instance`` 非空且核验
通过。不是 ``TaskHandle`` / ``TaskStatus``——L3 promote 无任务号。

``check_promote`` 的 ``QueryError`` 当未就绪（含 copy 后核验）。
``IntentInputError`` 原样上抛。``AdmissionWait``：库直调 Prefetch 时原样
上抛；TaskEngine 注入带闸 prefetch 时由闸循环消化，不记 intent 失败。
copy 默认 ``PrefetchFlow``；生产注入闸路径（只挡真要 copy 之前）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kvcm.flows.prefetch import PrefetchError, PrefetchFlow
from kvcm.ops.base import KvOps, QueryError
from kvcm.ops.types import InstanceId, Prefix

logger = logging.getLogger(__name__)

# (prefix, target, *, poll_s, timeout_s) -> None
PrefetchFn = Callable[..., Awaitable[None]]


class PromoteError(RuntimeError):
    """提回失败（前缀不在池 / 超时 / 搬到 target 失败）。"""


@dataclass(frozen=True)
class PromoteResult:
    """提回完成；``ready`` 为真时表示 D2 READY（可把请求调到 ``instance``）。

    - 传了 ``target``：成功则 ``ready=True``，``instance`` 为该 target。
    - 不传 ``target``：池内已有 memory 即返回；反查到持有方则
      ``ready=True``，否则 ``ready=False``（有 memory、目录对不上，
      不能上报调度）。
    """

    instance: InstanceId | None = None
    ready: bool = False

    @classmethod
    def schedulable(cls, instance: InstanceId) -> PromoteResult:
        return cls(instance=instance, ready=True)

    @classmethod
    def unschedulable(cls) -> PromoteResult:
        """提回完成但无持有方，不上报 READY。"""
        return cls(instance=None, ready=False)

    @classmethod
    def from_holder(cls, instance: InstanceId | None) -> PromoteResult:
        if instance is None:
            return cls.unschedulable()
        return cls.schedulable(instance)


class PromoteFlow:
    """提回流程。无状态，全部操作经注入的原子操作接口。"""

    def __init__(
        self, ops: KvOps, *, prefetch: PrefetchFn | None = None
    ) -> None:
        self._ops = ops
        self._prefetch = prefetch

    async def run(
        self,
        prefix: Prefix,
        *,
        target: InstanceId | None = None,
        poll_s: float = 0.5,
        timeout_s: float = 30.0,
    ) -> PromoteResult:
        """等到提回完成；``ready`` 见 ``PromoteResult``。

        ``timeout_s`` 覆盖提回等待与（若有）copy。带 ``target`` 时预算须
        够 copy（intent 常用 120s）；默认 30s 偏紧。
        """
        if poll_s <= 0:
            raise ValueError("poll_s must be positive")
        if timeout_s < 0:
            raise ValueError("timeout_s must be non-negative")
        if not await self._ops.exists(prefix):
            raise PromoteError(
                "prefix not fully in pool — 没有可提回的副本"
            )
        if target is not None:
            return await self._run_to_target(
                prefix, target, poll_s=poll_s, timeout_s=timeout_s
            )
        return await self._run_any(prefix, poll_s=poll_s, timeout_s=timeout_s)

    async def _run_any(
        self, prefix: Prefix, *, poll_s: float, timeout_s: float
    ) -> PromoteResult:
        try:
            has_memory = await self._ops.check_promote(prefix)
        except QueryError as e:
            logger.warning(
                "check_promote failed before trigger (prefix=%s): %s",
                prefix,
                e,
            )
            has_memory = False
        if has_memory:
            return PromoteResult.from_holder(
                await self._resolve_instance(prefix)
            )
        await self._ops.promote(prefix)
        await self._wait_any_memory(
            prefix, poll_s=poll_s, timeout_s=timeout_s
        )
        return PromoteResult.from_holder(await self._resolve_instance(prefix))

    async def _run_to_target(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float,
        timeout_s: float,
    ) -> PromoteResult:
        deadline = time.monotonic() + timeout_s
        try:
            if await self._ops.check_promote(prefix, target):
                return PromoteResult.schedulable(target)
        except QueryError as e:
            logger.warning(
                "check_promote(target) failed (prefix=%s target=%s): %s",
                prefix,
                target,
                e,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PromoteError(
                f"promote to target timeout after {timeout_s}s "
                f"(target={target})"
            )

        try:
            has_memory = await self._ops.check_promote(prefix)
        except QueryError as e:
            logger.warning(
                "check_promote failed before ensure-memory "
                "(prefix=%s): %s",
                prefix,
                e,
            )
            has_memory = False
        if not has_memory:
            await self._ops.promote(prefix)
            await self._wait_any_memory(
                prefix,
                poll_s=poll_s,
                timeout_s=remaining,
                reported_timeout_s=timeout_s,
                target=target,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PromoteError(
                f"promote to target timeout after {timeout_s}s "
                f"(before copy; target={target})"
            )
        try:
            await self._copy_to_target(
                prefix,
                target,
                poll_s=poll_s,
                timeout_s=remaining,
            )
        except PrefetchError as e:
            raise PromoteError(
                f"promote to target copy failed (target={target}): {e}"
            ) from e
        # AdmissionWait / IntentInputError / 其它：原样上抛
        await self._wait_on_target(
            prefix,
            target,
            poll_s=poll_s,
            deadline=deadline,
            timeout_s=timeout_s,
        )
        return PromoteResult.schedulable(target)

    async def _copy_to_target(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float,
        timeout_s: float,
    ) -> None:
        if self._prefetch is not None:
            await self._prefetch(
                prefix, target, poll_s=poll_s, timeout_s=timeout_s
            )
            return
        await PrefetchFlow(self._ops).run_after_exists(
            prefix,
            target,
            poll_s=poll_s,
            timeout_s=timeout_s,
        )

    async def _wait_on_target(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float,
        deadline: float,
        timeout_s: float,
    ) -> None:
        """copy 后以 ``check_promote(target)`` 为准；QueryError 当未就绪。"""
        last_error: QueryError | None = None
        while True:
            try:
                on_target = await self._ops.check_promote(prefix, target)
            except QueryError as e:
                if last_error is None:
                    logger.warning(
                        "check_promote(target) after copy failed; "
                        "retrying (prefix=%s target=%s): %s",
                        prefix,
                        target,
                        e,
                    )
                last_error = e
                on_target = None
            if on_target is True:
                return
            if on_target is False:
                raise PromoteError(
                    f"promote to target: copy READY but check_promote "
                    f"missed target={target}"
                )
            if time.monotonic() >= deadline:
                detail = (
                    f"; last query error: {last_error}" if last_error else ""
                )
                raise PromoteError(
                    f"promote to target verify timeout after {timeout_s}s "
                    f"(target={target}){detail}"
                )
            left = deadline - time.monotonic()
            await asyncio.sleep(min(poll_s, left) if left > 0 else 0)

    async def _wait_any_memory(
        self,
        prefix: Prefix,
        *,
        poll_s: float,
        timeout_s: float,
        reported_timeout_s: float | None = None,
        target: InstanceId | None = None,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        label = (
            reported_timeout_s if reported_timeout_s is not None else timeout_s
        )
        last_error: QueryError | None = None
        while True:
            try:
                has_memory = await self._ops.check_promote(prefix)
            except QueryError as e:
                if last_error is None:
                    logger.warning(
                        "check_promote failed; retrying (prefix=%s): %s",
                        prefix,
                        e,
                    )
                last_error = e
                has_memory = False
            if has_memory:
                return
            if time.monotonic() >= deadline:
                detail = (
                    f"; last query error: {last_error}" if last_error else ""
                )
                where = f" (target={target})" if target is not None else ""
                raise PromoteError(
                    f"promote timeout after {label}s{where}{detail}"
                )
            await asyncio.sleep(poll_s)

    async def _resolve_instance(self, prefix: Prefix) -> InstanceId | None:
        """反查持有方；失败返回 None（提回仍完成，ready=False）。"""
        try:
            return await self._ops.memory_instance(prefix)
        except QueryError as e:
            logger.warning(
                "memory_instance failed after promote (prefix=%s): %s",
                prefix,
                e,
            )
            return None
