"""C8：InputBatch + AttentionMetadata + TinyLM 残差 / 同批两请求。"""

from __future__ import annotations

from engine.agents.memory import InMemoryAgent
from engine.attn.metadata import build_attn_metadata
from engine.model_runner import ModelRunner
from engine.models.tiny_lm import TinyLM
from engine.pool_iface import PoolIface
from engine.pool_types import ReadyHandle
from kernels.attn_ref import causal_attn_queries
from runtime.node_scheduler import NodeScheduler, build_req_from_generate
from runtime.role import RoleConfig
from runtime.scheduler_output import ForwardMode, SchedulerOutput


def test_causal_attn_queries_matches_full_slice() -> None:
    q = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    k = list(q)
    v = list(q)
    full = __import__("kernels.attn_ref", fromlist=["causal_attn"]).causal_attn(q, k, v)
    partial = causal_attn_queries(q[1:], k, v, q_pos_start=1)
    assert len(partial) == 2
    for a, b in zip(partial, full[1:]):
        assert all(abs(x - y) < 1e-9 for x, y in zip(a, b))


def test_tiny_residual_matches_full_last() -> None:
    m = TinyLM(vocab_size=64, d_model=16, n_heads=4, seed=3, attn_backend="ref")
    toks = [1, 2, 3, 4, 5]
    full = m.forward_logits(toks)
    rows = m.forward_query_logits(toks, 4, 5)
    assert len(rows) == 1
    assert all(abs(a - b) < 1e-9 for a, b in zip(full, rows[0]))


def test_prepare_inputs_attn_metadata() -> None:
    pool = PoolIface(InMemoryAgent())
    runner = ModelRunner(pool, model_backend="tiny_lm", tiny_lm=TinyLM(attn_backend="ref"))
    req = build_req_from_generate("r", "m", list(range(8)), 2, "n0")
    out = SchedulerOutput(
        step_id=1,
        forward_mode=ForwardMode.EXTEND,
        num_scheduled_tokens={"r": 3},
        total_num_scheduled_tokens=3,
        req_num_computed_at_schedule={"r": 2},
        req_forward_modes={"r": ForwardMode.EXTEND},
    )
    batch = runner.prepare_inputs(out, {"r": req})
    assert batch.query_start["r"] == 2 and batch.query_end["r"] == 5
    assert batch.is_prompt_phase["r"] is True
    ready = ReadyHandle(step_id=1, block_table_by_req={"r": [0, 1]})
    meta = runner.prepare_attn(batch, ready)
    assert meta.block_tables["r"] == [0, 1]
    assert meta.max_query_len == 3
    assert meta.query_start_loc == [0, 3]


def test_two_reqs_same_batch_tiny() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    role = RoleConfig(
        model_backend="tiny_lm",
        enable_overlap=False,
        max_running_reqs=4,
        max_num_scheduled_tokens=64,
    )
    runner = ModelRunner(
        pool, model_backend="tiny_lm", tiny_lm=TinyLM(vocab_size=128, d_model=16, n_heads=4, attn_backend="ref")
    )
    sched = NodeScheduler(pool, runner, role)
    sched.add_request(build_req_from_generate("a", "tiny", list(range(6)), 2, "n0"))
    sched.add_request(build_req_from_generate("b", "tiny", list(range(6, 12)), 2, "n0"))
    out = sched.schedule()
    assert len(out.num_scheduled_tokens) == 2
    assert out.forward_mode == ForwardMode.EXTEND
    sched._run_batch(out)  # noqa: SLF001
    sched._pop_and_process()  # noqa: SLF001
    sched.run_until_idle()
    assert sched.get_req("a").finished and sched.get_req("b").finished
    assert set(ag.finished) == {"a", "b"}


def test_build_attn_metadata_loc() -> None:
    meta = build_attn_metadata(
        seq_lens={"a": 5, "b": 3},
        query_start={"a": 2, "b": 2},
        query_end={"a": 5, "b": 3},
        req_order=["a", "b"],
    )
    assert meta.query_start_loc == [0, 3, 4]
    assert meta.max_query_len == 3
