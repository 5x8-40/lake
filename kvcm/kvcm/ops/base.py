"""原子操作层抽象基类。

八项操作分三类：
- 原子动作：copy / move / keepalive / demote / promote / remove
- 元数据标记：pin
- 目的型：retain——由其他操作组合实现，对外仍呈现单步语义

完成信号：copy / move / promote 有 READY（任务状态完成 + 目标侧副本
核验通过）；retain 返回空句柄（task_ids=()，无异步工作的路径）时
query_task 立即 READY；其余方法（keepalive / stop_keepalive / pin /
demote / remove）无 READY。任务状态经 query_task 查询。

幂等：**本层不去重**——`copy` 每次调用都发起任务，同一前缀重复调用会产生
重复传输。去重归编排层（TaskEngine 以 intent_id = 前缀 × 目标实例 为键：
同键在途返回既有 id，不重复执行；同键已终态视为新意图、重新预热）。池侧对
同 key 的并发复制以 `OBJECT_HAS_REPLICATION_TASK` 拒绝，实现侧把它当「在途」
观测态处理（核验副本，不算失败）。

编排层只调本接口；后端 key、段、租约、client 身份、rank 等概念不允
许出现在签名里——那些归适配层。
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass

from kvcm.ops.capability import BackendCapability
from kvcm.ops.types import InstanceId, PinKind, Prefix


class IntentInputError(Exception):
    """这一次 intent 的输入不对，不是编程错误。

    未登记实例、前缀不足一块、段名单和 TP 对不上、PYTHONHASHSEED 未设。
    消费者按这个类型记 FAILED 后继续。其它异常仍记 FAILED 再抛。
    """


class TaskStatus(enum.Enum):
    """任务状态机（任务类操作：copy / move / promote）。"""

    PENDING = "pending"  # 当前没有生产者
    RUNNING = "running"  # 进行
    READY = "ready"  # 任务完成且目标侧副本核验通过
    FAILED = "failed"  # 失败（处理策略归编排层：重发 / 弃置清副本 / 衔接）


class OpKind(enum.Enum):
    """任务类操作的类型。"""

    COPY = "copy"
    MOVE = "move"
    PROMOTE = "promote"
    RETAIN = "retain"


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

    已经落账的句柄放在 ``handle`` 上，交给流程 ``abandon``（账本记
    abandoned）。池里的传输不取消。没有任务号时 ``handle`` 为 None。
    与 QueryError（查询可重试）、VerifyError（核验终态）区分：提交失败
    的重试策略归编排层（幂等键 + 后端幂等兜底）。
    """

    def __init__(self, message: str = "", *args, handle: TaskHandle | None = None):
        super().__init__(message, *args)
        self.handle = handle


