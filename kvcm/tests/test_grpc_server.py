"""E12 gRPC 通道检查：真 server + 真 TaskEngine（fake KvOps），走完整
RPC 往返——幂等去重、状态回执、参数校验、关闭语义。离线可跑。"""

from __future__ import annotations

import threading
import time

import grpc
import pytest

from kvcm.api.grpc_server import IntentGrpcServer
from kvcm.api.pb import kvcm_intent_pb2 as pb
from kvcm.api.pb import kvcm_intent_pb2_grpc as pb_grpc
from kvcm.ops.base import TaskStatus
from kvcm.task_engine import IntentStatus, TaskEngine, intent_id_of
from kvcm.ops.types import InstanceId, Prefix

from test_task_engine import FakeOps  # 同目录测试模块复用

_P = Prefix(model_id="qwen", token_ids=(1, 2, 3))
_T = InstanceId("inst-B")


@pytest.fixture()
def rpc():
    """起真 gRPC server（ephemeral 端口）+ stub；yield (stub, ops, engine)。"""
    holder = {}

    def _start(ops: FakeOps):
        engine = TaskEngine(ops)
        server = IntentGrpcServer(("127.0.0.1", 0), engine)
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{server.port}")
        grpc.channel_ready_future(channel).result(timeout=5)
        holder.update(engine=engine, server=server, channel=channel, ops=ops)
        return pb_grpc.KvcmIntentStub(channel)

    yield _start, holder
    holder["channel"].close()
    holder["server"].stop()
    holder["engine"].shutdown()


def _req(prefix: Prefix = _P, target: str = "inst-B") -> pb.SubmitIntentRequest:
    return pb.SubmitIntentRequest(
        prefix=pb.Prefix(model_id=prefix.model_id, token_ids=prefix.token_ids),
        target=target,
    )


def test_submit_then_get_task_ready(rpc) -> None:
    start, holder = rpc
    stub = start(FakeOps(seq=[TaskStatus.RUNNING, TaskStatus.READY]))
    resp = stub.SubmitIntent(_req())
    assert resp.intent_id == intent_id_of(_P, _T)
    status = pb.GetTaskResponse.PENDING
    deadline = time.monotonic() + 5
    while status == pb.GetTaskResponse.PENDING and time.monotonic() < deadline:
        status = stub.GetTask(pb.GetTaskRequest(intent_id=resp.intent_id)).status
        time.sleep(0.02)
    assert status == pb.GetTaskResponse.READY


def test_dedup_in_flight_same_intent_id(rpc) -> None:
    start, holder = rpc
    gate = threading.Event()
    ops = FakeOps(seq=[TaskStatus.READY] * 4, query_gate=gate)
    stub = start(ops)
    r1 = stub.SubmitIntent(_req())
    r2 = stub.SubmitIntent(_req())  # 在途重复 → 去重
    assert r1.intent_id == r2.intent_id
    gate.set()
    deadline = time.monotonic() + 5
    while holder["engine"].intent_status(r1.intent_id) is not IntentStatus.READY:
        if time.monotonic() > deadline:
            raise AssertionError("未就绪")
        time.sleep(0.02)
    assert holder["ops"].copy_count == 1  # 只执行一次


def test_failed_status_and_detail(rpc) -> None:
    start, _ = rpc
    stub = start(FakeOps(seq=[TaskStatus.FAILED]))
    resp = stub.SubmitIntent(_req())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        got = stub.GetTask(pb.GetTaskRequest(intent_id=resp.intent_id))
        if got.status != pb.GetTaskResponse.PENDING:
            assert got.status == pb.GetTaskResponse.FAILED
            return
        time.sleep(0.02)
    raise AssertionError("未到 FAILED")


def test_unknown_intent_id(rpc) -> None:
    start, _ = rpc
    stub = start(FakeOps())
    got = stub.GetTask(pb.GetTaskRequest(intent_id="no-such-intent"))
    assert got.status == pb.GetTaskResponse.STATUS_UNKNOWN


def test_invalid_request_rejected(rpc) -> None:
    start, _ = rpc
    stub = start(FakeOps())
    with pytest.raises(grpc.RpcError) as ei:
        stub.SubmitIntent(_req(target=""))  # 缺 target
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_submit_after_engine_shutdown_unavailable(rpc) -> None:
    start, holder = rpc
    stub = start(FakeOps())
    holder["engine"].shutdown()
    with pytest.raises(grpc.RpcError) as ei:
        stub.SubmitIntent(_req())
    assert ei.value.code() == grpc.StatusCode.UNAVAILABLE
