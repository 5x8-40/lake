"""后端能力声明的结构定义。

每项能力分两个维度：
- rpc：后端服务与 C++ 客户端是否具备该能力（与调用方语言无关）
- channel：kvcm 当前实际可调（语言绑定与 HTTP 管理端点的并集）

编排层按 channel 选路径；rpc 有而 channel 无的能力是封装候选。
同一后端不同版本能力不同：能力集由适配器初始化时按版本填充
（见各后端包），本模块不含任何具体后端的知识。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Support(enum.Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"  # 待核对


@dataclass(frozen=True)
class Capability:
    """单项能力：服务端有无 × kvcm 通道有无 + 通道说明。"""

    rpc: Support
    channel: Support
    note: str = ""


@dataclass(frozen=True)
class BackendCapability:
    """后端能力全集。字段值由适配器按后端版本填充，编排层只读。"""

    has_lease: Capability  # 读操作对副本续租
    soft_pin_after: Capability  # 对已存在的 key 事后加 soft pin
    hard_pin_after: Capability  # 对已存在的 key 事后加 hard pin
    can_copy_with_source: Capability  # 复制时指定源副本
    has_offload: Capability  # 池通道降层
    has_promotion: Capability  # 池通道提回
    has_per_key_demote: Capability  # 确定性立即降层
    has_task_cancel: Capability  # copy/move 任务取消
    has_retained_client: Capability  # 保留身份注册。两版本都没有这个接口
    has_dfs_replica: Capability  # 共享文件系统副本
    has_admin_query: Capability  # 零副作用副本核验
