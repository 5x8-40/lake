"""WorkerService gRPC 门面：Dispatch 后的 Generate 挂到 WorkerEngine（C6）。

P3 仍：Router → AgentService.Dispatch(ack) → WorkerService.Generate。
生产路径：Router Dispatch → agent 组 batch → FFI 引擎；本门面可收窄为控制面 RPC。

参考:vLLM EngineCore 单环；KVConnectorBase_V1 → lake `pool_iface`。
内核见 `runtime/worker_engine.py`（无 gRPC 依赖）。
"""

from __future__ import annotations

import logging
from concurrent import futures
from typing import Optional

import grpc

from engine.model_runner import ModelRunner
from engine.pool_iface import PoolIface, chain_block_hashes, mock_kv_bytes
from engine.pool_types import PoolError, PoolErrorCode
from lake_pb import lake_pb2, lake_pb2_grpc
from runtime.exec_mode import ExecMode
from runtime.mode_select import select_exec_mode
from runtime.node_scheduler import build_req_from_generate
from runtime.role import RoleConfig
from runtime.worker_engine import WorkerEngine

LOG = logging.getLogger("lake.worker")

NODE_ID = "worker-0"

# 兼容 scripts/verify-p3.sh 等旧 import
__all__ = [
    "WorkerEngine",
    "WorkerServicer",
    "serve",
    "chain_block_hashes",
    "mock_kv_bytes",
    "NODE_ID",
]

_POOL_ERROR_STATUS = {
    PoolErrorCode.TIMEOUT: grpc.StatusCode.UNAVAILABLE,  # 触发 F4 重路由
    PoolErrorCode.CAPACITY: grpc.StatusCode.RESOURCE_EXHAUSTED,
    PoolErrorCode.DOWNSTREAM: grpc.StatusCode.UNAVAILABLE,
    PoolErrorCode.PROTOCOL_ERROR: grpc.StatusCode.UNAVAILABLE,  # fence 乱 → F4
    PoolErrorCode.INVALID_ARG: grpc.StatusCode.INVALID_ARGUMENT,
}


def _abort_rpc(context: grpc.ServicerContext, exc: grpc.RpcError) -> None:
    code = exc.code() if hasattr(exc, "code") else grpc.StatusCode.INTERNAL
    details = exc.details() if hasattr(exc, "details") else str(exc)
    if code == grpc.StatusCode.UNAVAILABLE:
        context.abort(grpc.StatusCode.UNAVAILABLE, f"downstream: {details}")
    context.abort(grpc.StatusCode.INTERNAL, f"downstream: {details}")


def _abort_pool_error(context: grpc.ServicerContext, exc: PoolError) -> None:
    status = _POOL_ERROR_STATUS.get(exc.code, grpc.StatusCode.INTERNAL)
    context.abort(status, str(exc))


class WorkerServicer(lake_pb2_grpc.WorkerServiceServicer):
    def __init__(
        self,
        cp: lake_pb2_grpc.ControlPlaneServiceStub,
        kv: lake_pb2_grpc.TcpDataServiceStub,
        role: Optional[RoleConfig] = None,
        *,
        start_engine: bool = True,
    ):
        self._role = role or RoleConfig.from_env()
        self._pool = PoolIface.from_grpc(
            cp,
            kv,
            pull_budget_ms=self._role.pull_budget_ms,
            allow_partial_hit=self._role.allow_partial_hit,
        )
        self._runner = ModelRunner(
            self._pool,
            model_backend=self._role.model_backend,
            enable_drafter=self._role.enable_drafter,
            num_draft_tokens=self._role.num_draft_tokens,
        )
        self._engine = WorkerEngine(self._pool, self._runner, self._role)
        if start_engine:
            self._engine.start()

    @property
    def engine(self) -> WorkerEngine:
        return self._engine

    def Generate(self, request: lake_pb2.GenerateRequest, context: grpc.ServicerContext) -> lake_pb2.GenerateResponse:
        node = request.requester_node_id or NODE_ID
        req = build_req_from_generate(
            request_id=request.request_id,
            model_id=request.model_id or "mock-llm",
            prompt_tokens=list(request.prompt_tokens),
            max_new_tokens=request.max_new_tokens or 4,
            node_id=node,
        )
        try:
            hint = self._pool.probe_prefix(req)
            req.exec_mode = select_exec_mode(
                hint, prompt_len=len(req.prompt_token_ids), role=self._role.role
            )
            done = self._engine.submit(req, hint=hint)
        except PoolError as e:
            _abort_pool_error(context, e)
        except grpc.RpcError as e:
            _abort_rpc(context, e)
        except ValueError as e:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        except RuntimeError as e:
            context.abort(grpc.StatusCode.INTERNAL, str(e))
        except Exception as e:  # noqa: BLE001
            context.abort(grpc.StatusCode.INTERNAL, str(e))

        mode = done.exec_mode.value if isinstance(done.exec_mode, ExecMode) else str(done.exec_mode)
        return lake_pb2.GenerateResponse(
            request_id=request.request_id,
            output_tokens=done.output_token_ids,
            reused_blocks=done.reused_blocks,
            prefill_blocks=done.prefill_blocks,
            mode=mode,
        )


def serve(bind: str, cp_addr: str, kv_addr: str) -> None:
    cp_chan = grpc.insecure_channel(cp_addr)
    kv_chan = grpc.insecure_channel(kv_addr)
    cp = lake_pb2_grpc.ControlPlaneServiceStub(cp_chan)
    kv = lake_pb2_grpc.TcpDataServiceStub(kv_chan)

    role = RoleConfig.from_env()
    servicer = WorkerServicer(cp, kv, role=role)
    # gRPC 线程池可多路并发 Generate；实际组批在 WorkerEngine 单环
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    lake_pb2_grpc.add_WorkerServiceServicer_to_server(servicer, server)
    server.add_insecure_port(bind)
    server.start()
    LOG.info(
        "WorkerService on %s (cp=%s kv=%s) role=%s backend=%s via WorkerEngine",
        bind,
        cp_addr,
        kv_addr,
        role.role.value,
        role.model_backend,
    )
    try:
        server.wait_for_termination()
    finally:
        servicer.engine.stop()
