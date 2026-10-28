"""E12 gRPC 通道检查：真 server + 真 TaskEngine（fake KvOps），走完整
RPC 往返——幂等去重、状态回执、参数校验、关闭语义。离线可跑。

TaskEngine 是协程引擎（E11 执行模型），所以测试里起一条后台事件循环
线程承载引擎，gRPC servicer 从它自己的线程池调 `submit_intent`（这是
生产形态的缩影：gRPC 收请求 → 入队 → 协程执行）。
"""

from __future__ import annotations

import asyncio
import threading
import time

import grpc
import pytest

from kvcm.api.grpc_server import IntentGrpcServer
from kvcm.api.pb import kvcm_intent_pb2 as pb
from kvcm.api.pb import kvcm_intent_pb2_grpc as pb_grpc
from kvcm.ops.base import TaskStatus
from kvcm.ops.types import InstanceId, Prefix
from kvcm.task_engine import IntentStatus, TaskEngine, intent_id_of

from test_task_engine import FakeOps  # 同目录测试模块复用

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")


class _LoopThread:
    """后台事件循环线程（承载 TaskEngine）。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="test-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def call(self, coro, timeout=10):
        """在循环线程里跑协程并等结果。"""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@pytest.fixture()
def rpc():
    """起真 gRPC server（ephemeral 端口）+ stub；yield (start, holder)。"""
    holder = {}

    def _start(ops: FakeOps):
        loop_thread = _LoopThread()
        engine = TaskEngine(ops)
        loop_thread.call(_start_engine(engine))
        server = IntentGrpcServer(("127.0.0.1", 0), engine)
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{server.port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        holder.update(
            engine=engine, server=server, channel=channel, ops=ops, lt=loop_thread
        )
        return pb_grpc.KvcmIntentStub(channel)

    yield _start, holder
    if not holder:
        return
    holder["channel"].close()
    holder["server"].stop()
    holder["lt"].call(holder["engine"].shutdown(timeout_s=2))
    holder["lt"].stop()


async def _start_engine(engine: TaskEngine) -> None:
    engine.start()


def _req(prefix: Prefix = _P, target: str = "inst-B") -> pb.SubmitIntentRequest:
    return pb.SubmitIntentRequest(
        prefix=pb.Prefix(model_id=prefix.model_id, token_ids=prefix.token_ids),
        target=target,
    )


def _wait_status(stub, intent_id, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    status = pb.GetTaskResponse.STATUS_UNKNOWN
    while time.monotonic() < deadline:
        status = stub.GetTask(pb.GetTaskRequest(intent_id=intent_id)).status
        if status == want:
            return status
        time.sleep(0.02)
    return status


def test_submit_then_get_task_ready(rpc) -> None:
    start, holder = rpc
    stub = start(FakeOps(seq=[TaskStatus.RUNNING, TaskStatus.READY]))
    resp = stub.SubmitIntent(_req())
    assert resp.intent_id == intent_id_of(_P, _T)
    assert _wait_status(stub, resp.intent_id, pb.GetTaskResponse.READY) == (
        pb.GetTaskResponse.READY
    )


def test_dedup_in_flight_same_intent_id(rpc) -> None:
    start, holder = rpc
    gate = threading.Event()
    ops = FakeOps(seq=[TaskStatus.READY] * 4, query_gate=gate)
    stub = start(ops)
    r1 = stub.SubmitIntent(_req())
    r2 = stub.SubmitIntent(_req())  # 在途重复 → 去重
    assert r1.intent_id == r2.intent_id
    gate.set()
    assert _wait_status(stub, r1.intent_id, pb.GetTaskResponse.READY) == (
        pb.GetTaskResponse.READY
    )


def test_failed_status_and_detail(rpc) -> None:
    start, holder = rpc
    stub = start(FakeOps(seq=[TaskStatus.FAILED]))
    resp = stub.SubmitIntent(_req())
    assert _wait_status(stub, resp.intent_id, pb.GetTaskResponse.FAILED) == (
        pb.GetTaskResponse.FAILED
    )
    detail = stub.GetTask(pb.GetTaskRequest(intent_id=resp.intent_id)).detail
    assert "PrefetchError" in detail  # 失败必须带原因


def test_unknown_intent_id(rpc) -> None:
    start, holder = rpc
    stub = start(FakeOps())
    resp = stub.GetTask(pb.GetTaskRequest(intent_id="nope"))
    assert resp.status == pb.GetTaskResponse.STATUS_UNKNOWN


def test_invalid_request_aborts(rpc) -> None:
    start, holder = rpc
    stub = start(FakeOps())
    with pytest.raises(grpc.RpcError) as ei:
        stub.SubmitIntent(
            pb.SubmitIntentRequest(prefix=pb.Prefix(model_id="", token_ids=[]), target="")
        )
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_shutdown_rejects_new_intent(rpc) -> None:
    start, holder = rpc
    stub = start(FakeOps())
    holder["lt"].call(holder["engine"].shutdown(timeout_s=1))
    with pytest.raises(grpc.RpcError) as ei:
        stub.SubmitIntent(_req())
    assert ei.value.code() == grpc.StatusCode.UNAVAILABLE
