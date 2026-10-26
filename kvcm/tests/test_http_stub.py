"""IntentReceiver 占位（HTTP）端点检查：回执语义与请求校验（离线）。"""

from __future__ import annotations

import http.client
import json

from kvcm.api.http_stub import PrefetchHttpServer
from kvcm.flows.prefetch import PrefetchError
from kvcm.ops.types import InstanceId, Prefix


class FakeEngine:
    """记录 submit_sync 调用并按脚本演结果。"""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[tuple[Prefix, InstanceId]] = []

    def submit_sync(self, prefix, target, **kw):
        self.calls.append((prefix, target))
        if self._error is not None:
            raise self._error


def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _serve(engine: FakeEngine):
    server = PrefetchHttpServer(("127.0.0.1", 0), engine)
    server.start()
    return server, server.address[1]


def test_prefetch_ready_returns_200() -> None:
    engine = FakeEngine()
    server, port = _serve(engine)
    try:
        status, data = _post(port, "/prefetch", {"model_id": "qwen", "token_ids": [1, 2, 3], "target": "inst-B"})
        assert status == 200 and data["status"] == "ready"
        (prefix, target), = engine.calls
        assert prefix == Prefix(model_id="qwen", token_ids=(1, 2, 3))
        assert target == InstanceId("inst-B")
    finally:
        server.stop()


def test_prefetch_failure_returns_502() -> None:
    engine = FakeEngine(error=PrefetchError("副本缺失"))
    server, port = _serve(engine)
    try:
        status, data = _post(port, "/prefetch", {"model_id": "qwen", "token_ids": [1], "target": "inst-B"})
        assert status == 502 and data["status"] == "failed"
    finally:
        server.stop()


def test_bad_request_returns_400() -> None:
    engine = FakeEngine()
    server, port = _serve(engine)
    try:
        status, _ = _post(port, "/prefetch", {"model_id": "qwen"})  # 缺字段
        assert status == 400
        assert engine.calls == []
    finally:
        server.stop()


def test_healthz_and_unknown_path() -> None:
    server, port = _serve(FakeEngine())
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        status, _ = _post(port, "/nope", {})
        assert status == 404
        conn.close()
    finally:
        server.stop()
