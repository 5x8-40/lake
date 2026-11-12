"""原子操作层抽象基类。

八项操作分三类：
- 原子动作：copy / move / keepalive / demote / promote / remove
- 元数据标记：pin
- 目的型：retain——由其他操作组合实现，对外仍呈现单步语义

keepalive 拆成三件事：`register_keepalive` / `unregister_keepalive`
管理 Ledger 登记，`renew_once` 对前缀执行一次续租。周期续租由
KeepaliveLoop 调 `renew_once`；登记由 TaskEngine 触发
`KeepaliveLoop.register`（内部写 Ledger，也可直接调 L3 的
`register_keepalive`）。不再提供一个既登记又续租的 `keepalive`。

完成信号：copy / move 有 READY（任务完成 + 目标侧副本核验，经
``TaskStatus.READY``）。promote 也有 READY（D2：某实例可调度），但 L3
``promote`` 只触发、无 TaskHandle；READY 由 PromoteFlow 的
``PromoteResult.ready`` 表达。是否已有 memory 由 ``check_promote``
判定；不传 target 时落点反查 ``memory_instance``（对不上则
ready=False）。传 target 则落在该实例后 ready=True（必要时经
PrefetchFlow copy）。retain 的 READY 跟着做法走。其余方法无 READY。
任务状态经 query_task 查询（仅任务类操作）。

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
from kvcm.ops.types import InstanceId, IntentInputError, PinKind, Prefix


class TaskStatus(enum.Enum):
    """任务状态机（任务类操作：copy / move）。"""

    PENDING = "pending"  # 当前没有生产者
    RUNNING = "running"  # 进行
    READY = "ready"  # 任务完成且目标侧副本核验通过
    FAILED = "failed"  # 失败（处理策略归编排层：重发 / 弃置清副本 / 衔接）


class OpKind(enum.Enum):
    """任务类操作的类型。"""

    COPY = "copy"
    MOVE = "move"
    RETAIN = "retain"


@dataclass(frozen=True)
class TaskHandle:
    """任务类操作的句柄：操作类型 + 后端任务标识列表。

    一次前缀操作展开为多个后端任务（每 key 一个），句柄承载全部标识。
    任务标识是**不透明对象**：本层不解释其类型（不同后端的任务号形态
    不同，如整数对），只在 copy/move 与 query_task 之间透传；
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
    """操作提交失败（copy/move 的发起阶段）。

    已经落账的句柄放在 `handle` 上，交给流程 `abandon`（账本记
    abandoned）。池里的传输不取消。没有任务号时 `handle` 为 None。
    与 QueryError（查询可重试）、VerifyError（核验终态）区分：提交失败
    的重试策略归编排层（幂等键 + 后端幂等兜底）。
    """

    def __init__(self, message: str = "", *args, handle: TaskHandle | None = None):
        super().__init__(message, *args)
        self.handle = handle

class AdmissionWait(Exception):
    """这一轮目标段放不下，或管理端口暂时连不上。

    调用方在前缀闸外等待后再查。不是复制失败，没有任务号。
    """


