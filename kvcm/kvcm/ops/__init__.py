"""原子操作层：八项 KV 管理操作的抽象接口、公共类型与能力声明结构。"""

from kvcm.ops.base import (
    IntentInputError,
    KvOps,
    OpKind,
    QueryError,
    SubmitError,
    TaskHandle,
    TaskStatus,
    UnsupportedCapabilityError,
    VerifyError,
)
from kvcm.ops.capability import BackendCapability, Capability, Support
from kvcm.ops.types import InstanceId, PinKind, Prefix

__all__ = [
    "IntentInputError",
    "KvOps",
    "OpKind",
    "QueryError",
    "SubmitError",
    "TaskHandle",
    "TaskStatus",
    "UnsupportedCapabilityError",
    "VerifyError",
    "BackendCapability",
    "Capability",
    "Support",
    "InstanceId",
    "PinKind",
    "Prefix",
]
