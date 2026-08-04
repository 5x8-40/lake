"""长期 WorkerEngine 单环（C6）——无 gRPC 依赖，便于单测。

参考:vLLM EngineCore 单环；lake：一份 NodeScheduler + 入队 drain。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from lake.engine.model_runner import ModelRunner
from lake.engine.pool_iface import PoolIface
from lake.runtime.executor import RuntimeExecutor, SingleProcessExecutor
from lake.runtime.lifecycle import CapacitySignal, WorkerLifecycle, WorkerState
from lake.runtime.node_scheduler import NodeScheduler
from lake.runtime.prefix_hint import PrefixHint
from lake.runtime.req import Req
from lake.runtime.role import RoleConfig

LOG = logging.getLogger("lake.worker_engine")


@dataclass
class _Inbound:
    req: Req
    hint: Optional[PrefixHint]
    done: threading.Event
    error: list  # 0-or-1 Exception
    result: list  # 0-or-1 Req


class WorkerEngine:
    """长期 EngineCore 式进程内核：入队 + 单 step 环。

    对齐 vLLM 单 EngineCore：禁止每请求新建 Scheduler / 并行 prepare。

    停机契约：`stop` 只设 flag + sentinel；**由 loop 线程** `_fail_inflight`
    后退出。调用方 `join`；若超时则只打日志，**不得**再碰 scheduler / inflight
    （timeout 后勿再 submit，进程应退出）。
    """

    def __init__(
        self,
        pool: PoolIface,
        runner: ModelRunner,
        role: Optional[RoleConfig] = None,
        *,
        coalesce_s: float = 0.005,
        executor: Optional[RuntimeExecutor] = None,
    ) -> None:
        self._role = role or RoleConfig()
        self._pool = pool
        self._runner = runner
        self._executor = executor or SingleProcessExecutor(runner)
        self._coalesce_s = max(0.0, coalesce_s)
        self._life = WorkerLifecycle(WorkerState.IDLE)
        self._sched = NodeScheduler(
            pool,
            runner,
            self._role,
            on_req_finished=self._on_req_finished,
            executor=self._executor,
        )
        self._inbound: "queue.Queue[Optional[_Inbound]]" = queue.Queue()
        self._inflight: Dict[str, _Inbound] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="lake-worker-engine", daemon=True)
        self._started = False

    @property
    def scheduler(self) -> NodeScheduler:
        return self._sched

    @property
    def role(self) -> RoleConfig:
        return self._role

    @property
    def lifecycle(self) -> WorkerLifecycle:
        return self._life

    def capacity_signal(self) -> CapacitySignal:
        """上报用快照（不限流）。"""
        with self._lock:
            inflight = len(self._inflight)
        return CapacitySignal(
            waiting=self._sched.num_waiting,
            running=self._sched.num_running,
            inflight_reqs=inflight,
            max_running_reqs=self._role.max_running_reqs,
            state=self._life.state,
            role=self._role.role.value,
            model_backend=self._role.model_backend,
            served_model_name=self._runner.served_model_name,
            model_loaded=self._runner.model_loaded,
            model_warmed=self._runner.model_warmed,
        )

    def start(self) -> None:
        with self._lock:
            if self._started or self._thread.is_alive():
                return
            # C12：Boot→Warm(load/warmup)→Ready→Serving。
            self._life.advance(WorkerState.BOOT)
            self._life.warm()
            self._runner.load_model(
                model_path=self._role.model_path,
                served_model_name=self._role.served_model_name,
                revision=self._role.model_revision,
            )
            self._runner.warmup(
                num_reqs=self._role.warmup_num_reqs,
                tokens_per_req=self._role.warmup_tokens_per_req,
            )
            self._life.ready()
            self._life.serve()
            self._started = True
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """请求停机并等待 loop 自行收尾。

        超时后不清理 scheduler/inflight（避免与仍存活的 step 线程竞态）。
        """
        with self._lock:
            if self._life.state == WorkerState.TERMINATE:
                return
            if not self._started:
                # 从未 start：无 loop 可 join，直接终态
                self._life.advance(WorkerState.TERMINATE)
                return
            first = not self._stop.is_set()
            if first:
                self._life.drain()
                self._stop.set()
                self._inbound.put(None)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            LOG.error(
                "WorkerEngine.stop timed out after %.3fs; step thread still alive. "
                "Do not submit; process should exit. "
                "Caller will not clear scheduler/inflight.",
                timeout,
            )
            return
        self._life.advance(WorkerState.TERMINATE)

    def submit(self, req: Req, hint: Optional[PrefixHint] = None) -> Req:
        """阻塞直到该请求 finished（或引擎故障）。返回完成后的 Host Req。"""
        item = _Inbound(req=req, hint=hint, done=threading.Event(), error=[], result=[])
        with self._lock:
            if self._stop.is_set() or not self._started:
                raise RuntimeError("WorkerEngine is not running")
            if not self._life.accepts_new_requests():
                raise RuntimeError(
                    f"worker not accepting requests in state={self._life.state.value}"
                )
            self._inbound.put(item)
        item.done.wait()
        if item.error:
            raise item.error[0]
        if not item.result:
            raise RuntimeError(f"submit finished without result req_id={req.req_id}")
        return item.result[0]

    def _drain_inbound(self) -> None:
        while True:
            try:
                item = self._inbound.get_nowait()
            except queue.Empty:
                break
            if item is None:
                if self._stop.is_set():
                    break
                continue
            rid = item.req.req_id
            with self._lock:
                if rid in self._inflight or self._sched.has_req(rid):
                    item.error.append(ValueError(f"duplicate req_id={rid}"))
                    item.done.set()
                    continue
                self._inflight[rid] = item
            try:
                self._sched.add_request(item.req, hint=item.hint)
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._inflight.pop(rid, None)
                item.error.append(e)
                item.done.set()

    def _on_req_finished(self, req: Req) -> None:
        with self._lock:
            inn = self._inflight.pop(req.req_id, None)
        if inn is None:
            return
        finished = self._sched.release_req(req.req_id) or req
        inn.result.append(finished)
        inn.done.set()

    def _fail_inflight(self, exc: BaseException) -> None:
        with self._lock:
            items = list(self._inflight.values())
            self._inflight.clear()
        for inn in items:
            # 故障/停机：尽量打 agent 收尾，避免池侧 ref/槽泄漏
            req = self._sched.abandon_req(inn.req.req_id) or inn.req
            try:
                self._pool.on_request_finished(req)
            except Exception:  # noqa: BLE001
                LOG.exception("on_request_finished during fail req=%s", req.req_id)
            if not inn.done.is_set():
                inn.error.append(exc)
                inn.done.set()

    def _reject_inbound(self, exc: BaseException) -> None:
        """停机时丢弃队列中尚未登记的 inbound，避免调用方永久 wait。"""
        while True:
            try:
                item = self._inbound.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            if not item.done.is_set():
                item.error.append(exc)
                item.done.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._drain_inbound()
            if not self._sched.has_work():
                try:
                    item = self._inbound.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    # 哨兵：先抽干其后真实请求，再退出（避免 submit 永久 wait）
                    self._drain_inbound()
                    if self._stop.is_set() and not self._sched.has_work():
                        break
                    continue
                self._inbound.put(item)
                continue
            # 空闲→忙碌时短窗 coalesce，吞掉几乎同时到达的并发 submit（C6）
            if self._coalesce_s > 0:
                t0 = time.monotonic()
                while time.monotonic() - t0 < self._coalesce_s:
                    self._drain_inbound()
                    time.sleep(min(0.001, self._coalesce_s))
            try:
                self._sched.run_until_idle(
                    before_schedule=self._drain_inbound,
                    should_stop=self._stop.is_set,
                )
            except Exception as e:  # noqa: BLE001
                LOG.exception("WorkerEngine step loop failed: %s", e)
                self._fail_inflight(e)
                self._reject_inbound(e)
                self._stop.set()
                break
        # 正常停机：由 loop 自己收尾（调用方 stop 不得再碰 scheduler）
        stopped = RuntimeError("WorkerEngine stopped")
        self._reject_inbound(stopped)
        if self._inflight:
            self._fail_inflight(stopped)
