"""PrefetchFlow：预热编排——把已有前缀的 KV 预复制到目标实例本机层级。

编排步骤（只调原子操作层）：
1. 前置检查：前缀完整在池（warm 未覆盖或 key 口径漂移则 fail fast，
   复制无意义）
2. copy 发起（返回任务句柄）
3. query_task 轮询到 READY
4. 保活注册与 READY 事件补发随后续接线（保活落地后注册；补发归接口
   层 EventPublisher）

失败分类（统一出口 = PrefetchError；编程错误除外，直接传播）：
- 前置检查不过 / copy 提交失败（SubmitError，带已发任务号明细）——
  终态上抛
- 核验失败（VerifyError：任务完成但副本不到位）——终态，带明细上抛
- 查询失败（QueryError：RPC 错误）——可重试，继续轮询到超时兜底
- 任务 FAILED / 超时——终态上抛；超时先 abandon 收口（删登记 + 记
  abandoned），再抛

任务状态同步轮询是本步形态；进程化后由 TaskEngine 跟踪（并发上限 +
限速器），流程本体不变。重发 / 弃置清副本 / 源掉线衔接的策略归
TaskEngine。

任务状态记录（Ledger）由后端实现侧在 copy / query_task / abandon 时
登记迁移，本流程不直接写——记账需要任务号的持久化编码与状态迁移
的时机，两样都只在实现侧手里。
"""

from __future__ import annotations

import time

from kvcm.ops.base import KvOps, QueryError, SubmitError, TaskStatus, VerifyError
from kvcm.ops.types import InstanceId, Prefix


class PrefetchError(RuntimeError):
    """预热失败（前置检查不过 / 任务失败 / 核验失败 / 超时）。"""


class PrefetchFlow:
    """预热流程。无状态，全部操作经注入的原子操作接口。"""

    def __init__(self, ops: KvOps) -> None:
        self._ops = ops

    def run(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        poll_s: float = 0.5,
        timeout_s: float = 120.0,
    ) -> None:
        """同步执行到 READY；失败抛 PrefetchError。

        timeout_s 从 exists 通过起算、覆盖 copy 提交——大前缀逐 key 串行
        提交本身可达几十秒，不计入则超时预算失真。
        """
        if not self._ops.exists(prefix):
            raise PrefetchError(
                "prefix not fully in pool — warm 未覆盖该前缀，或 key 口径与引擎漂移"
            )
        deadline = time.monotonic() + timeout_s
        try:
            handle = self._ops.copy(prefix, target)
        except SubmitError as e:
            raise PrefetchError(f"copy submit failed: {e}") from e
        while True:
            try:
                status = self._ops.query_task(handle)
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
                self._ops.abandon(handle)
                raise PrefetchError(
                    f"copy not READY after {timeout_s}s (handle={handle})"
                )
            time.sleep(poll_s)
