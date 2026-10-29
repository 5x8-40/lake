"""IntentReceiver 的 gRPC 实现（E12）：策略侧埋钩子调用的下行通道。

协议与语义见 `proto/kvcm_intent.proto` 头注释——**intent ≠ command**：
kvcm 按池现状决策执行（可推迟/去重/拒绝），回执走 GetTask 查询，
本服务不在请求放行链路上（D1 §4）。

servicer 只调 `TaskEngine` 公开方法（submit_intent / intent_status），
不含任何存储概念；引擎不动即可替换/并存其他入口（脚本直调、调试
通道）。stub 入仓（`kvcm/api/pb/`，`scripts/gen_stubs.sh` 重生成）。
"""

from __future__ import annotations

from concurrent import futures

import grpc

from kvcm.api.pb import kvcm_intent_pb2 as pb
from kvcm.api.pb import kvcm_intent_pb2_grpc as pb_grpc
from kvcm.ops.types import InstanceId, Prefix
from kvcm.task_engine import IntentStatus, TaskEngine

_STATUS_MAP = {
    IntentStatus.PENDING: pb.GetTaskResponse.PENDING,
    IntentStatus.READY: pb.GetTaskResponse.READY,
    IntentStatus.FAILED: pb.GetTaskResponse.FAILED,
}


class KvcmIntentServicer(pb_grpc.KvcmIntentServicer):
    def __init__(self, engine: TaskEngine) -> None:
        self._engine = engine

    def SubmitIntent(self, request, context):
        prefix = Prefix(
            model_id=request.prefix.model_id,
            token_ids=tuple(request.prefix.token_ids),
        )
        if not prefix.model_id or not prefix.token_ids or not request.target:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "prefix/target 不完整")
        try:
            intent_id = self._engine.submit_intent(prefix, InstanceId(request.target))
        except RuntimeError as e:  # 关闭中
            context.abort(grpc.StatusCode.UNAVAILABLE, str(e))
        return pb.SubmitIntentResponse(intent_id=intent_id)

    def GetTask(self, request, context):
        status = self._engine.intent_status(request.intent_id)
        if status is None:
            return pb.GetTaskResponse(status=pb.GetTaskResponse.STATUS_UNKNOWN)
        return pb.GetTaskResponse(
            status=_STATUS_MAP[status],
            detail=self._engine.intent_detail(request.intent_id),
        )


class IntentGrpcServer:
    """gRPC 服务包装：start/stop 生命周期归进程壳。"""

    def __init__(self, addr: tuple[str, int], engine: TaskEngine) -> None:
        # 池由本类持有并负责关闭：`grpc.server()` 收到外部池只借用，
        # `stop()` 不会关它。池线程是非 daemon，`stop()` 之后仍会存活
        # （实测 4 个 `grpc-worker_*`），占着直到进程退出。
        self._pool = futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="kvcm-grpc"
        )
        self._server = grpc.server(self._pool)
        pb_grpc.add_KvcmIntentServicer_to_server(KvcmIntentServicer(engine), self._server)
        self._port = self._server.add_insecure_port(f"{addr[0]}:{addr[1]}")
        if self._port == 0:
            self._pool.shutdown(wait=False)
            raise RuntimeError(f"gRPC 端口绑定失败：{addr[0]}:{addr[1]}")

    @property
    def address(self) -> tuple[str, int]:
        return ("", self._port)

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        self._server.start()

    def stop(self, *, grace_s: float = 2.0) -> None:
        """停收新请求（grace 内让在途 RPC 收尾；进程退出序列的第一棒，
        在途任务收口归 engine.shutdown，不在本层等）。"""
        self._server.stop(grace_s)
        self._server.wait_for_termination(timeout=grace_s + 1)
        # 池是本类建的，必须自己关：grpc 只借用外部传入的 executor，
        # `stop()` 不碰它。不关的话这几个非 daemon 线程会活到进程退出。
        self._pool.shutdown(wait=False)
