#!/usr/bin/env python3
"""Unit checks for keys.py.

Pure-string tests run anywhere. ``test_upstream_parity`` is the in-container
DRIFT SENTINEL: it compares the offline rc1 mirror against the real
vllm-ascend PoolKey byte-for-byte — red means upstream changed the key
format and the mirror fallback in keys.py is stale (update KeySpec or drop
the fallback). It skips silently outside a vllm-ascend container.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRECOPY = os.path.join(os.path.dirname(_HERE), "precopy")
if _PRECOPY not in sys.path:
    sys.path.insert(0, _PRECOPY)

import keys as km
from keys import (
    KeySpec,
    expand_store_keys,
    group_keys_by_rank,
    parse_head_or_tp_rank,
)


def test_pool_key_format() -> None:
    s = KeySpec(model_name="qwen", chunk_hash="abc").to_string()
    assert s == (
        "qwen@pcp:0@dcp:0@head_or_tp_rank:0@pp_rank:0"
        "@group:0@cache_role:kv@cache_family:default@abc"
    )


def test_layer_key_format() -> None:
    s = KeySpec(model_name="qwen", chunk_hash="abc", layer_id=3).to_string()
    assert "@layer_id:3@" in s
    assert "@pp_rank:" not in s


def test_expand_tp2() -> None:
    keys = expand_store_keys(
        model_name="qwen",
        chunk_hashes=["h0"],
        tp_size=2,
        prefer_upstream=False,
    )
    assert len(keys) == 2
    assert "head_or_tp_rank:0" in keys[0]
    assert "head_or_tp_rank:1" in keys[1]


def test_expand_tp4_two_blocks() -> None:
    keys = expand_store_keys(
        model_name="qwen",
        chunk_hashes=["h0", "h1"],
        tp_size=4,
        prefer_upstream=False,
    )
    assert len(keys) == 8
    by = group_keys_by_rank(keys)
    assert list(by) == [0, 1, 2, 3]
    assert all(len(v) == 2 for v in by.values())


def test_parse_and_group() -> None:
    keys = expand_store_keys(
        model_name="qwen", chunk_hashes=["h0"], tp_size=2, prefer_upstream=False
    )
    assert parse_head_or_tp_rank(keys[0]) == 0
    assert parse_head_or_tp_rank(keys[1]) == 1
    by = group_keys_by_rank(keys)
    assert by[0] == [keys[0]]
    assert by[1] == [keys[1]]


def test_expand_layerwise() -> None:
    keys = expand_store_keys(
        model_name="qwen",
        chunk_hashes=["h0"],
        include_layers=True,
        num_layers=2,
        prefer_upstream=False,
    )
    assert len(keys) == 2
    assert all("@layer_id:" in k for k in keys)


def test_upstream_parity() -> None:
    """In-container sentinel: mirror must byte-match upstream PoolKey."""
    if km.upstream_key_classes() is None:
        print("skip upstream parity: vllm_ascend not importable (offline)")
        return
    for tp, step in [(1, 1), (2, 1), (4, 1), (4, 2), (4, 4)]:
        up = expand_store_keys(
            model_name="qwen",
            chunk_hashes=["h0", "h1"],
            tp_size=tp,
            put_step=step,
            prefer_upstream=True,
        )
        mir = expand_store_keys(
            model_name="qwen",
            chunk_hashes=["h0", "h1"],
            tp_size=tp,
            put_step=step,
            prefer_upstream=False,
        )
        assert up == mir, (
            f"DRIFT: upstream PoolKey != builtin mirror (tp={tp} put_step={step})\n"
            f"upstream={up}\nmirror ={mir}\n"
            "-> update KeySpec (or drop the offline fallback)"
        )
    up_l = expand_store_keys(
        model_name="qwen",
        chunk_hashes=["h0"],
        tp_size=2,
        include_layers=True,
        num_layers=2,
        prefer_upstream=True,
    )
    mir_l = expand_store_keys(
        model_name="qwen",
        chunk_hashes=["h0"],
        tp_size=2,
        include_layers=True,
        num_layers=2,
        prefer_upstream=False,
    )
    assert up_l == mir_l, (
        f"DRIFT (layerwise): upstream={up_l}\nmirror={mir_l}"
    )


TESTS = [
    test_pool_key_format,
    test_layer_key_format,
    test_expand_tp2,
    test_expand_tp4_two_blocks,
    test_parse_and_group,
    test_expand_layerwise,
    test_upstream_parity,
]

if __name__ == "__main__":
    for t in TESTS:
        t()
    print("test_keys: PASS")
