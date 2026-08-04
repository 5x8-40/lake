"""C5：三模式选路 + vLLM 几何调度（整段本地命中 → computed=prompt_len → 直接生成）。"""

from __future__ import annotations

from lake.engine.agents.memory import InMemoryAgent
from lake.engine.model_runner import ModelRunner
from lake.engine.pool_iface import PoolIface
from lake.runtime.exec_mode import ExecMode
from lake.runtime.mode_select import full_local_hit, select_exec_mode
from lake.runtime.node_scheduler import NodeScheduler, build_req_from_generate
from lake.runtime.prefix_hint import PrefixHint
from lake.runtime.req import Req
from lake.runtime.role import RoleConfig, WorkerRole
from lake.runtime.scheduler_output import ForwardMode, ReqIoSet, SamplingParams, SchedulerOutput


def test_mode_select_d_direct() -> None:
    h = PrefixHint(computed_tokens=16, local_hit=True)
    assert select_exec_mode(h, prompt_len=16) == ExecMode.D_DIRECT
    assert full_local_hit(h, 16)


def test_mode_select_partial_hit_is_d_direct() -> None:
    """部分本地命中 → D_DIRECT；整段才 full_local_hit。"""
    h = PrefixHint(computed_tokens=8, local_hit=True)
    assert select_exec_mode(h, prompt_len=16) == ExecMode.D_DIRECT
    assert not full_local_hit(h, 16)
    h2 = PrefixHint(computed_tokens=8, local_hit=False)
    assert select_exec_mode(h2, prompt_len=16) == ExecMode.COLOCATED


def test_mode_select_pd_role() -> None:
    h = PrefixHint()
    assert select_exec_mode(h, prompt_len=8, role=WorkerRole.DECODE) == ExecMode.PD_DISAGG
    assert select_exec_mode(h, prompt_len=8, role=WorkerRole.HYBRID) == ExecMode.COLOCATED


def test_full_local_hit_skips_prompt_phase() -> None:
    """整段 L0 命中：无 prompt 残差步，直接 DECODE（无 PREBUILT 分相）。"""
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    role = RoleConfig(model_backend="mock", enable_overlap=False)
    runner = ModelRunner(pool, model_backend="mock")
    sched = NodeScheduler(pool, runner, role)

    prompt = list(range(8))
    ag.seed_local_prefix("d1", len(prompt))
    hint = PrefixHint(
        computed_tokens=len(prompt),
        reused_blocks=1,
        local_hit=True,
        prebuilt=True,
    )
    req = build_req_from_generate("d1", "m", prompt, 2, "n0")
    sched.add_request(req, hint=hint)
    assert req.exec_mode == ExecMode.D_DIRECT
    assert req.num_computed_tokens == len(prompt)

    modes = []
    while sched._waiting or sched._running or sched._result_queue:  # noqa: SLF001
        out = sched.schedule()
        if out.total_num_scheduled_tokens == 0:
            sched._drain_results()  # noqa: SLF001
            if not sched._waiting and not sched._running:  # noqa: SLF001
                break
            continue
        modes.append(out.forward_mode)
        sched._run_batch(out)  # noqa: SLF001
        sched._pop_and_process()  # noqa: SLF001

    assert ForwardMode.EXTEND not in modes
    assert ForwardMode.PREBUILT not in modes
    assert ForwardMode.DECODE in modes
    done = sched.get_req("d1")
    assert done.finished
    assert done.exec_mode == ExecMode.D_DIRECT
    assert done.num_output_tokens == 2


def test_partial_hit_prompt_residual_has_read_set() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    role = RoleConfig(model_backend="mock", enable_overlap=False)
    runner = ModelRunner(pool, model_backend="mock")
    sched = NodeScheduler(pool, runner, role)
    prompt = list(range(16))
    ag.seed_local_prefix("p1", 8)
    hint = pool.probe_prefix(build_req_from_generate("p1", "m", prompt, 1, "n0"))
    assert hint.computed_tokens == 8
    assert hint.local_hit is True
    req = build_req_from_generate("p1", "m", prompt, 1, "n0")
    sched.add_request(req, hint=hint)
    assert req.exec_mode == ExecMode.D_DIRECT
    out = sched.schedule()
    assert out.forward_mode == ForwardMode.EXTEND  # 派生标签：仍在算 prompt
    assert any(io.token_end == 8 for io in out.read_set)
    assert any(io.token_start == 8 for io in out.write_set)


def test_forward_exception_does_not_call_done() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    runner = ModelRunner(pool, model_backend="qwen3")
    runner._forward_model = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: SLF001
    req = Req(
        req_id="r1",
        served_model_name="model",
        prompt_token_ids=[1, 2, 3, 4],
        sampling_params=SamplingParams(max_new_tokens=1),
        node_id="n",
    )
    out = SchedulerOutput(
        step_id=1,
        forward_mode=ForwardMode.EXTEND,
        num_scheduled_tokens={"r1": 4},
        total_num_scheduled_tokens=4,
        write_set=[ReqIoSet(req_id="r1", token_start=0, token_end=4)],
        req_forward_modes={"r1": ForwardMode.EXTEND},
        req_num_computed_at_schedule={"r1": 0},
    )
    ready = pool.prepare_step(out, {"r1": req})
    try:
        runner.execute_model(out, ready, {"r1": req})
        raise AssertionError("expected boom")
    except RuntimeError as e:
        assert "boom" in str(e)
    assert ag.done_calls == 0
    assert ag._ready_step == 1  # noqa: SLF001
    out2 = SchedulerOutput(
        step_id=2,
        forward_mode=ForwardMode.EXTEND,
        num_scheduled_tokens={"r1": 4},
        total_num_scheduled_tokens=4,
        write_set=[ReqIoSet(req_id="r1", token_start=0, token_end=4)],
        req_num_computed_at_schedule={"r1": 0},
    )
    try:
        pool.prepare_step(out2, {"r1": req})
        raise AssertionError("expected prepare to wait for abort/failure cleanup")
    except Exception as e:
        assert "prepare while ready=1" in str(e)


if __name__ == "__main__":
    test_mode_select_d_direct()
    test_mode_select_partial_hit_is_d_direct()
    test_mode_select_pd_role()
    test_full_local_hit_skips_prompt_phase()
    test_partial_hit_prompt_residual_has_read_set()
    test_forward_exception_still_calls_done()
    print("test_prebuilt OK")
