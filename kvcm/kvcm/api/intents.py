"""IntentReceiver：接收 Router 预热 intent 与会话结束线索（下行通道）。

协议字段随设计定型后填充（通道候选 gRPC，与仓内跨语言 RPC 选型一致）；
本步仅占位接口形态。
"""

from __future__ import annotations

from typing import Callable

from kvcm.ops.types import InstanceId, Prefix


class IntentReceiver:
    """预热 intent 入口。注册回调，由进程壳驱动（随通信协议接线）。"""

    def __init__(self) -> None:
        self._on_prefetch: Callable[[Prefix, InstanceId], None] | None = None
        self._on_session_end: Callable[[str], None] | None = None

    def on_prefetch(self, handler: Callable[[Prefix, InstanceId], None]) -> None:
        """预热 intent 回调：（前缀，目标实例）。"""
        self._on_prefetch = handler

    def on_session_end(self, handler: Callable[[str], None]) -> None:
        """会话结束线索回调：（会话标识）。"""
        self._on_session_end = handler

    def start(self) -> None:
        """开始接收（通信协议接线后实现）。"""
        raise NotImplementedError("随通信协议定型接线")
