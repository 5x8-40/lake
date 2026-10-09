#!/usr/bin/env python3
"""Pure unit checks for keys.py (no mooncake / NPU / vllm)."""

from __future__ import annotations

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
    )
    assert len(keys) == 2
    assert "head_or_tp_rank:0" in keys[0]
    assert "head_or_tp_rank:1" in keys[1]


def test_expand_tp4_two_blocks() -> None:
    keys = expand_store_keys(
        model_name="qwen",
        chunk_hashes=["h0", "h1"],
        tp_size=4,
    )
    assert len(keys) == 8
    by = group_keys_by_rank(keys)
    assert list(by) == [0, 1, 2, 3]
    assert all(len(v) == 2 for v in by.values())


def test_parse_and_group() -> None:
    keys = expand_store_keys(model_name="qwen", chunk_hashes=["h0"], tp_size=2)
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
    )
    assert len(keys) == 2
    assert all("@layer_id:" in k for k in keys)


if __name__ == "__main__":
    test_pool_key_format()
    test_layer_key_format()
    test_expand_tp2()
    test_expand_tp4_two_blocks()
    test_parse_and_group()
    test_expand_layerwise()
    print("test_keys: PASS")
