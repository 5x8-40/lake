"""EventPublisher：KV 事件补发（上行通道）的接口占位。

实现是 ``kvcm.events.BlockStoredPublisher``（E14）：READY 后按目标
实例端点补发 BlockStored。本类不接发布，避免和薄模块各写一套。
端点注册给索引仍归 D4/E13。
"""

from __future__ import annotations

from kvcm.ops.types import InstanceId, Prefix


class EventPublisher:
    """READY 事件补发器。本步仅占位接口形态。"""

    def publish_block_stored(self, prefix: Prefix, target: InstanceId) -> None:
        """向目标实例的补发端点发 BlockStored（前缀已在该实例本机就绪）。"""
        raise NotImplementedError("随端点生命周期与事件格式接线")
