"""进程内实例目录：实例 id → rank 有序的 seg 列表。

段和 rank 的对应登记在这里，不随命令带来。SubmitIntent、copy、move、
remove 的入参都没有 segment；调用方只给实例，操作内部自己 ``locate``。

``--instance-segs`` 只在进程拉起时做初始登记，不是每次操作换一套。
没有运行期入口再加实例。``locate(实例)`` 只返回该实例的段；
没登记就报错，不退回别人的段。

没有下线。实例销毁、事件发布不在这里。
"""

from __future__ import annotations

import threading
from typing import Sequence

from kvcm.ops.base import IntentInputError
from kvcm.ops.types import InstanceId


class InstanceDirectory:
    """实例 id 到本机 seg 名单的登记表。段的顺序就是 rank 顺序。"""

    def __init__(self) -> None:
        self._segs: dict[str, tuple[str, ...]] = {}
        # 启动登记和 locate 可能不在同一条线程上。锁只保护这张表。
        self._lock = threading.Lock()

    def register(self, instance: InstanceId | str, segs: Sequence[str]) -> None:
        """登记一个实例的 rank 有序 seg 列表。已登记的实例不能覆盖。"""
        inst = str(instance).strip()
        ordered = tuple(s.strip() for s in segs if s.strip())
        if not inst:
            raise ValueError("instance id is empty")
        if not ordered:
            raise ValueError(f"instance {inst!r} has no segments")
        with self._lock:
            if inst in self._segs:
                raise ValueError(f"instance already registered: {inst}")
            self._segs[inst] = ordered

    def locate(self, instance: InstanceId | str) -> tuple[str, ...]:
        """返回该实例的 seg 列表。没登记过抛 IntentInputError。"""
        inst = str(instance).strip()
        with self._lock:
            try:
                return self._segs[inst]
            except KeyError:
                raise IntentInputError(f"instance not registered: {inst}") from None


def parse_instance_segs(spec: str) -> tuple[str, tuple[str, ...]]:
    """解析 ``实例=seg0,seg1``。实例名和段名单都不能空。"""
    if "=" not in spec:
        raise ValueError(
            f"--instance-segs 需要 实例=seg0,seg1，收到 {spec!r}"
        )
    inst, rest = spec.split("=", 1)
    inst = inst.strip()
    segs = tuple(s.strip() for s in rest.split(",") if s.strip())
    if not inst or not segs:
        raise ValueError(
            f"--instance-segs 实例名和段名单都不能空：{spec!r}"
        )
    return inst, segs


def directory_from_specs(specs: Sequence[str]) -> InstanceDirectory:
    """按启动参数装一份目录。同一实例出现两次直接报错。"""
    if not specs:
        raise ValueError("至少要有一条 --instance-segs")
    directory = InstanceDirectory()
    for spec in specs:
        inst, segs = parse_instance_segs(spec)
        directory.register(inst, segs)
    return directory
