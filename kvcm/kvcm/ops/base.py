"""原子操作层抽象基类。

八项操作分三类：
- 原子动作：copy / move / keepalive / demote / promote / remove
- 元数据标记：pin
- 目的型：retain——由其他操作组合实现，对外仍呈现单步语义

完成信号：copy / move / promote 有 READY（任务状态完成 + 目标侧副本
核验通过）；其余五项无 READY。任务状态经 query_task 查询。

幂等：以（前缀 × 目标实例）为幂等键——在途不重复发、完成不重复发；
后端幂等兜底。

编排层只调本接口；后端 key、段、租约、client 身份、rank 等概念不允
许出现在签名里——那些归适配层。
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass

from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, PinKind, Prefix


class TaskStatus(enum.Enum):
    """任务状态机（任务类操作：copy / move / promote）。"""

    PENDING = "pending"  # 排队
    RUNNING = "running"  # 进行
    READY = "ready"  # 任务完成且目标侧副本核验通过
    FAILED = "failed"  # 失败（处理策略归编排层：重发 / 弃置清副本 / 衔接）


@dataclass(frozen=True)
class TaskHandle:
    """任务类操作的句柄：操作类型 + 后端任务标识列表。

    一次前缀操作展开为多个后端任务（每 key 一个），句柄承载全部标识；
    READY 核验所需的 key/目标映射由实现侧登记（任务状态机归状态层），
    不随句柄泄漏存储概念。
    """

    op: str  # "copy" / "move" / "promote"
    task_ids: tuple[str, ...]


class UnsupportedCapabilityError(Exception):
    """调用了后端通道不具备能力的操作（capability().xxx.channel 为 NO）。"""


class KvOps(ABC):
    """原子操作接口。实现由存储后端适配器提供（如 mooncake 后端）。"""

    @abstractmethod
    def capability(self) -> BackendCapability:
        """后端能力声明，初始化时按后端版本填充。编排层按 channel 列选路径。"""

    @abstractmethod
    def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """在目标实例本机层级为前缀 KV 建副本；源副本由池侧选择。
        READY：任务完成 + 目标侧副本核验通过。"""

    @abstractmethod
    def move(
        self,
        prefix: Prefix,
        target: InstanceId,
        source: InstanceId | None = None,
    ) -> TaskHandle:
        """把前缀 KV 的副本归属转移到目标实例，源副本释放；可指定源实例
        （复制无此能力，移动才有）。READY 同 copy。"""

    @abstractmethod
    def keepalive(self, prefix: Prefix) -> None:
        """注册保活：对前缀 KV 周期续租防驱逐。停止经 stop_keepalive。
        无 READY。"""

    @abstractmethod
    def stop_keepalive(self, prefix: Prefix) -> None:
        """注销保活。不是第九项操作——是 keepalive 注册的管理面。"""

    @abstractmethod
    def pin(self, prefix: Prefix, kind: PinKind = PinKind.SOFT) -> None:
        """元数据标记防驱逐：对已存在的 KV 生效，不写数据。
        SOFT 可解；HARD 写入时固化、不可解，已存在的 KV 无法事后补标
        （能力集 hard_pin_after）。无 READY。"""

    @abstractmethod
    def demote(self, prefix: Prefix) -> None:
        """放弃前缀 KV 的热层位置。本接口不指定目标层——去向由池的冷热
        策略决定；是否立即生效取决于后端能力（has_per_key_demote 全版本
        均无，实际为停止保活后的自然降层）。无 READY。"""

    @abstractmethod
    def promote(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """把前缀 KV 提回指定实例本机层级（调度意图，与 demote 的
        「放弃位置」不对称是刻意的）。READY 同 copy。"""

    @abstractmethod
    def retain(self, prefix: Prefix) -> None:
        """目的型：让前缀 KV 在源引擎下线后仍存活。按后端能力组合实现
        （保留身份注册 / 共享文件系统副本 / 迁往存活节点），目标节点由
        实现结合拓扑与存活信息选择。对外单步语义。无 READY。"""

    @abstractmethod
    def remove(self, prefix: Prefix) -> None:
        """删除前缀 KV 的全部副本，全局生效。无 READY。"""

    @abstractmethod
    def query_task(self, handle: TaskHandle) -> TaskStatus:
        """查询任务状态。任务管理面，非第九项 KV 操作。"""

    @abstractmethod
    def exists(self, prefix: Prefix) -> bool:
        """前缀 KV 是否完整在池（全部 key 都在）。

        查询面，非第九项 KV 操作。注意：读即续租（能力集 has_lease）——
        本检查同时是保活机制的续租通道；预热编排用它做前置检查（warm
        未覆盖前缀或 key 口径漂移时 fail fast，复制无意义）。"""
