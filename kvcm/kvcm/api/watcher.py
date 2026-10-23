"""InstanceWatcher：推理实例上线/销毁事件接入（下行通道）。

- 新实例启动 → 触发热点前缀预热
- 实例销毁 → 该实例持有的副本的清理/归属处理

同时是实例注册的权威来源（生产环境的实例发现取代 demo 的
pidfile/proctitle 探测：适配层的拓扑子件把数据源换到这里的注册即可，
本层不出现任何存储概念）。事件源选型（K8s watch / 部署系统通知）随
设计定型；本步仅占位接口形态。
"""

from __future__ import annotations

from typing import Callable

from kvcm.ops.types import InstanceId


class InstanceWatcher:
    """实例事件入口。注册回调，由进程壳驱动（事件源接线后）。"""

    def __init__(self) -> None:
        self._on_up: Callable[[InstanceId], None] | None = None
        self._on_down: Callable[[InstanceId], None] | None = None

    def on_instance_up(self, handler: Callable[[InstanceId], None]) -> None:
        self._on_up = handler

    def on_instance_down(self, handler: Callable[[InstanceId], None]) -> None:
        self._on_down = handler

    def start(self) -> None:
        """开始监听（事件源接线后实现）。"""
        raise NotImplementedError("随事件源选型接线")
