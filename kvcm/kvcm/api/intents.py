"""IntentReceiver：接收 Router 预热 intent 与会话结束线索（下行通道）。

预热 intent 通道已落地为 gRPC 服务（E12，`kvcm/api/grpc_server.py`，
协议 `proto/kvcm_intent.proto`；语义 intent ≠ command，回执走
GetTask）。本类剩余占位 = 会话结束线索入口，随设计定型填充。
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
