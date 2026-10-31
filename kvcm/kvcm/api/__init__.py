"""接口层：进程对外通道（不含任何存储概念）。

intent 下行、实例事件接入。BlockStored 补发在 ``kvcm.events``。
"""

from kvcm.api.intents import IntentReceiver
from kvcm.api.watcher import InstanceWatcher

__all__ = ["IntentReceiver", "InstanceWatcher"]