class AdmissionStop(Exception):
    """目标段在 master 上不存在。

    不等待。这次预热停止，不记成复制失败。
    """



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
    # async（会等待后端 / 网络 / 时间）：copy / move / register_keepalive /
    # unregister_keepalive / renew_once / pin / demote / promote /
    # check_promote / retain / remove / query_task / abandon / exists
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

    def open_segment_budget(self) -> object | None:
        """开始一轮等段剩余。占目标段的后端在这里新建一份本轮字节需求的缓存。

        与「准入」无关：这里不接纳请求、不限并发，只是记这条前缀要往目标段写多少字节。
        默认不占新段，返回 None。编排层总是调用，不探测具体后端有没有这个方法。
        """
        return None

    def close_segment_budget(self, token: object | None) -> None:
        """结束一轮等待，丢掉缓存。默认无事可做。"""
        return None

    async def check_segment_room(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        timeout_s: float | None = None,
    ) -> None:
        """闸外判断：这条前缀要写的新字节，目标段还放不放得下。

        放不下由子类抛 AdmissionWait，段名不存在抛 AdmissionStop。
        默认不占目标段，直接通过。第二个后端不 override 就不会静默漏掉调用，
        只是没有段容量这一步。timeout_s 是剩余预算；默认实现不查询，忽略它。
        """
        return None

    async def move(
        self,
        prefix: Prefix,
        target: InstanceId,
        source: InstanceId,
    ) -> TaskHandle:
        """把前缀 KV 的副本归属转移到目标实例，源副本释放；必须指定源实例
        （复制无此能力，移动才有）。READY 同 copy。"""
        raise self._unsupported("move")

    async def register_keepalive(self, prefix: Prefix) -> None:
        """登记保活：把前缀写进 Ledger，周期续租由 KeepaliveLoop 负责。
        不在这里发续租 RPC。无 READY。"""
        raise self._unsupported("register_keepalive")

    async def unregister_keepalive(self, prefix: Prefix) -> None:
        """注销保活登记。无 READY。"""
        raise self._unsupported("unregister_keepalive")

    async def renew_once(self, prefix: Prefix) -> bool:
        """对前缀执行一次续租；返回是否全部 key 仍在池。

        这是真正的 lease 续租动作，由 KeepaliveLoop 周期调用。
        """
        raise self._unsupported("renew_once")

    async def pin(
        self, prefix: Prefix, kind: PinKind = PinKind.SOFT, *, enable: bool = True
    ) -> None:
        """防驱逐。SOFT 是事后 soft pin，不写数据。``enable=False`` 走空写关闭。
        HARD 标记创建时固化，事后设置不了；适配器拒绝 ``pin(HARD)``。
        要 lease 保护请显式调 ``register_keepalive()``。无 READY。"""
        raise self._unsupported("pin")

    async def demote(self, prefix: Prefix) -> None:
        """放弃前缀 KV 的热层位置。没有立刻搬到盘上的调用。
        mooncake 上先注销保活，等满一个 lease TTL，再用
        GET /batch_query_keys 确认盘副本。写回且池不满时失败。无 READY。"""
        raise self._unsupported("demote")

    async def promote(self, prefix: Prefix) -> None:
        """触发池提回：只打 N 次访问，不验证、不等待、不带 target。

        落点由池派给盘副本登记 client。是否已有 memory、落在哪台实例，
        分别由 ``check_promote`` / ``memory_instance`` 查询。
        """
        raise self._unsupported("promote")

    async def check_promote(
        self, prefix: Prefix, target: InstanceId | None = None
    ) -> bool:
        """前缀 memory 是否就绪。

        - ``target is None``：每个 key 至少有一处 memory（不限实例）。
        - ``target`` 给定：每个 key 按 ``build_plan`` 落在该实例对应 seg。

        查询本身有副作用（mooncake：授 lease、累计命中、可能触发
        promotion / dynamic replication）。失败抛 QueryError。供
        PromoteFlow 轮询。
        """
        raise self._unsupported("check_promote")

    async def memory_instance(self, prefix: Prefix) -> InstanceId | None:
        """若每 key 已有 memory，返回任一可按 rank 映射服务全前缀的实例。

        对不上目录 / 反查失败则 ``None``（提回仍算成功）。默认不实现则
        返回 ``None``。
        """
        return None

    async def retain(self, prefix: Prefix, target: InstanceId | None = None) -> TaskHandle:
        """目的型：让前缀 KV 在源引擎下线后仍存活。

        做法决定完成信号（D2 §4）：
        - 给了 target、用 copy/move 搬到那台实例：返回搬运任务号，
          完成后那台实例具备服务这份前缀的条件（READY，可切流、停源实例）
        - 搬进 kvcm 自己的段盘 / 用旧 client_id 接管盘上数据 / DFS：
          副本不落在某台引擎的 DRAM 上，没有可切流的实例；返回的任务号
          用于跟这次搬运，完成后用副本查询确认在册

        返回 TaskHandle 而非 None：结果必须可观测（「保住了没有」是
        本操作存在的全部意义）。
        """
        raise self._unsupported("retain")

    async def remove(self, prefix: Prefix, force: bool = False) -> None:
        """删除前缀 KV 的全部副本，全局生效；不支持按 target 删除。无 READY。

        无论 force 都先注销保活。force=False 再等一个 lease 后
        ``batch_remove(force=False)``。force=True 立刻强删。
        失败或取消不把登记加回去：注销过就留注销，调用方看到原因后自己重试，
        租约可以过期。
        """
        raise self._unsupported("remove")

    @abstractmethod
    async def query_task(self, handle: TaskHandle) -> TaskStatus:
        """查询任务状态。任务管理面，非第九项 KV 操作。

        失败语义：核验失败抛 VerifyError（终态）；查询本身失败抛
        QueryError（可重试）；未知句柄抛 KeyError（编程错误，直接传播）；
        空任务号抛 ValueError（契约错误，直接传播）。
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

    def target_of(self, handle: TaskHandle) -> InstanceId | None:
        """句柄对应的目标实例。与 prefix_of 对称，须在轮询前取。

        终态会删核验登记，READY 返回后再取就是 None。恢复路径按实例
        补发 BlockStored 要用它。默认 None：适配器未接线时恢复路径
        不补发（保活仍走 prefix_of）。"""
        return None
