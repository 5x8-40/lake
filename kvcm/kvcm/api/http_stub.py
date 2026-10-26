"""IntentReceiver 占位实现：最小 HTTP 端点（E11 决策五，stdlib 零依赖）。

    POST /prefetch {"model_id": "qwen", "token_ids": [1,2,3], "target": "inst-B"}
        → 同步阻塞到 READY（200 {"status": "ready"}）或失败（502）
    GET  /healthz → 200

进程内入口 = `TaskEngine.submit`（脚本/测试直调，不过 HTTP）。占位通
道只调 TaskEngine 公开方法——E12 定型 gRPC 后替换本层，引擎不动；
替换后本端点可留作调试通道。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kvcm.flows.prefetch import PrefetchError
from kvcm.ops.types import InstanceId, Prefix
from kvcm.task_engine import TaskEngine


class PrefetchHttpServer:
    """POST /prefetch 的同步占位服务。"""

    def __init__(self, addr: tuple[str, int], engine: TaskEngine) -> None:
        engine_ = engine

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/healthz":
                    self._reply(200, {"status": "ok"})
                else:
                    self._reply(404, {"error": "unknown path"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/prefetch":
                    self._reply(404, {"error": "unknown path"})
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    prefix = Prefix(
                        model_id=body["model_id"],
                        token_ids=tuple(int(t) for t in body["token_ids"]),
                    )
                    target = InstanceId(body["target"])
                except (KeyError, TypeError, ValueError) as e:
                    self._reply(400, {"error": f"bad request: {e}"})
                    return
                try:
                    engine_.submit_sync(prefix, target)
                except PrefetchError as e:
                    self._reply(502, {"status": "failed", "error": str(e)})
                    return
                self._reply(200, {"status": "ready"})

            def _reply(self, code: int, obj: dict) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_a) -> None:  # 静默（print→logging 归生产化）
                pass

        self._server = ThreadingHTTPServer(addr, Handler)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="kvcm-http", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None
