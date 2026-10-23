#!/usr/bin/env python3
"""MooncakeBackend._build_plan 与 Ledger 单元检查（离线，无 mooncake 依赖）。"""

from __future__ import annotations

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.client import StoreClient
from kvcm.engines.vllm.keys import KeyComputer, expand_store_keys
from kvcm.ledger import Ledger, prefix_key
from kvcm.ops.types import Prefix


def _backend(tp: int = 1, peer_tp: int | None = None) -> MooncakeBackend:
    return MooncakeBackend(
        client=StoreClient(),  # 未 connect：_build_plan 不触碰 client
        key_computer=KeyComputer(model_name="qwen", tp_size=tp, peer_tp_size=peer_tp),
        locate=lambda _inst: [],
    )


def _keys(tp: int, hashes: list[str] | None = None) -> list[str]:
    return expand_store_keys(
        model_name="qwen",
        chunk_hashes=hashes or ["h0"],
        tp_size=tp,
        prefer_upstream=False,
    )


def test_plan_tp1_single_seg() -> None:
    b = _backend(tp=1)
    plan = b._build_plan(_keys(1), ["10.0.0.1:5001"])
    assert plan == [(k, "10.0.0.1:5001") for k in _keys(1)]


def test_plan_tp2_rank_mapped() -> None:
    b = _backend(tp=2)
    keys = _keys(2)
    plan = b._build_plan(keys, ["10.0.0.1:5001", "10.0.0.1:5002"])
    assert plan[0] == (keys[0], "10.0.0.1:5001")
    assert plan[1] == (keys[1], "10.0.0.1:5002")


def test_plan_hetero_reverse_expands() -> None:
    """peer 4 > local 2：eff rank i → targets[i//2]（seg 列表按倍展开）。"""
    b = _backend(tp=2, peer_tp=4)
    keys = _keys(4)
    plan = b._build_plan(keys, ["10.0.0.1:5001", "10.0.0.1:5002"])
    expect = ["10.0.0.1:5001", "10.0.0.1:5001", "10.0.0.1:5002", "10.0.0.1:5002"]
    assert [seg for _, seg in plan] == expect


def test_plan_rank_overflow_fails() -> None:
    b = _backend(tp=2)
    keys = _keys(4)  # rank 0..3，但无 peer 配置 → 不展开
    try:
        b._build_plan(keys, ["10.0.0.1:5001", "10.0.0.1:5002"])
    except ValueError as e:
        assert "rank max=3" in str(e)
        return
    raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_ledger_four_tables() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        led = Ledger(path)
        p = Prefix(model_id="qwen", token_ids=(1, 2, 3))

        led.upsert_task("t1", op="copy", prefix=p, target="inst-B", status="running")
        rec = led.get_task("t1")
        assert rec and rec.op == "copy" and rec.status == "running"
        led.upsert_task("t1", op="copy", prefix=p, target="inst-B", status="ready")
        assert led.get_task("t1").status == "ready"  # type: ignore[union-attr]

        led.register_keepalive(p)
        assert led.list_keepalive() == [prefix_key(p)]
        led.unregister_keepalive(p)
        assert led.list_keepalive() == []

        led.put_replica("k1", "inst-B")
        assert led.list_replicas("k1") == ["inst-B"]
        led.delete_replica("k1", "inst-B")
        assert led.list_replicas("k1") == []

        led.bind_session("s1", p, "inst-B")
        assert led.list_session("s1") == [prefix_key(p)]
        led.unbind_session("s1")
        assert led.list_session("s1") == []

        led.close()
    finally:
        os.unlink(path)


TESTS = [
    test_plan_tp1_single_seg,
    test_plan_tp2_rank_mapped,
    test_plan_hetero_reverse_expands,
    test_plan_rank_overflow_fails,
    test_ledger_four_tables,
]

if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"test_backend: PASS ({len(TESTS)} cases)")
