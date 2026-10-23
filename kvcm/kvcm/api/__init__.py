"""接口层：进程对外通道（不含任何存储概念）。

三条通道全部单向：intent 下行、事件上行、实例事件接入。
"""

from kvcm.api.events import EventPublisher
from kvcm.api.intents import IntentReceiver
from kvcm.api.watcher import InstanceWatcher

__all__ = ["IntentReceiver", "EventPublisher", "InstanceWatcher"]
