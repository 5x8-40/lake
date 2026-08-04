"""C8：InputBatch + AttentionMetadata + 同批两请求。"""

from __future__ import annotations

from lake.engine.agents.memory import InMemoryAgent
from lake.engine.model_executor.layers.attentions import build_attn_metadata
from lake.engine.input_batch import InputBatch, InputBuffers
from lake.engine.model_runner import ModelRunner
from lake.engine.pool_iface import PoolIface
from lake.engine.pool_types import PreparePlan, ReadyHandle
from lake.kernels.attn_ref import causal_attn_queries
from lake.runtime.node_scheduler import NodeScheduler, build_req_from_generate
from lake.runtime.role import RoleConfig
from lake.runtime.scheduler_output import ForwardMode, ReqIoSet, SchedulerOutput


def test_causal_attn_queries_matches_full_slice() -> None:
    q = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    k = list(q)
    v = list(q)
    full = __import__("lake.kernels.attn_ref", fromlist=["causal_attn"]).causal_attn(q, k, v)
    partial = causal_attn_queries(q[1:], k, v, q_pos_start=1)
    assert len(partial) == 2
    for a, b in zip(partial, full[1:]):
        assert all(abs(x - y) < 1e-9 for x, y in zip(a, b))


def test_prepare_inputs_attn_metadata() -> None:
    pool = PoolIface(InMemoryAgent())
    runner = ModelRunner(pool, model_backend="mock")
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
    assert meta.positions == [2, 3, 4]
    assert meta.slot_mapping == [2, 3, 4]
    assert meta.block_table_tensor == [[0, 1]]


def test_decode_slot_mapping_matches_query_position() -> None:
    pool = PoolIface(InMemoryAgent())
    runner = ModelRunner(pool, model_backend="mock")
    req = build_req_from_generate("r", "m", list(range(4)), 1, "n0")
    req.num_computed_tokens = len(req.prompt_token_ids)
    out = SchedulerOutput(
        step_id=2,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"r": 1},
        total_num_scheduled_tokens=1,
        req_num_computed_at_schedule={"r": len(req.prompt_token_ids)},
        req_forward_modes={"r": ForwardMode.DECODE},
    )
    batch = runner.prepare_inputs(out, {"r": req})
    assert batch.query_start["r"] == 3
    assert batch.query_end["r"] == 4

    ready = ReadyHandle(
        step_id=2,
        block_table_by_req={"r": [0]},
        slot_mapping_by_req={"r": [3]},
    )
    meta = runner.prepare_attn(batch, ready)
    assert meta.positions == [3]
    assert meta.slot_mapping == [3]


def test_prepare_inputs_uses_scheduler_query_geometry_under_overlap() -> None:
    pool = PoolIface(InMemoryAgent())
    runner = ModelRunner(pool, model_backend="mock")
    req = build_req_from_generate("r", "m", list(range(4)), 2, "n0")
    req.num_computed_tokens = len(req.prompt_token_ids)
    out = SchedulerOutput(
        step_id=3,
        forward_mode=ForwardMode.DECODE,
        num_scheduled_tokens={"r": 1},
        total_num_scheduled_tokens=1,
        req_num_computed_at_schedule={"r": len(req.prompt_token_ids)},
        req_query_start={"r": 4},
        req_query_end={"r": 5},
        req_forward_modes={"r": ForwardMode.DECODE},
    )

    batch = runner.prepare_inputs(out, {"r": req})
    assert batch.query_start["r"] == 4
    assert batch.query_end["r"] == 5
    assert len(batch.token_ids["r"]) == 5

    ready = ReadyHandle(
        step_id=3,
        block_table_by_req={"r": [0]},
        slot_mapping_by_req={"r": [4]},
    )
    meta = runner.prepare_attn(batch, ready)
    assert meta.positions == [4]
    assert meta.slot_mapping == [4]


def test_two_reqs_same_batch_mock() -> None:
    ag = InMemoryAgent()
    pool = PoolIface(ag)
    role = RoleConfig(
        model_backend="mock",
        enable_overlap=False,
        max_running_reqs=4,
        max_num_scheduled_tokens=64,
    )
    runner = ModelRunner(pool, model_backend="mock")
    sched = NodeScheduler(pool, runner, role)
    sched.add_request(build_req_from_generate("a", "mock", list(range(6)), 2, "n0"))
    sched.add_request(build_req_from_generate("b", "mock", list(range(6, 12)), 2, "n0"))
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


def test_input_buffers_materialize_ragged_queries() -> None:
    batch = InputBatch(
        req_ids=["a", "b"],
        token_ids={"a": [10, 11, 12, 13], "b": [20, 21, 22]},
        query_start={"a": 1, "b": 2},
        query_end={"a": 4, "b": 3},
    )
    buffers = InputBuffers(max_num_reqs=4, max_num_tokens=8)
    buffers.materialize(batch, slot_mapping_by_req={"a": [101, 102, 103], "b": [201]})
    assert buffers.num_reqs == 2
    assert buffers.num_tokens == 4
    assert buffers.query_start_loc[:3] == [0, 3, 4]
    assert buffers.input_ids[:4] == [11, 12, 13, 22]
    assert buffers.positions[:4] == [1, 2, 3, 2]
    assert buffers.slot_mapping[:4] == [101, 102, 103, 201]
    assert buffers.is_padding[:4] == [False, False, False, False]


def test_build_attn_metadata_rejects_bad_slot_mapping() -> None:
    try:
        build_attn_metadata(
            seq_lens={"a": 4},
            query_start={"a": 1},
            query_end={"a": 4},
            slot_mapping_by_req={"a": [7]},
            req_order=["a"],
        )
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "slot_mapping len mismatch" in str(e)


def test_inmemory_agent_returns_c11_tables() -> None:
    ag = InMemoryAgent()
    plan = PreparePlan(
        step_id=1,
        forward_mode=ForwardMode.EXTEND,
        read_set=[],
        write_set=[ReqIoSet(req_id="r1", token_start=0, token_end=9)],
        num_scheduled_tokens={"r1": 9},
    )
    ready = ag.prepare_step(plan)
    assert ready.block_table_by_req["r1"] == [0, 1]
    assert ready.slot_mapping_by_req["r1"] == list(range(9))
