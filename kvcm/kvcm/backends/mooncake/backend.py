"""MooncakeBackend：原子操作接口的 mooncake 实现。

组装两个适配维度的子件：
- 存储侧：StoreClient（绑定 + HTTP 双通道）、能力集（按版本）
- 引擎侧：KeyComputer（前缀 → key 列表 → rank 映射计划）、实例 → seg
  列表（以 locate 可调用注入——demo 环境 = pidfile/proctitle 探测，生产
  = 实例注册）

本类不组装 SegTopology。任务标识（mooncake 为整数对）在本实现内原样
透传，不做字符串化；需要写入 Ledger 时由本实现做可逆编码（"hi:lo"）。

已接线：copy / move / remove / exists / query_task /
register_keepalive / unregister_keepalive / renew_once / demote /
promote / check_promote。pin 仍走基类 UnsupportedCapabilityError
（soft pin 后续 PR）。retain 通道关闭，开头直接 NotImplementedError。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

from kvcm.backends.mooncake.admin_main import AdminMainError
from kvcm.backends.mooncake.client import StoreClient, StoreClientError
from kvcm.backends.mooncake.http_admin import AdminClient, AdminError, SegmentNotFound
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger
from kvcm.ops.base import (
    KvOps,
    OpKind,
    QueryError,
    AdmissionStop,
    AdmissionWait,
    SubmitError,
    TaskHandle,
    TaskStatus,
    UnsupportedCapabilityError,
    VerifyError,
)
from kvcm.ops.capability import BackendCapability, Support
from kvcm.ops.types import InstanceId, IntentInputError, Prefix

logger = logging.getLogger(__name__)

# 一轮预热等待的字节需求。只在该协程里有效，并发的前缀各持各的。
# None = 还没取大小；缺省 _UNSET = 不在 TaskEngine 的等待里（直接 copy）。
class _AdmissionSlot:
    def __init__(self) -> None:
        # key = (计划第一条的段名, 计划里的 key 集合)。对不上就重查大小。
        self.key: tuple[str, frozenset[str]] | None = None
        # None 且 key 已设 = 算过了，没有新占用。不要用空字典表示。
        self.need: tuple[str, int] | None = None


_UNSET = object()
_admission_need: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "kvcm_admission_need", default=_UNSET
)


def _name_of(fn: Any) -> str:
    """函数的可读名（日志用）。"""
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))


def _replica_row(row: object) -> tuple[str, int | None, tuple[str, ...]]:
    """(端点, 字节数或 None, 拿不到大小时的属性名)。"""
    if not isinstance(row, tuple) or len(row) < 2:
        raise QueryError(f"unexpected replica size row: {row!r}")
    ep = str(row[0]) if row[0] else ""
    sz = row[1]
    if sz is None or isinstance(sz, bool) or not isinstance(sz, int) or sz < 0:
        sz = None
    else:
        sz = int(sz)
    attrs: tuple[str, ...] = ()
    if len(row) > 2 and row[2] is not None:
        attrs = tuple(str(a) for a in row[2])
    return ep, sz, attrs


def encode_task_id(task_id: object) -> str:
    """任务标识 → 可逆字符串编码（Ledger 持久化用）。

    mooncake 任务号为 (uint64, uint64) 整数对，编码为 "hi:lo"；其他形态
    兜底 str()。
    """
    if isinstance(task_id, (tuple, list)) and len(task_id) == 2:
        try:
            return f"{int(task_id[0])}:{int(task_id[1])}"
        except (TypeError, ValueError):
            pass
    return str(task_id)


def decode_task_id(s: str) -> object:
    """encode_task_id 的逆（重启恢复用）："hi:lo" → (hi, lo)；其余原样。"""
    parts = s.split(":")
    if len(parts) == 2:
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            pass
    return s


class MooncakeBackend(KvOps):
    """mooncake 后端的原子操作实现。"""

    def __init__(
        self,
        *,
        client: StoreClient,
        key_computer: KeyComputer,
        locate: Callable[[InstanceId], Sequence[str]],
        capability: BackendCapability,
        ledger: Ledger | None = None,
        submit_concurrency: int = 4,
        executor: ThreadPoolExecutor | None = None,
        rpc_timeout_s: float = 30.0,
        admin: AdminClient | None = None,
        promote_hits: int = 2,
        lease_ttl_s: float = 10.0,
    ) -> None:
        """
        client：已 connect 的 StoreClient（控制面身份，global_segment_size=0）。
        admin：master 管理端口（查目标段剩余）。不传则 _admit_new_bytes
            抛 QueryError，不盲提交。
        key_computer：引擎侧 key 计算器（持展开配置与 rank 映射知识）。
        locate：实例 → rank 有序 seg 列表（targets[i] = rank i 的本机 seg）。
        capability：按连接上的后端版本填充的能力集（必填——能力宣告不
            允许默认冒充，填错版本 = 编排层选错路径）。
        ledger：任务状态记录（可选；不注入则任务状态只留进程内）。
        executor：执行同步阻塞调用的线程池（mooncake 同步 RPC）。不传则
            按 RPC 执行密度自建（4~8，E11 定稿）——**不用
            asyncio.to_thread 默认池**（大小不可控）。Ledger 自己有专用
            单线程池，与本池分开。
        submit_concurrency：**整个 backend 内**同时在飞的 create_copy_task
            上限（`asyncio.Semaphore`，所有 copy 共用一把）。名额在这次
            RPC 返回时就还，任务已经被 master 接受——它限建链速率，不限
            「还没传完的前缀」。后者是传输闸（TaskEngine `max_concurrent`，
            一个名额 = 一个前缀从提交占到终态）。两把不重复：只有本信号量
            时，排队前缀会以每次 4 个 RPC 的速度全部提交完。它也不是「单次
            copy 的窗口」——传输闸允许 4 个前缀同时提交，若每个 copy 各持
            一把默认 4 的窗口，在飞建链就是 4×4=16，正好超过 #16 的打爆点
            （单机 14 连发是建链 TRANSFER_FAIL，不是 #17 的
            NO_AVAILABLE_HANDLE；窗口 4 节奏化 42/42 全成）。0.3.11 实测
            ascend 上 14 连发约半数 TRANSFER_FAIL；#17 跨机同样窗口大约
            一半失败，主导错误名是 NO_AVAILABLE_HANDLE（max retries reached:
            10），所以它不是跨机的充分条件。上限必须是**进程级**的，
            不代替 E15，也不做网卡限速。
        """
        if submit_concurrency <= 0:
            raise ValueError("submit_concurrency must be positive")
        if promote_hits < 1:
            raise ValueError("promote_hits must be positive")
        if lease_ttl_s < 0:
            raise ValueError("lease_ttl_s must be >= 0")
        self._client = client
        # 副本核验走主线 GET /batch_query_keys。不设则核验失败，不退回
        # batch_get_replica_desc。
        self._admin = admin
        self._promote_hits = promote_hits
        # 与 master 的 default_kv_lease_ttl 对齐。main 默认 10s
        # （DEFAULT_DEFAULT_KV_LEASE_TTL = 10000ms）。kvcm 不改 master 的这个值。
        # demote 等满这一段：没有马上撤销 lease 的调用，也不查剩余租约。
        self._lease_ttl_s = lease_ttl_s
        self._keys = key_computer
        self._locate = locate
        self._cap = capability
        self._ledger = ledger
        self._submit_concurrency = submit_concurrency
        # 进程级提交信号量：延迟创建（首次 copy 时建）。Semaphore() 构造
        # 不绑循环，首次排队等名额才绑；什么时候创建不是正确性的关键。
        self._submit_sem: asyncio.Semaphore | None = None
        # 同一 handle 内并发查任务，不超过 4，不另建线程池。
        self._query_sem: asyncio.Semaphore | None = None
        # 协程侧 RPC 超时（0.3.11 的绑定没有超时参数；main 有
        # MC_RPC_TIMEOUT_MS，届时可与此对齐）。<=0 表示不设超时。
        self._rpc_timeout_s = rpc_timeout_s
        # 管理端口：每轮只打一次 /query_segment，是因为只查计划第一条，
        # 不是因为假定各段剩余相同。没有它就不知道剩余，准入直接拒绝，
        # 不许盲提交。
        self._admin = admin
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max(4, min(8, (os.cpu_count() or 4))), thread_name_prefix="kvcm-rpc"
        )
        self._owns_executor = executor is None
        # ── 共享态纪律（E11 定稿，务必遵守）─────────────────────────
        # `_plans` / `_watch` 无锁，靠「事件循环单线程 + 每个 handle 同一
        # 时刻只有一个协程操作」保证正确性。因此：
        #
        #   **禁止在读写这两个字典之间 await。**
        #
        # 现在 `query_task` 里有一处跨 await 的读→写（读 entry → await
        # RPC → 写 _watch）。它安全的前提是「同一 handle 只有一条协程在
        # 推进」——取消也是在同一条协程里抛 CancelledError 直接跳去
        # abandon，不会另起一条。**如果要新增「另起协程对同一 handle 做
        # abandon/查询」的路径，必须先给这两个字典加锁**，否则会出现
        # 孤儿条目（entry 被删后又写回 _watch）。
        #
        # handle → (prefix, target, plan [(key, target_seg), ...], handle_id)：
        # READY 核验与 Ledger 迁移登记；终态即删（防长驻泄漏）。
        # handle_id = 同一次操作发起的一组任务的分组键（uuid），写进
        # Ledger 供重启恢复时把同一组的任务重新拼成一个 handle。
        self._plans: dict[
            TaskHandle, tuple[Prefix, InstanceId, list[tuple[str, str]], str]
        ] = {}
        # 在途冲突观测态：handle → 冲突失败的任务号。任务被
        # OBJECT_HAS_REPLICATION_TASK 拒（别人在复制同 key，D1 §4 按
        # 「在途」不当失败）后，不再查任务、转为只核验副本——副本到位
        # 即 READY（字节是谁搬的不重要，到位是目的）。
        self._watch: dict[TaskHandle, tuple[object, ...]] = {}

    def capability(self) -> BackendCapability:
        return self._cap

    @property
    def executor(self) -> ThreadPoolExecutor:
        """RPC executor（供其他组件复用，避免再建线程池——定稿只要两个
        池：RPC 池 + Ledger 单线程池）。"""
        return self._executor

    async def _expand_keys(self, token_ids: tuple[int, ...]) -> list[str]:
        """算 key 走已有 RPC 池，不占事件循环。不新建线程池。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._keys.keys_for_tokens, token_ids
        )

    # ------------------------------------------------------------------
    # 阻塞调用的协程包装
    # ------------------------------------------------------------------

    async def _rpc(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout_s: float | None = None,
    ) -> Any:
        """在 RPC executor 里执行同步调用（mooncake 绑定），协程挂起等
        结果——等待期间不占事件循环。

        超时只放弃等待，**不中断线程**（Python 无法中断 C++ 调用）：线程
        会继续跑到自然结束。所以超时后仍占着一个 RPC 线程——退出时等它的
        是 daemon 的 `_wait_executor`（池由 daemon 建并注入，`close()` 对
        注入池无事可等）。
        """
        loop = asyncio.get_running_loop()

        def _call():
            try:
                return fn(*args)
            except BaseException as e:
                # 调用方可能已超时离开（wait_for 取消了 future），异常到不了
                # 它手上——记一条，避免「绑定随后的失败」静默丢失。
                if isinstance(e, Exception):
                    logger.warning("RPC %s failed in worker: %s", _name_of(fn), e)
                raise

        fut = loop.run_in_executor(self._executor, _call)
        limit = self._rpc_timeout_s
        if timeout_s is not None:
            limit = (
                timeout_s
                if self._rpc_timeout_s <= 0
                else min(self._rpc_timeout_s, timeout_s)
            )
        if timeout_s is None and self._rpc_timeout_s <= 0:
            return await fut
        if limit <= 0:
            raise QueryError(f"RPC timeout after {limit}s: {_name_of(fn)}")
        try:
            return await asyncio.wait_for(fut, timeout=limit)
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise QueryError(
                f"RPC timeout after {limit}s: {_name_of(fn)}"
            ) from e

    async def _in_ledger(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在 Ledger 自己的单线程池里调 Ledger。

        **不用 RPC 池**：Ledger 的公共方法是「submit().result()」的同步
        阻塞形态，本来就要进线程；投到 Ledger 池只套一层。用 RPC 池还有
        两个害处：占掉传输名额，以及退出时池已关（`abandon` 恰好在那个
        窗口落账，会抛 `cannot schedule new futures after shutdown`
        （解释器退出时是 `after interpreter shutdown`），
        账本留 `running`）。
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._ledger.executor, functools.partial(fn, *args, **kwargs)
        )

    def close(self, *, wait_timeout_s: float = 5.0) -> bool:
        """释放自建的 executor（外部注入的由调用方管）。

        `shutdown(wait=True)` 会等在途 RPC 结束，但它没有超时参数，卡死的
        调用会让退出永远挂着。所以在另一个线程里跑它、外层设 5 秒上限。

        为什么不能只 `shutdown(wait=False)`：那会立刻返回，调用方接着
        `client.close()`，与还在 mooncake 绑定里的 RPC 并发（demo 实测的
        退出期崩溃正是这个形状）。

        **返回值**：`True` = 线程全部结束；`False` = 还有调用停在绑定里（调用方
        须立即 `os._exit(1)`，见 daemon 退出说明）。池是外部注入时无事可等。
        """
        if not self._owns_executor:
            return True

        done = threading.Event()

        def _shutdown() -> None:
            try:
                self._executor.shutdown(wait=True)
            finally:
                done.set()

        threading.Thread(target=_shutdown, name="kvcm-rpc-drain", daemon=True).start()
        return done.wait(timeout=wait_timeout_s)

    # ------------------------------------------------------------------
    # copy（已落地）
    # ------------------------------------------------------------------

    def _submit_gate(self) -> asyncio.Semaphore:
        """进程级提交信号量（所有 copy 共用）。

        与传输闸（TaskEngine `max_concurrent`）的分工：传输闸一个名额
        是一个前缀，从 copy 提交占到该前缀终态；本信号量只包住
        `create_copy_task` 这一次 RPC，返回即还。前者挡住「还没传完就
        继续提交下一个前缀」，后者限建链速率。本信号量是**进程级**的，
        因为闸允许 4 个前缀同时提交，每 copy 各限 4 就会变成 16 个并发
        建链（#16：14 连发 ConnectToPeer FAILED / TRANSFER_FAIL；0.3.11
        实测 14 连发就打爆）。move 的 `create_move_task` 走同一把：两种
        建链都计入进程级在飞数。

        延迟创建：`Semaphore()` 构造时不绑循环（`daemon.py:_serve` 里
        `build_backend` 执行时循环已经在跑，构造绑不绑都不影响），但
        **首次有人排队等名额**时会 `_get_loop()`。所以「什么时候创建」
        不是正确性的关键——它既不造成、也不挡住跨循环的问题；只是把
        创建推迟到真正用得着的时候。`_submit_gate` 本身不含 `await`，
        事件循环线程上两次调用不会各建一把。

        **跨循环复用会炸**：同一个 backend 先在循环 A 里排过队、又在
        循环 B 里排队，会 `RuntimeError: bound to a different event
        loop`（名额没用完时不会——那种情况根本不去取循环）。当前不构成
        问题：生产只有一个循环，测试里同一个 backend 也只在一个 `run()`
        内用这把信号量。真要跨循环用，得按循环各建一把。
        """
        if self._submit_sem is None:
            self._submit_sem = asyncio.Semaphore(self._submit_concurrency)
        return self._submit_sem

    def open_segment_budget(self) -> contextvars.Token:
        """开始一轮等段剩余。新开一个空缓存：本轮的字节需求留到第一次判断时再算。

        与「准入」无关：这里不接纳任何请求，只是记住这条前缀要往目标段写多少字节。
        """
        return _admission_need.set(_AdmissionSlot())

    def close_segment_budget(self, token: contextvars.Token) -> None:
        """结束这一轮等待，丢掉缓存。"""
        _admission_need.reset(token)

    def _copy_plan(self, prefix: Prefix, target: InstanceId) -> list[tuple[str, str]]:
        keys = self._keys.keys_for_tokens(prefix.token_ids)
        if not keys:
            return []
        return self._keys.build_plan(keys, list(self._locate(target)))

    async def check_segment_room(
        self,
        prefix: Prefix,
        target: InstanceId,
        *,
        timeout_s: float | None = None,
    ) -> None:
        """闸外判断：这条前缀要写的新字节，目标段还放不放得下。

        放不下抛 AdmissionWait，段不存在抛 AdmissionStop。不在这里等。
        等待循环在 TaskEngine，而且在前缀闸外面。
        timeout_s 是剩余预算：这一次查询（取大小和查剩余）必须在预算内
        返回。不改比较本身。
        """
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        await self._admit_new_bytes(
            self._copy_plan(prefix, target), deadline=deadline
        )

    async def _admit_new_bytes(
        self,
        plan: list[tuple[str, str]],
        *,
        deadline: float | None = None,
    ) -> None:
        """判断这一批现在放不放得下。不 sleep，函数里没有提交。

        plan 是 (key, 目标段名)。只看计划里的第一个目标段：新占用只加
        落在这个段上、且副本端点里还没有它的 key。字节需求在一轮等待开始时
        用一次 batch_get_replica_desc 取齐，之后每轮只对这个段打一次
        GET /query_segment，不再重查大小。缓存键是计划第一条的段名加上
        计划里的 key 集合；对不上就重查大小。

        放不下或管理端口暂时连不上：AdmissionWait（闸外再等）。
        段名在 master 上不存在：AdmissionStop（这次预热停止）。
        没有 admin、或整批一个大小都没有：QueryError，拒绝提交。

        查完到真正占上之间，别的请求仍可能先占。这里不加锁、不向 master
        预留——只能少失败，不能保证提交时空间还在。
        """
        if not plan:
            return
        key = (plan[0][1], frozenset(k for k, _seg in plan))
        slot = _admission_need.get()
        if isinstance(slot, _AdmissionSlot) and slot.key == key:
            need = slot.need
        else:
            need = await self._new_bytes_by_segment(plan, deadline=deadline)
            if isinstance(slot, _AdmissionSlot):
                slot.key = key
                slot.need = need
        if need is None:
            return
        probe, nbytes = need
        await self._check_segment(probe, nbytes, deadline=deadline)

    async def _bounded_rpc(
        self, fn: Callable[..., Any], *args: Any, deadline: float | None
    ) -> Any:
        """deadline 为 None 时用默认 RPC 超时。有截止时间则不超过剩余预算。"""
        if deadline is None:
            return await self._rpc(fn, *args)
        left = deadline - time.monotonic()
        if left <= 0:
            raise QueryError(f"RPC timeout after 0s: {_name_of(fn)}")
        return await self._rpc(fn, *args, timeout_s=left)

    async def _check_segment(
        self, probe: str, nbytes: int, *, deadline: float | None = None
    ) -> None:
        """只对计划里的第一个目标段打一次 /query_segment。

        这一段的新占用大于这份剩余，整批不发。不查其他段。
        """
        if self._admin is None:
            raise QueryError("没有 admin 客户端，无法知道目标段剩余，拒绝提交")
        try:
            used, capacity = await self._bounded_rpc(
                self._admin.query_segment, probe, deadline=deadline
            )
        except SegmentNotFound as e:
            raise AdmissionStop(f"段不存在: {probe}") from e
        except AdminError as e:
            if e.status is None or (e.status is not None and e.status >= 500):
                logger.warning("准入：查询段 %s 失败（%s），闸外稍后重试", probe, e)
                raise AdmissionWait(f"管理端口暂时不可用: {probe}") from e
            raise QueryError(str(e)) from e
        # 容量为 0 当段不存在。真 master 在容量为 0 时已经用
        # 「Failed to query segment」变成 SegmentNotFound，到不了这里。
        # 再判一次是给假 admin：测试直接返回 (0, 0)，不经 HTTP 解析。
        if capacity == 0:
            raise AdmissionStop(f"段不存在: {probe}")
        remaining = capacity - used
        if nbytes > remaining:
            logger.info(
                "准入等待：段 %s 需要 %d 字节，剩余 %d 字节",
                probe,
                nbytes,
                remaining,
            )
            raise AdmissionWait(
                f"段放不下: {probe} need={nbytes} remaining={remaining}"
            )

    async def _new_bytes_by_segment(
        self,
        plan: list[tuple[str, str]],
        *,
        deadline: float | None = None,
    ) -> tuple[str, int] | None:
        """只算计划里第一个目标段上、还没有该段副本的 key。

        返回 (段名, 字节数)。None = 没有新占用。
        """
        keys = list(dict.fromkeys(key for key, _seg in plan))
        try:
            layout = await self._bounded_rpc(
                self._client.batch_memory_replica_sizes, keys, deadline=deadline
            )
        except StoreClientError as e:
            raise QueryError(str(e)) from e
        placed: dict[str, set[str]] = {}
        known: dict[str, int] = {}
        missing_attrs: list[str] = []
        sample_key: str | None = None
        sample_size: int | None = None
        for key in keys:
            endpoints: set[str] = set()
            size: int | None = None
            for row in layout.get(key, []):
                ep, sz, attrs = _replica_row(row)
                if ep:
                    endpoints.add(ep)
                if sz is not None and size is None:
                    size = sz
                if sz is None:
                    missing_attrs.extend(attrs)
            placed[key] = endpoints
            if size is not None:
                known[key] = size
                if sample_size is None:
                    sample_key = key
                    sample_size = size
        probe = plan[0][1]
        total = 0
        missing_n = 0
        seen: set[str] = set()
        for key, seg in plan:
            if seg != probe or key in seen:
                continue
            seen.add(key)
            # 已经有这个段的不计入，也不参加估算乘法。
            if seg in placed.get(key, ()):
                continue
            size = known.get(key)
            if size is None:
                if sample_size is None:
                    names = ", ".join(sorted(set(missing_attrs))) or "(无描述对象)"
                    raise QueryError(
                        "整批没有可读的副本大小，拒绝用 0 或常数顶上；"
                        f"描述属性: {names}"
                    )
                size = sample_size
                missing_n += 1
            total += size
        if missing_n:
            logger.info(
                "准入：%d 个 key 缺副本大小，按本批 key %s 的 %d 字节估算",
                missing_n,
                sample_key,
                sample_size,
            )
        if total <= 0:
            return None
        return (probe, total)
    def _query_gate(self) -> asyncio.Semaphore:
        """整个 backend 共用一把 query_task 信号量，并发不超过 4。

        不是每个 handle 一把。跨循环复用会
        ``RuntimeError: bound to a different event loop``，和
        ``_submit_sem`` 一样。
        """
        if self._query_sem is None:
            self._query_sem = asyncio.Semaphore(min(4, self._submit_concurrency))
        return self._query_sem

    async def copy(self, prefix: Prefix, target: InstanceId) -> TaskHandle:
        """在目标实例本机 seg 为前缀 KV 建副本（发起式；READY 经 query_task）。

        调用方只给实例，入参没有 segment。段从进程内目录 ``locate(target)``
        读。内部展开：前缀 → key 列表（引擎侧）→ 目标实例 seg 列表
        → rank 映射计划（引擎侧）→ 逐 key 发起复制任务。「复制前确认
        warm 已覆盖前缀」的存在性检查归编排层，不在本操作内。
        """
        keys = await self._expand_keys(prefix.token_ids)
        if not keys:
            raise IntentInputError(
                "prefix expands to no keys (shorter than one block?)"
            )
        targets = list(self._locate(target))
        plan = self._keys.build_plan(keys, targets)
        # 进闸之后、真正提交之前再查一次。不在这里等：放不下抛
        # AdmissionWait，调用方立刻出闸。取消落在这里时还没有任务号，
        # 不进下面的部分落账。
        await self._admit_new_bytes(plan)
        # 下标槽：并发完成顺序不定，按 plan 下标落号，失败时筛出已发出的。
        slots: list[object | None] = [None] * len(plan)
        # 进程级信号量（所有 copy 共用），不是本次 copy 独有的窗口。
        sem = self._submit_gate()
        stop = asyncio.Event()

        async def _submit_one(i: int, key: str, seg: str) -> None:
            async with sem:
                # 失败/取消后不再发新的：排队中的拿到名额就直接返回，在飞
                # 的照常收号（号必须落账，见下方 except）。
                if stop.is_set():
                    return
                slots[i] = await self._rpc(self._client.create_copy_task, key, [seg])

        tasks = [
            asyncio.create_task(_submit_one(i, key, seg))
            for i, (key, seg) in enumerate(plan)
        ]
        try:
            if tasks:
                await asyncio.gather(*tasks)
        except BaseException as e:
            # CancelledError 不是 Exception 子类；已发出的任务号必须落账，
            # 否则重启的 rebuild 捞不到这些在途传输。
            stop.set()
            # 排空：排队的子任务见到 `stop` 直接返回，不再发起。
            # 排空若跑完，已经在 `_rpc` 里的会写槽；排空**自己**被取消时，
            # `gather` 会把没结束的子任务一起取消，它们的号（这一次还在
            # `await _rpc` 的那些）就此丢掉——只有已经写进 `slots` 的能落账
            # （口径见 design/code-architecture.md 运行时结构
            # 「取消落在并发提交上」）。
            # 这里不 shield 子任务：那会把最长 `rpc_timeout_s`（30 秒）的
            # 等待带进退出路径。
            #
            # 要保证的是**记账不被跳过**：先有 key 失败 → 进到这里等其余
            # 在飞调用 → 退出信号到达。若让 CancelledError 直接冒出，读槽和
            # 记账都不跑，已拿到的号也会丢。所以吞下它、把异常换成取消，
            # 照常落账。已经有任务号就把句柄交回去；没有任务号才按取消结束。
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                e = asyncio.CancelledError()
            task_ids = [t for t in slots if t is not None]
            partial_plan = [plan[i] for i, t in enumerate(slots) if t is not None]
            handed: TaskHandle | None = None
            if task_ids:
                partial = TaskHandle(op=OpKind.COPY, task_ids=tuple(task_ids))
                handed = partial
                self._plans[partial] = (
                    prefix,
                    target,
                    partial_plan,
                    uuid.uuid4().hex,
                )
                try:
                    # 记账必须完成：已发出的号不落账 = 重启后没人跟踪。
                    # 若这次 shield 也被取消（第二次取消），记账会中断——
                    # 那是「取消两次」的极端场景，接受丢账（登记在
                    # code-architecture.md 运行时结构「取消落在并发提交上」）。
                    await asyncio.shield(self._record(partial, "running"))
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    pass  # 记账失败不再掩盖原始异常
                # 已经有任务号：把句柄交回去，不再把 CancelledError 抛出
                # 去，否则 await 拿不到返回值，调用方无法 abandon。
                if isinstance(e, asyncio.CancelledError):
                    return partial
            if isinstance(e, asyncio.CancelledError):
                raise e
            raise SubmitError(
                f"copy submit failed after {len(task_ids)}/{len(plan)} tasks "
                f"(submitted ids: {task_ids}): {e}",
                handle=handed,
            ) from e
        task_ids = [t for t in slots if t is not None]
        handle = TaskHandle(op=OpKind.COPY, task_ids=tuple(task_ids))
        self._plans[handle] = (prefix, target, plan, uuid.uuid4().hex)
        try:
            await asyncio.shield(self._record(handle, "running"))
        except asyncio.CancelledError:
            raise
        return handle

    async def query_task(self, handle: TaskHandle) -> TaskStatus:
        """聚合判定：任一任务失败 → FAILED（终态）；全部成功且副本核验
        通过 → READY（终态）；否则 RUNNING。终态即删核验登记。

        例外——在途冲突：失败任务的 message 全部含
        OBJECT_HAS_REPLICATION_TASK 时，说明别的任务在复制同 key
        （D1 §4 按「在途」不当失败），本方法转入副本观测态：冲突任务
        如实记 failed，之后只核验副本，副本到位即 READY。

        查询本身的 RPC 失败直接上抛（调用方可重试，不是任务终态）；
        副本核验失败（任务白跑）抛 VerifyError（终态，带明细）。
        """
        from mooncake.store import TaskStatus as StoreTaskStatus

        # 空任务号不是「已经完成」。
        if not handle.task_ids:
            raise ValueError("task handle has no task ids")
        entry = self._plans.get(handle)
        if entry is None:
            raise KeyError(f"unknown task handle: {handle}")
        _, _, plan, _ = entry
        if handle in self._watch:
            # 在途冲突观测态：任务已死（冲突失败，账已记），只等副本到位
            return await self._verify_watch(handle, plan)
        sem = self._query_gate()

        async def _one(tid: object) -> object:
            async with sem:
                return await self._rpc(self._client.query_task, tid)

        try:
            resps = list(await asyncio.gather(*(_one(tid) for tid in handle.task_ids)))
        except StoreClientError as e:
            raise QueryError(str(e)) from e
        failed = [r for r in resps if r.status == StoreTaskStatus.FAILED]
        if failed:
            conflicted = tuple(
                tid
                for tid, r in zip(handle.task_ids, resps)
                if r.status == StoreTaskStatus.FAILED
                and "OBJECT_HAS_REPLICATION_TASK" in getattr(r, "message", "")
            )
            if len(conflicted) == len(failed):
                # 全部失败都是在途冲突：别的任务在复制同 key（典型 = 同
                # intent 重发）。按 D1 §4 不当失败——冲突任务如实记
                # failed，转副本观测态，立刻核一轮。
                #
                # 写回 `_watch` 与校验放在同一个不跨 await 的块里：上面跨
                # 了 await（query_task 的 RPC），若期间该 handle 已被
                # abandon（entry 删除），再写会留下孤儿条目。先校验+写回，
                # 再记账（记账本身是 await，必须放在字典操作之后）。
                if handle not in self._plans:
                    raise KeyError(f"task handle abandoned concurrently: {handle}")
                self._watch[handle] = conflicted
                await self._record(handle, "failed", tids=list(conflicted))
                return await self._verify_watch(handle, plan)
            await self._finish(handle, "failed")
            return TaskStatus.FAILED
        if not all(r.status == StoreTaskStatus.SUCCESS for r in resps):
            return TaskStatus.RUNNING
        # READY 核验：每个 key 在其映射目标上有内存副本（不是每个 seg）
        missing = await self._verify_missing(plan)
        if missing:
            await self._finish(handle, "failed")
            raise VerifyError(
                "task(s) succeeded but replica verify failed:\n  "
                + "\n  ".join(missing)
            )
        await self._finish(handle, "ready")
        return TaskStatus.READY

    @staticmethod
    def _require_admin_rows(keys: Sequence[str], parsed: dict[str, Any]) -> None:
        """admin 请求的 key 必须全部返回且 ok；否则是可重试的 QueryError。"""
        missing = [key for key in keys if key not in parsed]
        if missing:
            raise QueryError(
                "batch_query_keys 短回，缺 key: " + ", ".join(missing[:4])
            )
        failed = [
            (key, parsed[key].error) for key in keys if not parsed[key].ok
        ]
        if failed:
            detail = ", ".join(
                f"{key}({error or 'ok=false'})" for key, error in failed[:4]
            )
            raise QueryError("batch_query_keys 查询失败: " + detail)

    async def _verify_missing(self, plan: list[tuple[str, str]]) -> list[str]:
        """副本核验：每个 key 在其映射目标上有 DRAM 内存副本；返回缺失明细
        （空 = 通过）。

        走主线 GET /batch_query_keys（零副作用）。段还在不在，用副本描述
        里的传输端点去比。只用 ``memory``（``values``）；``nof`` 不是 DRAM，
        不计 READY。不用 batch_get_replica_desc：那条会续租、计命中。
        查询失败抛 QueryError（可重试）。
        """
        if self._admin is None:
            raise QueryError(
                "副本核验需要主线 GET /batch_query_keys，未配置 admin"
            )
        keys = [key for key, _ in plan]
        try:
            parsed = await self._rpc(self._admin.batch_query_keys, keys)
        except (AdminMainError, StoreClientError) as e:
            raise QueryError(str(e)) from e
        self._require_admin_rows(keys, parsed)
        missing: list[str] = []
        for key, seg in plan:
            endpoints = [m.transport_endpoint for m in parsed[key].memory]
            if seg not in endpoints:
                missing.append(f"{key} missing on {seg} (have={sorted(endpoints)})")
        return missing

    async def _verify_watch(
        self, handle: TaskHandle, plan: list[tuple[str, str]]
    ) -> TaskStatus:
        """在途冲突观测：副本全部到位 → READY；否则 RUNNING 继续等。

        局限：在途任务的目标 seg 未必是本计划的 seg（可能来自别的
        intent）——等不到由编排层超时兜底（abandon）。核验失败不抛
        VerifyError：本模式不保证「任务成功过」，缺失只意味着还没传完。
        """
        missing = await self._verify_missing(plan)
        if missing:
            return TaskStatus.RUNNING
        # 目的达成（副本核验为准）：非冲突任务记 ready；冲突任务保持
        # failed（如实——它们确实失败了，数据是别人搬的）。
        # 字典读取集中在 await 之前（`_watch` 取出即删，之后不再依赖它）
        conflicted = set(self._watch.pop(handle, ()))
        rest = [t for t in handle.task_ids if t not in conflicted]
        # entry 已不在册（并发 abandon）：账已由对方处理，不再记
        if self._plans.get(handle) is None:
            return TaskStatus.READY
        if rest:
            # 记账需要 entry（_record 内部从 _plans 取），所以 entry 必须
            # 留到记账之后再删——见下方 pop。
            await self._record(handle, "ready", tids=rest)
        self._plans.pop(handle, None)
        return TaskStatus.READY

    async def abandon(self, handle: TaskHandle) -> None:
        """放弃跟踪：Ledger 记 abandoned 并删登记（不取消后端任务——后
        端无任务取消能力，在途传输可能仍完成；副本清理由 remove 负责）。
        观测态的冲突任务保持 failed 不改写。

        **调用约束**：必须在「推进该 handle 的那条协程」里调用（超时收口
        就是这样）。不得另起一条协程对同一 handle 调它——那会与还停在
        `query_task` 里的字典更新交错（见 `_plans` 定义处的纪律说明）。"""
        if handle not in self._plans:
            raise KeyError(f"unknown task handle: {handle}")
        conflicted = set(self._watch.pop(handle, ()))
        rest = [t for t in handle.task_ids if t not in conflicted]
        if rest:
            try:
                # 和 copy 里记 running 一样：取消打在这次 sqlite 写上时
                # 不能把 abandon 拦腰截断。shield 再被取消（取消两次）
                # 仍按 E11 已知遗留接受丢账，不再套一层。
                await asyncio.shield(self._record(handle, "abandoned", tids=rest))
            except asyncio.CancelledError:
                raise
        self._plans.pop(handle, None)

    async def exists(self, prefix: Prefix) -> bool:
        """全部 key 在池才为真（batch_is_exist；读即续租）。

        查询本身失败（返回值不是 0/1/True/False，或列表长度和 key 数
        不一致）抛 QueryError——「查不出来」与「不存在」语义不同：
        前者调用方可重试，后者才触发重算/预热。短回不当成不在池。
        """
        keys = await self._expand_keys(prefix.token_ids)
        if not keys:
            return False
        try:
            result = await self._rpc(self._client.is_exist, keys)
        except StoreClientError as e:
            raise QueryError(str(e)) from e
        if len(result) != len(keys):
            raise QueryError(
                f"batch_is_exist size mismatch keys={len(keys)} results={len(result)}"
            )
        return all(result)

    def prefix_of(self, handle: TaskHandle) -> Prefix | None:
        """句柄登记的前缀（_plans 第一项）。终态删除登记后为 None——
        调用方（TaskEngine.track）需在轮询前取。"""
        entry = self._plans.get(handle)
        return entry[0] if entry is not None else None

    def target_of(self, handle: TaskHandle) -> InstanceId | None:
        """句柄登记的目标实例（_plans 第二项）。终态删除登记后为 None——
        调用方（TaskEngine.track）需在轮询前取，READY 时才能按实例补发。"""
        entry = self._plans.get(handle)
        return entry[1] if entry is not None else None

    # ------------------------------------------------------------------
    # 任务状态记录（归实现侧，不归编排层）
    #
    # 记这笔账需要两样东西都齐：①任务号的持久化编码——mooncake 任务号
    # 是整数对，编码知识只在本层，编排层不该懂；②状态迁移的时机——
    # query_task 是本层判定的，顺手迁移。编排层记账则两头都缺：编码要
    # 本层翻译好再上交，路径一多还会漏（超时、异常分支没人写）。原则：
    # 谁手里有原始信息，谁就记账。（状态机可丢：裸状态可按 task_id
    # 从 master 重查；跟踪关系由 rebuild() 用可重算输入现场重算重建）
    # ------------------------------------------------------------------

    async def _record(
        self, handle: TaskHandle, status: str, tids: list[object] | None = None
    ) -> None:
        if self._ledger is None:
            return
        entry = self._plans.get(handle)
        if entry is None:
            return
        prefix, target, _, handle_id = entry
        await self._in_ledger(
            self._ledger.upsert_tasks,
            [encode_task_id(tid) for tid in (tids if tids is not None else handle.task_ids)],
            handle_id=handle_id,
            op=handle.op.value,
            prefix=prefix,
            target=str(target),
            status=status,
        )

    def rebuild(self) -> list[TaskHandle]:
        """重启后恢复 running 任务的跟踪登记（tasks 表存的是可重算输
        入——model_id/token_ids/target/op；plan 由 KeyComputer 现场重
        算，key/seg 不落库）。返回恢复的 handle 列表（E11 进程壳的恢
        复轮询以返回值为输入；长度 = 恢复数）。

        规则：
        - 恢复 copy 与 move（已落地的任务型操作）；promote 仍跳过。
          源段不落库，恢复只重算目标侧 plan，供 query_task 核验目标副本
        - 空 handle_id / 空 token_ids / 重算出空 plan：跳过（数据残缺行
          或口径漂移——登记空 plan 会在任务成功后假 READY）
        - 重算失败（实例已不在）：跳过，交由 gc_tasks / 下一次预热
        - 观测态恢复：同组已有冲突 failed 兄弟（部分 running 部分
          failed 的组，failed 行只可能是冲突失败——真失败会整组终态），
          恢复的 handle 同时进 _watch：冲突任务保持 failed，之后只核验
          副本，缺副本保持 RUNNING（不抛 VerifyError）
        - 全部任务均冲突失败的组**不恢复**——这不是缺陷：该组自己没
          有在途传输。副本到位的判断链：第一遍任务组若仍是 running，
          由它恢复跟踪并核验；否则下一次请求的前置检查（exists）重新
          判断——副本未到则重新 copy（对方还在传则再次撞车进观测态）。
          任何路径都不存在「副本没到却报成功」。本组的「等副本」只是
          给重复请求的回执，重启时调用方连接已断，回执本无接收对象

        已知边界（多模型/多后端实例共用同一 Ledger 时）：本方法不区分
        任务归属——账本里全部 running 任务都按本实例的 KeyComputer
        （单模型、单 TP 组合）重算 plan，别的模型/实例的任务会被错算
        并错写状态。当前部署形态（单实例单模型）无此问题；多模型前必
        须先加归属判定（行上的 model_id 与本实例不符则跳过）。
        """
        if self._ledger is None:
            return []
        running = self._ledger.list_running_tasks()
        groups: dict[str, list] = {}
        for rec in running:
            groups.setdefault(rec.handle_id, []).append(rec)
        restored: list[TaskHandle] = []
        for handle_id, rows in groups.items():
            if not handle_id:
                continue
            r0 = rows[0]
            if r0.op not in (OpKind.COPY.value, OpKind.MOVE.value) or not r0.token_ids:
                continue
            prefix = Prefix(model_id=r0.model_id, token_ids=r0.token_ids)
            target = InstanceId(r0.target)
            try:
                keys = self._keys.keys_for_tokens(prefix.token_ids)
                plan = self._keys.build_plan(keys, list(self._locate(target)))
            except Exception:
                continue
            if not plan:
                continue
            # 部分提交的 handle 只落了已发出的任务号；rebuild 按 token_ids
            # 重算的是全量 plan，若按全量核验会把从未提交的 key 当成缺副本。
            # 这类 handle 不参与重建（在途任务重启后不再跟踪，下一次
            # exists / 预热重新判断；见 A07）。
            all_rows = self._ledger.list_tasks_by_handle(handle_id)
            if len(all_rows) != len(plan):
                logger.warning(
                    "rebuild: skip partial handle %s (%d/%d tasks)",
                    handle_id,
                    len(all_rows),
                    len(plan),
                )
                continue
            handle = TaskHandle(
                op=OpKind(r0.op),
                task_ids=tuple(decode_task_id(r.task_id) for r in rows),
            )
            self._plans[handle] = (prefix, target, plan, handle_id)
            # 观测态恢复：同组的冲突 failed 兄弟进 _watch
            conflicted = tuple(
                decode_task_id(r.task_id)
                for r in self._ledger.list_tasks_by_handle(handle_id)
                if r.status == "failed"
            )
            if conflicted:
                self._watch[handle] = conflicted
            restored.append(handle)
        return restored

    async def _finish(self, handle: TaskHandle, status: str) -> None:
        """终态处理：Ledger 迁移 + 删核验登记（防长驻进程泄漏）。"""
        await self._record(handle, status)
        self._watch.pop(handle, None)
        self._plans.pop(handle, None)

    # ------------------------------------------------------------------
    # 以下操作随能力接线逐个落地
    # ------------------------------------------------------------------

    async def move(
        self,
        prefix: Prefix,
        target: InstanceId,
        source: InstanceId,
    ) -> TaskHandle:
        """把前缀 KV 从源实例挪到目标实例（一次一个 key）。

        调用方只给实例，入参没有 segment。内部自己 ``locate(源)`` 和
        ``locate(目标)``，两边可以是不同的段。与 copy 同形：前缀展开 →
        rank 映射计划 → 进程级信号量逐 key 提交 → 句柄登记，READY 仍走
        query_task 的目标侧副本核验。绑定 ``create_move_task(key, source,
        target)`` 的 source 是段名且必填，所以源实例不能省——段从目录里
        按 rank 映射出来，不是调用方传入的。

        提交前要先做容量判断。plan 与 copy 同形，是 ``(key, 目标 seg)``
        列表：目标段要新占一份。``_admit_new_bytes`` 在
        ``feat/kvcm-segment-admission``（PR 72），本类不定义它。实例上
        没有这份可调用对象时，``move`` 在任何 ``create_move_task`` 之前
        抛出 ``RuntimeError``，错误信息点名 ``_admit_new_bytes``。测试可以
        往实例上赋一个假实现来走提交路径。删除不占新内存，不走这里。

        没有实例销毁事件，本方法不被 daemon / TaskEngine 调用。
        移动成功后，源实例上这份 KV 没有了，要发 BlockRemoved；目标实例要发
        BlockStored。发布端在 E14 分支。两分支合入后再接。本次 move 不发事件。
        """
        keys = await self._expand_keys(prefix.token_ids)
        if not keys:
            raise IntentInputError(
                "prefix expands to no keys (shorter than one block?)"
            )
        plan = self._keys.build_plan(keys, list(self._locate(target)))
        source_plan = self._keys.build_plan(keys, list(self._locate(source)))
        # 容量判断在任何 create_move_task 之前。拒绝则一个任务都不发。
        # 方法本身不在本类：它在 feat/kvcm-segment-admission（PR 72）。
        admit = getattr(self, "_admit_new_bytes", None)
        if not callable(admit):
            raise RuntimeError(
                "move 在 create_move_task 之前需要 _admit_new_bytes；"
                "该方法在 feat/kvcm-segment-admission（PR 72），"
                "本分支没有这个方法"
            )
        await admit(plan)
        moves = [
            (key, src_seg, tgt_seg)
            for (key, tgt_seg), (_, src_seg) in zip(plan, source_plan)
        ]
        slots: list[object | None] = [None] * len(moves)
        sem = self._submit_gate()
        stop = asyncio.Event()

        async def _submit_one(i: int, key: str, src_seg: str, tgt_seg: str) -> None:
            async with sem:
                if stop.is_set():
                    return
                slots[i] = await self._rpc(
                    self._client.create_move_task, key, src_seg, tgt_seg
                )

        tasks = [
            asyncio.create_task(_submit_one(i, key, src_seg, tgt_seg))
            for i, (key, src_seg, tgt_seg) in enumerate(moves)
        ]
        try:
            if tasks:
                await asyncio.gather(*tasks)
        except BaseException as e:
            # 与 copy 相同：已发出的任务号必须落账，取消不能把它们丢掉。
            stop.set()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                e = asyncio.CancelledError()
            task_ids = [t for t in slots if t is not None]
            partial_plan = [plan[i] for i, t in enumerate(slots) if t is not None]
            handed: TaskHandle | None = None
            if task_ids:
                partial = TaskHandle(op=OpKind.MOVE, task_ids=tuple(task_ids))
                handed = partial
                self._plans[partial] = (
                    prefix,
                    target,
                    partial_plan,
                    uuid.uuid4().hex,
                )
                try:
                    await asyncio.shield(self._record(partial, "running"))
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    pass
            if isinstance(e, asyncio.CancelledError):
                raise e
            raise SubmitError(
                f"move submit failed after {len(task_ids)}/{len(plan)} tasks "
                f"(submitted ids: {task_ids}): {e}",
                handle=handed,
            ) from e
        task_ids = [t for t in slots if t is not None]
        handle = TaskHandle(op=OpKind.MOVE, task_ids=tuple(task_ids))
        self._plans[handle] = (prefix, target, plan, uuid.uuid4().hex)
        try:
            await asyncio.shield(self._record(handle, "running"))
        except asyncio.CancelledError:
            raise
        return handle

    async def _require_ledger(self):
        if self._ledger is None:
            raise RuntimeError("保活登记需要 Ledger")

    async def register_keepalive(self, prefix: Prefix) -> None:
        """登记保活。写入与 KeepaliveLoop.register 同一张 keepalive 表。

        只写登记，不发续租 RPC；周期续租由 KeepaliveLoop 调 renew_once。
        """
        await self._require_ledger()
        await self._in_ledger(self._ledger.register_keepalive, prefix)

    async def unregister_keepalive(self, prefix: Prefix) -> None:
        """注销保活。同一张表，DELETE 这一条前缀。"""
        await self._require_ledger()
        await self._in_ledger(self._ledger.unregister_keepalive, prefix)

    async def renew_once(self, prefix: Prefix) -> bool:
        """对前缀执行一次续租：exists → BatchExistKey（不改成 ProbeKey）。

        返回是否全部 key 仍在池；RPC 失败按 exists 的约定抛 QueryError。
        """
        return await self.exists(prefix)

    async def demote(self, prefix: Prefix) -> None:
        """先确认能展开成 key、并且 admin 已配置，再注销保活。

        然后等满一个 lease TTL，再查是否已有盘副本。没有马上撤销
        lease 的调用，也不查剩余租约，所以睡满 ``lease_ttl_s``
        （须与 master 的 ``default_kv_lease_ttl`` 一致）。
        然后用已有的 ``GET /batch_query_keys`` 看 disk / local_disk 条数。
        不用 ``GetReplicaList`` / ``batch_get_replica_desc``，那会续租，
        把刚停的保活又续上。每个 key 都有盘副本才算成功。
        写回且池不满时仍会失败：不读数据落盘，不调 ``MountLocalDiskSegment``。
        展开不出 key、或没配 admin，在注销保活之前就失败。
        失败或取消只恢复原本就有的保活行，并写回原来的
        ``registered_at``。原来没有行不新建。成功不加回。
        """
        keys = await self._expand_keys(prefix.token_ids)
        if not keys:
            raise IntentInputError(
                "prefix expands to no keys (shorter than one block?)"
            )
        if self._admin is None:
            raise QueryError("降层查盘副本需要主线 GET /batch_query_keys")
        if self._cap.has_offload.channel is not Support.YES:
            raise UnsupportedCapabilityError(
                "当前能力集没有盘副本字段，不能降层"
            )
        await self._require_ledger()
        registered_at = await self._in_ledger(
            self._ledger.keepalive_registered_at, prefix
        )
        await self.unregister_keepalive(prefix)
        try:
            await asyncio.sleep(self._lease_ttl_s)
            try:
                parsed = await self._rpc(self._admin.batch_query_keys, keys)
            except (AdminMainError, StoreClientError) as e:
                raise QueryError(str(e)) from e
            self._require_admin_rows(keys, parsed)
            missing = [key for key in keys if parsed[key].local_disk_replicas < 1]
            if missing:
                raise VerifyError(
                    "demote: 还没有 LOCAL_DISK 副本，降层未完成: "
                    + ", ".join(missing[:4])
                )
        except asyncio.CancelledError:
            try:
                await self._restore_demote_keepalive(prefix, registered_at)
            except BaseException:
                pass
            raise
        except (QueryError, VerifyError):
            await self._restore_demote_keepalive(prefix, registered_at)
            raise

    async def _restore_demote_keepalive(
        self, prefix: Prefix, registered_at: float | None
    ) -> None:
        """只把停之前就有的保活行按原 registered_at 插回去。"""
        if registered_at is None:
            return
        await self._in_ledger(
            self._ledger.restore_keepalive, prefix, registered_at
        )

    def _require_promotion(self) -> None:
        if self._cap.has_promotion.channel is not Support.YES:
            raise UnsupportedCapabilityError("提回通道不具备")

    async def promote(self, prefix: Prefix, target: InstanceId) -> None:
        """只触发提回：打 ``promote_hits`` 次 GetReplicaList，不验证、不等待。

        `PromoteFlow.run()` 用 `check_promote` 轮询；L3 没有 TaskStatus.READY。
        target 仅保留给上层 flow 的验证语义，触发本身是 key 级。
        """
        keys = await self._expand_keys(prefix.token_ids)
        if not keys:
            raise IntentInputError(
                "prefix expands to no keys (shorter than one block?)"
            )
        self._require_promotion()
        for _ in range(self._promote_hits):
            await self._rpc(self._client.batch_memory_replica_endpoints, keys)

    async def check_promote(self, prefix: Prefix, target: InstanceId) -> bool:
        """查 target 是否已有该前缀全部 key 的 memory replica。

        用 GetReplicaList 的返回端点对 locate(target)；查询本身也会继续
        累计 promotion 命中，属于预期副作用。
        """
        keys = await self._expand_keys(prefix.token_ids)
        if not keys:
            raise IntentInputError(
                "prefix expands to no keys (shorter than one block?)"
            )
        self._require_promotion()
        plan = self._keys.build_plan(keys, list(self._locate(target)))
        last = await self._rpc(
            self._client.batch_memory_replica_endpoints, keys
        )
        return all(seg in last.get(key, []) for key, seg in plan)

    async def retain(self, prefix: Prefix, target: InstanceId | None = None) -> TaskHandle:
        raise NotImplementedError("retain：做法见 D2 §4，尚未接通")

    async def remove(self, prefix: Prefix, force: bool = False) -> None:
        """删除该前缀展开出的全部 key，每个 key 的全部副本一起删；不支持 target。

        无论 force，都先 ``unregister_keepalive``。``force=False`` 再等满一个
        ``lease_ttl_s``，然后 ``batch_remove(..., force=False)``。
        ``force=True`` 注销保活后立刻 ``batch_remove(..., force=True)``，不等。
        失败或取消不把保活加回去：停过就留停。调用方看到原因后自己重试，
        租约可以过期。成功路径也不加回。
        不占新内存，不调用 ``_admit_new_bytes``。
        """
        keys = await self._expand_keys(prefix.token_ids)
        if not keys:
            raise IntentInputError(
                "prefix expands to no keys (shorter than one block?)"
            )
        await self.unregister_keepalive(prefix)
        if not force:
            await asyncio.sleep(self._lease_ttl_s)
        await self._rpc(self._client.batch_remove, keys, force)
