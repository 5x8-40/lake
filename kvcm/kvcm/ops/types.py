"""原子操作层的公共类型。

接口以「前缀 × 实例」为参数：后端 key、段、租约、rank 等概念由适配层
在实现内部展开，不出现在本层。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import NewType

#: 引擎实例标识（实例注册名，由实例注册来源分配）。
InstanceId = NewType("InstanceId", str)


@dataclass(frozen=True)
class Prefix:
    """一段可复用前缀的标识：模型 + token 序列。

    前缀 → 后端 key 列表的计算由引擎适配器完成（各引擎口径不同），
    本层只携带计算的输入。
    """

    model_id: str
    token_ids: tuple[int, ...]


class IntentInputError(Exception):
    """这一次 intent 的输入不对，不是编程错误。

    未登记实例、前缀不足一块、段名单和 TP 对不上、PYTHONHASHSEED 未设。
    消费者按这个类型记 FAILED 后继续。其它异常仍记 FAILED 再抛。
    """


class PinKind(enum.Enum):
    """pin 类型。"""

    SOFT = "soft"  # 可解
    HARD = "hard"  # 标记创建时固化；pin(HARD) 拒绝，要 lease 保护用 register_keepalive()
