"""MooncakeBackend：原子操作接口的 mooncake 实现。

组装两个适配维度的子件：
- 存储侧：StoreClient（绑定 + HTTP 双通道）、SegTopology、能力集（按版本）
- 引擎侧：KeyComputer（前缀 → key 列表 → rank 映射计划）、实例 → seg
  列表（以 locate 可调用注入——demo 环境 = pidfile/proctitle 探测，生产
  = 实例注册）

任务标识（mooncake 为整数对）在本实现内原样透传，不做字符串化；需要
写入 Ledger 时由本实现做可逆编码（"hi:lo"）。

本步落地 copy / exists / query_task；其余操作随能力接线逐个落地，未落
地的抛 NotImplementedError，通道不具备的抛 UnsupportedCapabilityError。
"""

from __future__ import annotations

from typing import Callable, Sequence

from kvcm.backends.mooncake.client import StoreClient, StoreClientError
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger
from kvcm.ops.base import (
    KvOps,
    OpKind,
    TaskHandle,
    TaskStatus,
    UnsupportedCapabilityError,
    VerifyError,
)
from kvcm.ops.capability import BackendCapability, Support
from kvcm.ops.types import InstanceId, PinKind, Prefix


def encode_task_id(task_id: object) -> str:
    """任务标识 → 可逆字符串编码（Ledger 持久化用）。

    mooncake 任务号为 (uint64, uint64) 整数对，编码为 "hi:lo"；其他形态
    兜底 str()。解码不在本步（任务状态机可丢，重启后从 master 重查）。
    """
    if isinstance(task_id, (tuple, list)) and len(task_id) == 2:
        try:
            return f"{int(task_id[0])}:{int(task_id[1])}"
        except (TypeError, ValueError):
            pass
    return str(task_id)


