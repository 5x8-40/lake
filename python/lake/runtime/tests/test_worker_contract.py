"""P6.3 review 回归(#55):空 exec_mode → COLOCATED **无复用**。

契约(与 proto 注释一致):hint 字段只在 Router 决策后随 exec_mode 一起有意义;
调用方未经 Router(空 exec_mode)时,worker 不得消费 wire 上的
computed_tokens/reused_blocks/local_hit——否则非 Router 调用方可以"伪造"复用。
"""

from types import SimpleNamespace
from unittest import mock

from lake.pb import lake_pb2
from lake.runtime.exec_mode import ExecMode
from lake.runtime.role import RoleConfig
from lake.runtime.worker import WorkerServicer


def _servicer() -> WorkerServicer:
    with mock.patch("lake.runtime.worker.PoolIface.from_grpc", return_value=mock.Mock()):
        return WorkerServicer(mock.Mock(), mock.Mock(), role=RoleConfig(), start_engine=False)


def _generate(s: WorkerServicer, **kw):
    captured = {}

    def fake_submit(req, hint=None):
        captured["req"] = req
        captured["hint"] = hint
        return SimpleNamespace(
            exec_mode=req.exec_mode,
            output_token_ids=[1, 2],
            reused_blocks=hint.reused_blocks if hint else 0,
            prefill_blocks=1,
        )

    s._engine.submit = fake_submit  # type: ignore[method-assign]
    request = lake_pb2.GenerateRequest(
        request_id="r1",
        model_id="m",
        prompt_tokens=[1, 2, 3, 4],
        max_new_tokens=2,
        **kw,
    )
    return s.Generate(request, mock.Mock()), captured


def test_empty_exec_mode_ignores_wire_hint() -> None:
    resp, captured = _generate(
        _servicer(),
        # exec_mode 缺省(空);wire 上却带 hint —— 必须被忽略
        computed_tokens=4,
        reused_blocks=1,
        local_hit=True,
    )
    hint = captured["hint"]
    assert hint.computed_tokens == 0
    assert hint.reused_blocks == 0
    assert hint.local_hit is False
    assert captured["req"].exec_mode == ExecMode.COLOCATED
    assert resp.reused_blocks == 0
    assert resp.mode == ExecMode.COLOCATED.value


def test_router_exec_mode_consumes_wire_hint() -> None:
    resp, captured = _generate(
        _servicer(),
        exec_mode=ExecMode.D_DIRECT.value,
        computed_tokens=4,
        reused_blocks=1,
        local_hit=True,
    )
    hint = captured["hint"]
    assert hint.computed_tokens == 4
    assert hint.reused_blocks == 1
    assert hint.local_hit is True
    assert captured["req"].exec_mode == ExecMode.D_DIRECT
    assert resp.reused_blocks == 1
    assert resp.mode == ExecMode.D_DIRECT.value
