"""执行模式（与 Router 选路对齐；节点侧消费已定模式）。"""

from __future__ import annotations

from enum import Enum


class ExecMode(str, Enum):
    COLOCATED = "COLOCATED"  # 混部
    PD_DISAGG = "PD_DISAGG"  # PD 分离
    D_DIRECT = "D_DIRECT"  # 本地命中直跳
