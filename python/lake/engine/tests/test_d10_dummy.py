"""C9：D10 overlap×commit 会计 + D6 dummy_run。"""

from __future__ import annotations

import os

from lake.engine.agents.memory import InMemoryAgent
from lake.engine.model_runner import ModelLoadInfo, ModelRunner
from lake.engine.pool_iface import PoolIface
from lake.engine.pool_types import PreparePlan
from lake.runtime.req import Req
from lake.runtime.scheduler_output import ForwardMode, ReqIoSet, SamplingParams, SchedulerOutput


QWEN3_0_6B_MODEL_ID = os.path.expanduser(
    os.environ.get("LAKE_TEST_QWEN3_MODEL_PATH", "Qwen/Qwen3-0.6B")
)


def test_commit_does_not_undercut_newer_prepare() -> None:
    """overlap：process(N-1).commit 不得压掉 prepare(N) 已抬高的 HWM。"""
    ag = InMemoryAgent()
    # prepare step1：写到 8
    ag.prepare_step(
        PreparePlan(
            step_id=1,
            forward_mode=ForwardMode.DECODE,
            read_set=[],
            write_set=[ReqIoSet(req_id="r", token_start=7, token_end=8)],
            num_scheduled_tokens={"r": 1},
        )
    )
    ag.done(1)
    # prepare step2：写到 10（更新的预留）
    ag.prepare_step(
        PreparePlan(
            step_id=2,
            forward_mode=ForwardMode.DECODE,
            read_set=[],
            write_set=[ReqIoSet(req_id="r", token_start=9, token_end=10)],
            num_scheduled_tokens={"r": 1},
        )
    )
    ag.done(2)
    assert ag.l0_token_end["r"] == 10
    # 迟到的 step1 commit(8) 不得压到 8
    ag.commit_write_extent("r", 8)
    assert ag.l0_token_end["r"] == 10
    # 对应 step2 的 commit(10) 可对齐
    ag.commit_write_extent("r", 10)
    assert ag.l0_token_end["r"] == 10


def test_commit_shrinks_when_no_newer_prepare() -> None:
    ag = InMemoryAgent()
    ag.l0_token_end["r1"] = 20
    ag._prepares_since_commit["r1"] = 1  # noqa: SLF001 — 模拟单次 prepare 后
    ag.commit_write_extent("r1", 12)
    assert ag.l0_token_end["r1"] == 12


def test_dummy_run_skips_pool_done() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    runner = ModelRunner(pool, model_backend="mock")
    runner.load_model()
    out = runner.dummy_run(num_reqs=2, tokens_per_req=3, step_id=42)
    assert out.step_id == 42
    assert ag.done_calls == 0
    assert ag.prepare_calls == 0
    assert runner._input_batch.req_ids == ["dummy-0", "dummy-1"]  # noqa: SLF001
    assert runner._attn_meta is not None  # noqa: SLF001
    assert runner._attn_meta.num_actual_tokens == 2  # noqa: SLF001
    assert out.next_token_ids == {"dummy-0": [0], "dummy-1": [0]}


def test_extend_process_consumes_prepare_commit_guard() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    runner = ModelRunner(pool, model_backend="mock")
    from lake.runtime.node_scheduler import NodeScheduler, build_req_from_generate
    from lake.runtime.role import RoleConfig

    sched = NodeScheduler(
        pool,
        runner,
        RoleConfig(model_backend="mock", enable_overlap=False, max_num_scheduled_tokens=8),
    )
    sched.add_request(build_req_from_generate("r", "m", list(range(8)), 2, "n0"))

    out = sched.schedule()
    assert out.req_forward_modes["r"] == ForwardMode.EXTEND
    sched._run_batch(out)  # noqa: SLF001
    assert ag._prepares_since_commit["r"] == 1  # noqa: SLF001
    sched._pop_and_process()  # noqa: SLF001
    assert ag._prepares_since_commit["r"] == 0  # noqa: SLF001


def test_dummy_run_requires_loaded_model() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    runner = ModelRunner(pool, model_backend="mock")
    try:
        runner.dummy_run(num_reqs=1, tokens_per_req=1)
        raise AssertionError("expected unloaded model")
    except ValueError as e:
        assert "model must be loaded" in str(e)


def test_execute_model_does_not_done_failed_step() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    runner = ModelRunner(pool, model_backend="mock")
    req = Req(
        req_id="bad-ready",
        served_model_name="model",
        prompt_token_ids=[1],
        sampling_params=SamplingParams(max_new_tokens=1),
    )
    output = SchedulerOutput(
        step_id=7,
        forward_mode=ForwardMode.EXTEND,
        num_scheduled_tokens={req.req_id: 1},
        total_num_scheduled_tokens=1,
        write_set=[ReqIoSet(req_id=req.req_id, token_start=0, token_end=1)],
        req_forward_modes={req.req_id: ForwardMode.EXTEND},
        req_num_computed_at_schedule={req.req_id: 0},
    )
    ready = pool.prepare_step(output, {req.req_id: req})
    ready.slot_mapping_by_req[req.req_id] = []

    try:
        runner.execute_model(output, ready, {req.req_id: req})
        raise AssertionError("expected execute_model to fail")
    except ValueError as e:
        assert "slot_mapping" in str(e)
    assert ag.done_calls == 0
    assert ag._ready_step == 7  # noqa: SLF001


def test_load_qwen3_model_pins_weights_and_warmup_skips_pool() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    pins: list[ModelLoadInfo] = []
    runner = ModelRunner(
        pool,
        model_backend="qwen3",
        weight_pin_callback=pins.append,
    )
    info = runner.load_model(model_path=QWEN3_0_6B_MODEL_ID)
    assert info.model_path == QWEN3_0_6B_MODEL_ID
    assert info.served_model_name == "model"
    assert info.revision == ""
    assert info.backend == "qwen3"
    assert info.load_dummy_weights is True
    assert info.weight_pinned is True
    assert pins == [info]
    assert runner.model_loaded is True
    assert runner.model_warmed is False
    assert runner._model is not None  # noqa: SLF001
    assert runner._model.config.num_hidden_layers == 28  # noqa: SLF001
    assert runner._model.config.num_key_value_heads == 8  # noqa: SLF001

    out = runner.warmup(num_reqs=2, tokens_per_req=1)
    assert out.step_id == -1
    assert runner.model_warmed is True
    assert ag.done_calls == 0
    assert ag.prepare_calls == 0
    assert runner.status().model_path == QWEN3_0_6B_MODEL_ID
    assert runner.status().served_model_name == "model"
