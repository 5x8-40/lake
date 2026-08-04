"""Worker 节点生命周期 + 容量信号（C10 骨架）。

状态机对齐 compute-layer「节点生命周期」：
Idle → Boot → Warm → Ready → Serving → Drain → Terminate。

过载 shedding 不在此——只上报队列/in-flight，供 gateway / Router 决策（原则 3）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class WorkerState(str, Enum):
    IDLE = "idle"
    BOOT = "boot"
    WARM = "warm"
    READY = "ready"
    SERVING = "serving"
    DRAIN = "drain"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class CapacitySignal:
    """推理系统过载职责：只上报，不自决限流。"""

    waiting: int
    running: int
    # Engine 侧已 submit、尚未 finished 的请求数（非 in-flight step 数）
    inflight_reqs: int
    max_running_reqs: int
    state: WorkerState
    role: str
    model_backend: str
    served_model_name: str = "model"
    model_loaded: bool = False
    model_warmed: bool = False

    @property
    def remaining_slots(self) -> int:
        """相对 max_running_reqs 的 running 余量（与 inflight_reqs 独立）。"""
        return max(0, self.max_running_reqs - self.running)


class WorkerLifecycle:
    """进程内状态机；Warm 权重 pin / Ready 接流量在此挂点（真加载仍后置）。"""

    def __init__(self, initial: WorkerState = WorkerState.IDLE) -> None:
        self._state = initial

    @property
    def state(self) -> WorkerState:
        return self._state

    def advance(self, to: WorkerState) -> None:
        order = [
            WorkerState.IDLE,
            WorkerState.BOOT,
            WorkerState.WARM,
            WorkerState.READY,
            WorkerState.SERVING,
            WorkerState.DRAIN,
            WorkerState.TERMINATE,
        ]
        if to not in order:
            raise ValueError(f"unknown state {to}")
        # 允许同态；前进或进入 Drain/Terminate
        if to == self._state:
            return
        if to in (WorkerState.DRAIN, WorkerState.TERMINATE):
            self._state = to
            return
        if order.index(to) < order.index(self._state):
            raise ValueError(f"cannot regress {self._state} → {to}")
        self._state = to

    def warm(self) -> None:
        """Warm：向存储池申请权重/热点 KV 放置（骨架仅推进状态）。"""
        if self._state == WorkerState.IDLE:
            self.advance(WorkerState.BOOT)
        self.advance(WorkerState.WARM)

    def ready(self) -> None:
        self.advance(WorkerState.READY)

    def serve(self) -> None:
        self.advance(WorkerState.SERVING)

    def drain(self) -> None:
        self.advance(WorkerState.DRAIN)

    def accepts_new_requests(self) -> bool:
        return self._state in (WorkerState.READY, WorkerState.SERVING)
