"""EventPublisher：KV 事件补发（上行通道）。

预热 READY 后按目标实例端点补发引擎格式的 BlockStored 事件——预复制
完全在引擎之外发生，引擎通道与池通道都不会自动产生这件事的事件，只
能由本进程补发。事件格式由引擎适配器提供（schema 归引擎侧）。

端点生命周期（每实例一个发布端点、注册给索引侧）随实例事件接线。
"""

from __future__ import annotations

from kvcm.ops.types import InstanceId, Prefix


class EventPublisher:
    """READY 事件补发器。本步仅占位接口形态。"""

    def publish_block_stored(self, prefix: Prefix, target: InstanceId) -> None:
        """向目标实例的补发端点发 BlockStored（前缀已在该实例本机就绪）。"""
        raise NotImplementedError("随端点生命周期与事件格式接线")