class MooncakeBackend(KvOps):
    """mooncake 后端的原子操作实现。"""

    def __init__(
        self,
        *,
        client: StoreClient,
        key_computer: KeyComputer,
        locate: Callable[[InstanceId], Sequence[str]],
        capability: BackendCapability,
        ledger: Ledger | None = None,
    ) -> None:
        """
        client：已 connect 的 StoreClient（控制面身份，global_segment_size=0）。
        key_computer：引擎侧 key 计算器（持展开配置与 rank 映射知识）。
        locate：实例 → rank 有序 seg 列表（targets[i] = rank i 的本机 seg）。
        capability：按连接上的后端版本填充的能力集（必填——能力宣告不
            允许默认冒充，填错版本 = 编排层选错路径）。
        ledger：任务状态记录（可选；不注入则任务状态只留进程内）。
        """
        self._client = client
        self._keys = key_computer
        self._locate = locate
        self._cap = capability
        self._ledger = ledger
        # handle → (prefix, target, plan [(key, target_seg), ...])：
        # READY 核验与 Ledger 迁移登记；终态即删（防长驻泄漏）。
        self._plans: dict[TaskHandle, tuple[Prefix, InstanceId, list[tuple[str, str]]]] = {}

    def capability(self) -> BackendCapability:
        return self._cap

    # ------------------------------------------------------------------
    # copy（已落地）
    # ------------------------------------------------------------------

    def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """在目标实例本机 seg 为前缀 KV 建副本（发起式；READY 经 query_task）。

        内部展开：前缀 → key 列表（引擎侧）→ 目标实例 seg 列表（locate）
        → rank 映射计划（引擎侧）→ 逐 key 发起复制任务。「复制前确认
        warm 已覆盖前缀」的存在性检查归编排层，不在本操作内。
        """
        keys = self._keys.keys_for_tokens(prefix.token_ids)
        if not keys:
            raise ValueError(
                "prefix expands to no keys (shorter than one block?)"
            )
        targets = list(self._locate(target))
        plan = self._keys.build_plan(keys, targets)
        task_ids: list[object] = []
        try:
            for key, seg in plan:
                task_ids.append(self._client.create_copy_task(key, [seg]))
        except Exception as e:
            # 已发起的任务必须带在异常上——否则这些任务无句柄可查，
            # 成为池里没人跟踪的在途传输。
            raise StoreClientError(
                f"copy submit failed after {len(task_ids)}/{len(plan)} tasks "
                f"(submitted ids: {task_ids}): {e}"
            ) from e
        handle = TaskHandle(op=OpKind.COPY, task_ids=tuple(task_ids))
        self._plans[handle] = (prefix, target, plan)
        self._record(handle, "running")
        return handle

    def query_task(self, handle: TaskHandle) -> TaskStatus:
        """聚合判定：任一任务失败 → FAILED（终态）；全部成功且副本核验
        通过 → READY（终态）；否则 RUNNING。终态即删核验登记。

        查询本身的 RPC 失败直接上抛（调用方可重试，不是任务终态）；
        副本核验失败（任务白跑）抛 VerifyError（终态，带明细）。
        """
        from mooncake.store import TaskStatus as StoreTaskStatus

        entry = self._plans.get(handle)
        if entry is None:
            raise KeyError(f"unknown task handle: {handle}")
        _, _, plan = entry
        statuses = [self._client.query_task(tid).status for tid in handle.task_ids]
        if any(s == StoreTaskStatus.FAILED for s in statuses):
            self._finish(handle, "failed")
            return TaskStatus.FAILED
        if not all(s == StoreTaskStatus.SUCCESS for s in statuses):
            return TaskStatus.RUNNING
        # READY 核验：每个 key 在其映射目标上有内存副本（不是每个 seg）
        missing: list[str] = []
        for key, seg in plan:
            endpoints = self._client.batch_memory_replica_endpoints([key]).get(key, [])
            if seg not in endpoints:
                missing.append(f"{key} missing on {seg} (have={sorted(endpoints)})")
        if missing:
            self._finish(handle, "failed")
            raise VerifyError(
                "task(s) succeeded but replica verify failed:\n  "
                + "\n  ".join(missing)
            )
        self._finish(handle, "ready")
        return TaskStatus.READY

    def exists(self, prefix: Prefix) -> bool:
        """全部 key 在池才为真（batch_is_exist；读即续租）。

        返回列表长度与 key 数不一致视为不存在（绑定异常时可能短回）。
        """
        keys = self._keys.keys_for_tokens(prefix.token_ids)
        if not keys:
            return False
        result = self._client.is_exist(keys)
        return len(result) == len(keys) and all(result)

    # ------------------------------------------------------------------
    # 任务状态记录（归实现侧，不归编排层）
    #
    # 记这笔账需要两样东西都齐：①任务号的持久化编码——mooncake 任务号
    # 是整数对，编码知识只在本层，编排层不该懂；②状态迁移的时机——
    # query_task 是本层判定的，顺手迁移。编排层记账则两头都缺：编码要
    # 本层翻译好再上交，路径一多还会漏（超时、异常分支没人写）。原则：
    # 谁手里有原始信息，谁就记账。（状态机可丢，重启后从 master 重查）
    # ------------------------------------------------------------------

    def _record(self, handle: TaskHandle, status: str) -> None:
        if self._ledger is None:
            return
        entry = self._plans.get(handle)
        if entry is None:
            return
        prefix, target, _ = entry
        for tid in handle.task_ids:
            self._ledger.upsert_task(
                encode_task_id(tid),
                op=handle.op.value,
                prefix=prefix,
                target=str(target),
                status=status,
            )

    def _finish(self, handle: TaskHandle, status: str) -> None:
        """终态处理：Ledger 迁移 + 删核验登记（防长驻进程泄漏）。"""
        self._record(handle, status)
        self._plans.pop(handle, None)

    # ------------------------------------------------------------------
    # 以下操作随能力接线逐个落地
    # ------------------------------------------------------------------

    def move(
        self,
        prefix: Prefix,
        target: InstanceId,
        source: InstanceId | None = None,
    ) -> TaskHandle:
        raise NotImplementedError("move：随保留/搬迁流程落地")

    def keepalive(self, prefix: Prefix) -> None:
        raise NotImplementedError("keepalive：随保活注册表落地")

    def stop_keepalive(self, prefix: Prefix) -> None:
        raise NotImplementedError("stop_keepalive：随保活注册表落地")

    def pin(self, prefix: Prefix, kind: PinKind = PinKind.SOFT) -> None:
        if kind is PinKind.HARD or self._cap.hard_pin_after.channel is not Support.YES:
            if kind is PinKind.HARD:
                raise UnsupportedCapabilityError(
                    "hard pin 写入时固化，已存在的 KV 无法事后补标（全版本无此能力）"
                )
            raise UnsupportedCapabilityError(
                "soft pin 事后加：通道不具备（自封装 .so 接线前）"
            )
        raise NotImplementedError("pin：事后通道待自封装 .so")

    def demote(self, prefix: Prefix) -> None:
        raise NotImplementedError("demote：随释放流程落地")

    def promote(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        raise NotImplementedError("promote：随提回流程落地")

    def retain(self, prefix: Prefix) -> None:
        raise NotImplementedError("retain：随保留流程落地（能力集 has_retained_client）")

    def remove(self, prefix: Prefix) -> None:
        raise NotImplementedError("remove：随删除流程落地")
