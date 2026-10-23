"""原子操作层抽象基类。

八项操作分三类：
- 原子动作：copy / move / keepalive / demote / promote / remove
- 元数据标记：pin
- 目的型：retain——由其他操作组合实现，对外仍呈现单步语义

完成信号：copy / move / promote 有 READY（任务状态完成 + 目标侧副本
核验通过）；retain 返回空句柄（task_ids=()，无异步工作的路径）时
query_task 立即 READY；其余五项无 READY。任务状态经 query_task 查询。

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


class OpKind(enum.Enum):
    """任务类操作的类型。"""

    COPY = "copy"
    MOVE = "move"
    PROMOTE = "promote"


@dataclass(frozen=True)
class TaskHandle:
    """任务类操作的句柄：操作类型 + 后端任务标识列表。

    一次前缀操作展开为多个后端任务（每 key 一个），句柄承载全部标识。
    任务标识是**不透明对象**：本层不解释其类型（不同后端的任务号形态
    不同，如整数对），只在 copy/move/promote 与 query_task 之间透传；
    需要持久化时由实现侧负责可逆编码。READY 核验所需的 key/目标映射
    由实现侧登记（任务状态机归状态层），不随句柄泄漏存储概念。
    """

    op: OpKind
    task_ids: tuple[object, ...]


class UnsupportedCapabilityError(Exception):
    """调用了后端通道不具备能力的操作（capability().xxx.channel 为 NO）。"""


class VerifyError(RuntimeError):
    """READY 核验失败：任务全部完成但目标侧副本不到位（终态，带明细）。

    与「查询失败」（RPC 错误，可重试）区分：核验失败意味着任务白跑，
    调用方应按终态处理并带原因上抛。
    """


class QueryError(RuntimeError):
    """任务状态查询失败（RPC 错误等瞬时原因，可重试）。

    与 VerifyError（终态）和编程错误（KeyError/TypeError 等，直接传播）
    区分：编排层只应把本类当可重试处理。
    """


class SubmitError(RuntimeError):
    """操作提交失败（copy/move/promote 的发起阶段）。

    异常消息携带明细（如已发起的任务号——中途失败时那些任务需要跟踪）。
    与 QueryError（查询可重试）、VerifyError（核验终态）区分：提交失败
    的重试策略归编排层（幂等键 + 后端幂等兜底）。
    """


class KvOps(ABC):
    """原子操作接口。实现由存储后端适配器提供（如 mooncake 后端）。

    抽象方法只留五项（capability / copy / query_task / abandon / exists）
    ——跑通 copy 流程的最小集。其余操作基类给默认实现（抛
    UnsupportedCapabilityError，消息注明适配器未接线）：编排层判断可
    用性应读 capability()，异常只是兜底；适配器落地某项时 override
    并细化（通道具备但未接线抛 NotImplementedError，通道不具备抛
    UnsupportedCapabilityError——mooncake 后端的 pin 即此形态）。第二
    后端接入时不必先 stub 全部操作。
    """

    def _unsupported(self, op: str) -> UnsupportedCapabilityError:
        return UnsupportedCapabilityError(
            f"{op}：后端适配器未接线本操作"
        )

    @abstractmethod
    def capability(self) -> BackendCapability:
        """后端能力声明，初始化时按后端版本填充。编排层按 channel 列选路径。"""

    @abstractmethod
    def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """在目标实例本机层级为前缀 KV 建副本；源副本由池侧选择。
        READY：任务完成 + 目标侧副本核验通过。"""

    def move(
        self,
        prefix: Prefix,
        target: InstanceId,
        source: InstanceId | None = None,
    ) -> TaskHandle:
        """把前缀 KV 的副本归属转移到目标实例，源副本释放；可指定源实例
        （复制无此能力，移动才有）。READY 同 copy。"""
        raise self._unsupported("move")

    def keepalive(self, prefix: Prefix) -> None:
        """注册保活：对前缀 KV 周期续租防驱逐。停止经 stop_keepalive。
        无 READY。"""
        raise self._unsupported("keepalive")

    def stop_keepalive(self, prefix: Prefix) -> None:
        """注销保活。不是第九项操作——是 keepalive 注册的管理面。"""
        raise self._unsupported("stop_keepalive")

    def pin(self, prefix: Prefix, kind: PinKind = PinKind.SOFT) -> None:
        """元数据标记防驱逐：对已存在的 KV 生效，不写数据。
        SOFT 可解；HARD 写入时固化、不可解，已存在的 KV 无法事后补标
        （能力集 hard_pin_after）。无 READY。"""
        raise self._unsupported("pin")

    def demote(self, prefix: Prefix) -> None:
        """放弃前缀 KV 的热层位置。本接口不指定目标层——去向由池的冷热
        策略决定；是否立即生效取决于后端能力（has_per_key_demote 全版本
        均无，实际为停止保活后的自然降层）。无 READY。"""
        raise self._unsupported("demote")

    def promote(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """把前缀 KV 提回指定实例本机层级（调度意图，与 demote 的
        「放弃位置」不对称是刻意的）。READY 同 copy。"""
        raise self._unsupported("promote")

    def retain(self, prefix: Prefix) -> TaskHandle:
        """目的型：让前缀 KV 在源引擎下线后仍存活。目标基线 = 后端主线
        能力：主路径 = 保留身份注册（mooncake main 的 retained client，
        一次注册 RPC 即完成，无异步工作）——返回空句柄 task_ids=()，
        query_task 立即 READY。过渡替代 = move 到存活节点（0.3.11 无
        retained 能力时的登记实现）则是任务型，返回真实句柄。

        返回 TaskHandle 而非 None：结果必须可观测（「保住了没有」是
        本操作存在的全部意义）；空句柄与真句柄由实现按路径给出。
        """
        raise self._unsupported("retain")

    def remove(self, prefix: Prefix) -> None:
        """删除前缀 KV 的全部副本，全局生效。无 READY。"""
        raise self._unsupported("remove")

    @abstractmethod
    def query_task(self, handle: TaskHandle) -> TaskStatus:
        """查询任务状态。任务管理面，非第九项 KV 操作。

        失败语义：核验失败抛 VerifyError（终态）；查询本身失败抛
        QueryError（可重试）；未知句柄抛 KeyError（编程错误，直接传播）。
        """

    @abstractmethod
    def abandon(self, handle: TaskHandle) -> None:
        """放弃跟踪：删除核验登记，任务状态记为 abandoned。

        不是任务取消——后端没有任务取消能力，已发起的传输可能仍在进
        行甚至成功；本方法只断开本进程与任务的跟踪关系（放弃等
        READY），副本的后续清理由 remove 负责。调用场景：编排层超时或
        主动放弃等待。放弃后句柄失效（再 query_task 抛 KeyError）。
        """

    @abstractmethod
    def exists(self, prefix: Prefix) -> bool:
        """前缀 KV 是否完整在池（全部 key 都在）。

        查询面，非第九项 KV 操作。注意：读即续租（能力集 has_lease）——
        本检查同时是保活机制的续租通道；预热编排用它做前置检查（warm
        未覆盖前缀或 key 口径漂移时 fail fast，复制无意义）。"""
