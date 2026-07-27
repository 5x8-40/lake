"""C9：D10 overlap×commit 会计 + D6 dummy_run。"""

from __future__ import annotations

from engine.agents.memory import InMemoryAgent
from engine.model_runner import ModelRunner
from engine.pool_iface import PoolIface
from engine.pool_types import PreparePlan
from runtime.scheduler_output import ForwardMode, ReqIoSet


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
    out = runner.dummy_run(num_reqs=2, tokens_per_req=1, step_id=42)
    assert out.step_id == 42
    assert ag.done_calls == 0
    assert ag.prepare_calls == 0
