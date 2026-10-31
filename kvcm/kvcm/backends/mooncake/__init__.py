"""mooncake 存储后端适配。

子件：StoreClient（绑定 + HTTP 双通道）、按版本能力集。
MooncakeBackend 组装这些子件并实现原子操作接口，不组装 SegTopology。
未接线的操作由基类抛 UnsupportedCapabilityError。
"""

from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.client import StoreClient
from kvcm.backends.mooncake.topology import SegTopology

__all__ = ["MooncakeBackend", "StoreClient", "SegTopology"]