class KvOps(ABC):
    """原子操作接口。实现由存储后端适配器提供（如 mooncake 后端）。

    抽象方法只留五项（capability / copy / query_task / abandon / exists）
    ——跑通 copy 流程的最小集。其余操作基类给默认实现（抛
    UnsupportedCapabilityError，消息注明适配器未接线）：编排层判断可
    用性应读 capability()，异常只是兜底；适配器落地某项时 override
    并细化。未接线抛 UnsupportedCapabilityError。第二后端接入时不必先
    stub 全部操作。
    """

    def _unsupported(self, op: str) -> UnsupportedCapabilityError:
        return UnsupportedCapabilityError(
            f"{op}：后端适配器未接线本操作"
        )

    # ------------------------------------------------------------------
    # 同步 / 异步的分界：**会等待的方法一律 async**
    #
    # async（会等待后端 / 网络 / 时间）：copy / move / keepalive /
    # stop_keepalive / pin / demote / promote / retain / remove /
    # query_task / abandon / exists
    #   —— 实现内部把阻塞调用（同步 RPC、sqlite）交给 executor，等待期
    #      间协程挂起、不占线程（E11 执行模型）
    # 同步（纯内存查询，无等待）：capability / prefix_of
    #   —— 给它们加 async 只会平白多一次调度，调用方还得 await
    # ------------------------------------------------------------------

    @abstractmethod
    def capability(self) -> BackendCapability:
        """后端能力声明，初始化时按后端版本填充。编排层按 channel 列选路径。"""

    @abstractmethod
    async def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """在目标实例本机层级为前缀 KV 建副本；源副本由池侧选择。
        READY：任务完成 + 目标侧副本核验通过。"""

    async def move(
        self,
        prefix: Prefix,
        target: InstanceId,
        source: InstanceId | None = None,
    ) -> TaskHandle:
        """把前缀 KV 的副本归属转移到目标实例，源副本释放；可指定源实例
        （复制无此能力，移动才有）。READY 同 copy。"""
        raise self._unsupported("move")

    async def keepalive(self, prefix: Prefix) -> None:
        """注册保活：对前缀 KV 周期续租防驱逐。停止经 stop_keepalive。
        无 READY。"""
        raise self._unsupported("keepalive")

    async def stop_keepalive(self, prefix: Prefix) -> None:
        """注销保活。不是第九项操作——是 keepalive 注册的管理面。"""
        raise self._unsupported("stop_keepalive")

    async def pin(
        self, prefix: Prefix, kind: PinKind = PinKind.SOFT, *, enable: bool = True
    ) -> None:
        """防驱逐。SOFT 是事后 soft pin，不写数据。``enable=False`` 走空写关闭。
        HARD 标记创建时固化，不接受 ``enable=False``。
        mooncake 上 pin(HARD) 的控制效果是登记保活，不是设置这个位。无 READY。"""
        raise self._unsupported("pin")

    async def demote(self, prefix: Prefix) -> None:
        """放弃前缀 KV 的热层位置。没有立刻搬到盘上的调用。
        mooncake 上先停保活，等满一个 lease TTL，再用
        GET /batch_query_keys 确认盘副本。写回且池不满时失败。无 READY。"""
        raise self._unsupported("demote")

    async def promote(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """把前缀 KV 提回到 target。

        打 N 次 GetReplicaList，再用内存副本端点对 locate(target)。
        不能在打次数之前证明 target 是盘副本登记实例。打完仍没有
        内存副本就失败，不在这里轮询。
        """
        raise self._unsupported("promote")

    async def retain(self, prefix: Prefix) -> TaskHandle:
        """目的型：让前缀 KV 在源引擎下线后仍存活。结果要可观测，所以
        签名返回 TaskHandle。当前不实现。1fc27b6 没有
        ``AddReplicaForRetainedClient``。``BufferRegistrationAction::kRetained``
        是外部 buffer 引用计数。通道保持关闭。
        """
        raise self._unsupported("retain")

    async def remove(self, prefix: Prefix, force: bool = False) -> None:
        """删除前缀 KV 的全部副本，全局生效。无 READY。

        无论 force 都先停保活。force=False 再等一个 lease 后
        ``batch_remove(force=False)``。force=True 立刻强删。
        失败或取消不把保活加回去：停过就留停，调用方看到原因后自己重试，
        租约可以过期。
        """
        raise self._unsupported("remove")

    @abstractmethod
    async def query_task(self, handle: TaskHandle) -> TaskStatus:
        """查询任务状态。任务管理面，非第九项 KV 操作。

        失败语义：核验失败抛 VerifyError（终态）；查询本身失败抛
        QueryError（可重试）；未知句柄抛 KeyError（编程错误，直接传播）。
        """

    @abstractmethod
    async def abandon(self, handle: TaskHandle) -> None:
        """放弃跟踪：删除核验登记，任务状态记为 abandoned。

        不是任务取消——后端没有任务取消能力，已发起的传输可能仍在进
        行甚至成功；本方法只断开本进程与任务的跟踪关系（放弃等
        READY），副本的后续清理由 remove 负责。调用场景：编排层超时或
        主动放弃等待。放弃后句柄失效（再 query_task 抛 KeyError）。
        """

    @abstractmethod
    async def exists(self, prefix: Prefix) -> bool:
        """前缀 KV 是否完整在池（全部 key 都在）。

        查询面，非第九项 KV 操作。注意：读即续租（能力集 has_lease）——
        本检查同时是保活机制的续租通道；预热编排用它做前置检查（warm
        未覆盖前缀或 key 口径漂移时 fail fast，复制无意义）。"""

    def prefix_of(self, handle: TaskHandle) -> Prefix | None:
        """句柄对应的前缀（句柄由某次 prefix 操作产生，后端持有登记）。

        查询面，非 KV 操作。用途：rebuild 恢复的 handle 被 TaskEngine
        track 到 READY 后，编排层要拿到 prefix 才能注册保活（恢复路径
        上没有 submit 的 prefix 入参）。句柄已终态被删除登记后返回
        None——调用方需在轮询前取。默认 None：适配器未接线时编排层
        退化为「恢复任务 READY 后不保活」（与重启前未注册同语义）。"""
        return None
