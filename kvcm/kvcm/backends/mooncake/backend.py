"""MooncakeBackend：原子操作接口的 mooncake 实现。

组装两个适配维度的子件：
- 存储侧：StoreClient（绑定 + HTTP 双通道）、SegTopology、能力集（按版本）
- 引擎侧：KeyComputer（前缀 → key 列表）、rank 拓扑（实例 → seg 列表，
  以 locate 可调用注入——demo 环境 = pidfile/proctitle 探测，生产 = 实
  例注册）

本步落地 copy（唯一经端到端验证的操作）；其余操作随能力接线逐个落地，
未落地的抛 NotImplementedError。
"""

from __future__ import annotations

from typing import Callable, Sequence

from kvcm.backends.mooncake.capability import mooncake_main
from kvcm.backends.mooncake.client import StoreClient
from kvcm.engines.vllm.keys import KeyComputer, group_keys_by_rank, parse_key_rank
from kvcm.ops.base import KvOps, TaskHandle, TaskStatus
from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, PinKind, Prefix


class MooncakeBackend(KvOps):
    """mooncake 后端的原子操作实现。"""

    def __init__(
        self,
        *,
        client: StoreClient,
        key_computer: KeyComputer,
        locate: Callable[[InstanceId], Sequence[str]],
        capability: BackendCapability | None = None,
    ) -> None:
        """
        client：已 connect 的 StoreClient（控制面身份，global_segment_size=0）。
        key_computer：引擎侧 key 计算器（持展开配置）。
        locate：实例 → rank 有序 seg 列表（targets[i] = rank i 的本机 seg）。
        capability：按后端版本填充的能力集；缺省取目标基线（main）。
        """
        self._client = client
        self._keys = key_computer
        self._locate = locate
        self._cap = capability or mooncake_main()
        # handle → (keys, plan)：READY 核验登记（任务状态机；进程内，
        # 持久化归状态层）。plan = [(key, target_seg), ...]
        self._plans: dict[TaskHandle, tuple[list[str], list[tuple[str, str]]]] = {}

    def capability(self) -> BackendCapability:
        return self._cap

    # ------------------------------------------------------------------
    # copy（已落地）
    # ------------------------------------------------------------------

    def _build_plan(self, keys: list[str], targets: list[str]) -> list[tuple[str, str]]:
        """key → 目标 seg 的映射计划：rank i 的 key → targets[i]。

        异构反向（目标 rank 数 < key 展开的 effective_tp）：eff rank i 归
        targets[i // n_sub]（seg 列表按倍数展开）；其余超界组合直接报错。
        """
        by_rank = group_keys_by_rank(keys)
        max_rank = max(by_rank) if by_rank else -1
        if len(targets) == 1 and max_rank <= 0:
            return [(k, targets[0]) for k in keys]
        if max_rank >= len(targets):
            local_tp = self._keys.tp_size
            peer_tp = self._keys.peer_tp_size
            eff = max(local_tp, peer_tp or 0)
            n_sub = eff // local_tp if local_tp else 1
            if (
                peer_tp
                and peer_tp > local_tp
                and local_tp > 0
                and eff % local_tp == 0
                and len(targets) == local_tp
                and max_rank == eff - 1
            ):
                targets = [targets[i // n_sub] for i in range(eff)]
            else:
                raise ValueError(
                    f"key rank max={max_rank} but only {len(targets)} targets "
                    f"(need targets[0..{max_rank}])"
                )
        unknown = [r for r in by_rank if r < 0 or r >= len(targets)]
        if unknown:
            raise ValueError(f"ranks out of range for targets: {unknown}")
        return [(k, targets[parse_key_rank(k)]) for k in keys]

    def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """在目标实例本机 seg 为前缀 KV 建副本（发起式；READY 经 query_task）。

        内部展开：前缀 → key 列表（引擎侧）→ 目标实例 seg 列表（locate）
        → rank 映射计划 → 逐 key 发起复制任务。「复制前确认 warm 已覆盖前
        缀」的存在性检查归编排层，不在本操作内。
        """
        keys = self._keys.keys_for_tokens(prefix.token_ids)
        targets = list(self._locate(target))
        plan = self._build_plan(keys, targets)
        task_ids = tuple(
            str(self._client.create_copy_task(key, [seg])) for key, seg in plan
        )
        handle = TaskHandle(op="copy", task_ids=task_ids)
        self._plans[handle] = (keys, plan)
        return handle

    def query_task(self, handle: TaskHandle) -> TaskStatus:
        """聚合判定：任一任务失败 → FAILED；全部成功 + 副本核验通过 →
        READY；核验不过 → FAILED；否则 RUNNING。"""
        from mooncake.store import TaskStatus as StoreTaskStatus

        entry = self._plans.get(handle)
        if entry is None:
            raise KeyError(f"unknown task handle: {handle}")
        keys, plan = entry
        statuses = [self._client.query_task(tid).status for tid in handle.task_ids]
        if any(s == StoreTaskStatus.FAILED for s in statuses):
            return TaskStatus.FAILED
        if not all(s == StoreTaskStatus.SUCCESS for s in statuses):
            return TaskStatus.RUNNING
        # READY 核验：每个 key 在其映射目标上有内存副本（不是每个 seg）
        try:
            for key, seg in plan:
                self._client.assert_targets_have_replicas([key], [seg])
        except Exception:
            return TaskStatus.FAILED
        return TaskStatus.READY

    def exists(self, prefix: Prefix) -> bool:
        """全部 key 在池才为真（batch_is_exist；读即续租）。"""
        keys = self._keys.keys_for_tokens(prefix.token_ids)
        if not keys:
            return False
        return all(self._client.is_exist(keys))

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
        raise NotImplementedError("pin：事后通道待自封装 .so（能力集 soft_pin_after）")

    def demote(self, prefix: Prefix) -> None:
        raise NotImplementedError("demote：随释放流程落地")

    def promote(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        raise NotImplementedError("promote：随提回流程落地")

    def retain(self, prefix: Prefix) -> None:
        raise NotImplementedError("retain：随保留流程落地（能力集 has_retained_client）")

    def remove(self, prefix: Prefix) -> None:
        raise NotImplementedError("remove：随删除流程落地")
