"""Sampling hooks independent of model backend."""

from __future__ import annotations

from lake.engine.agents.memory import InMemoryAgent
from lake.engine.model_runner import ModelRunner
from lake.engine.pool_iface import PoolIface
from lake.engine.sample.grammar import apply_token_bitmask
from lake.engine.sample.greedy import greedy_sample
from lake.runtime.req import Req
from lake.runtime.scheduler_output import ForwardMode, GrammarOutput, SamplingParams, SchedulerOutput


def test_greedy_sample() -> None:
    assert greedy_sample([0.1, 0.9, 0.2]) == 1


def test_apply_token_bitmask() -> None:
    masked = apply_token_bitmask([0.1, 0.9, 0.2], [True, False, True])
    assert greedy_sample(masked) == 2


def test_sample_tokens_uses_grammar_bitmask() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    runner = ModelRunner(pool, model_backend="mock")
    req = Req(
        req_id="g1",
        served_model_name="model",
        prompt_token_ids=[0],
        sampling_params=SamplingParams(max_new_tokens=1, structured_output="json"),
    )
    output = SchedulerOutput(
        step_id=1,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"g1": 1},
        total_num_scheduled_tokens=1,
        grammar_output=GrammarOutput(
            req_ids=["g1"],
            token_bitmask_by_req={"g1": [True, False, True]},
        ),
        has_structured_output=True,
    )
    sampled, _ = runner.sample_tokens(output, {"g1": req}, {"g1": [0.1, 0.9, 0.2]})
    assert sampled == {"g1": [2]}


def test_sample_tokens_can_defer_structured_output() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    runner = ModelRunner(pool, model_backend="mock")
    req = Req(
        req_id="g2",
        served_model_name="model",
        prompt_token_ids=[0],
        sampling_params=SamplingParams(max_new_tokens=1, structured_output="json"),
    )
    output = SchedulerOutput(
        step_id=2,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"g2": 1},
        total_num_scheduled_tokens=1,
        grammar_output=GrammarOutput(
            req_ids=["g2"],
            deferred_req_ids=["g2"],
            reason="waiting for prior token",
        ),
        has_structured_output=True,
    )
    sampled, _ = runner.sample_tokens(output, {"g2": req}, {"g2": [0.1, 0.9, 0.2]})
    assert sampled == {}
