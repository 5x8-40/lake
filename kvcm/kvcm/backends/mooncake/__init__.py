"""mooncake 存储后端适配。

子件：StoreClient（绑定 + HTTP 双通道）、SegTopology（段拓扑）、按版本
能力集；MooncakeBackend 组装子件并实现原子操作接口。
"""

from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.client import StoreClient
from kvcm.backends.mooncake.topology import SegTopology

__all__ = ["MooncakeBackend", "StoreClient", "SegTopology"]

