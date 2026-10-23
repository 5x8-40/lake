"""kvcm.engines.vllm.keys 单元检查。

纯字符串用例随处可跑；``test_upstream_parity`` 是容器内漂移哨兵：内置
rc1 镜像与真实 vllm-ascend PoolKey 逐字节比对——变红 = 上游改了 key 格
式、镜像回退已过时（更新 KeySpec 或删掉回退）。容器外自动跳过。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from kvcm.engines.vllm import keys as km
from kvcm.engines.vllm.keys import (
    KeySpec,
    expand_store_keys,
    group_keys_by_rank,
    load_keys_file,
    parse_key_rank,
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
        model_name="qwen", chunk_hashes=["h0"], tp_size=2, prefer_upstream=False
    )
    assert len(keys) == 2
    assert "head_or_tp_rank:0" in keys[0]
    assert "head_or_tp_rank:1" in keys[1]


def test_expand_tp4_two_blocks() -> None:
    keys = expand_store_keys(
        model_name="qwen", chunk_hashes=["h0", "h1"], tp_size=4, prefer_upstream=False
    )
    assert len(keys) == 8
    by = group_keys_by_rank(keys)
    assert list(by) == [0, 1, 2, 3]
    assert all(len(v) == 2 for v in by.values())


def test_parse_and_group() -> None:
    keys = expand_store_keys(
        model_name="qwen", chunk_hashes=["h0"], tp_size=2, prefer_upstream=False
    )
    assert parse_key_rank(keys[0]) == 0
    assert parse_key_rank(keys[1]) == 1
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


def test_load_keys_file() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# comment\n\nkey_a\n  key_b  \n")
        path = f.name
    try:
        assert load_keys_file(path) == ["key_a", "key_b"]
    finally:
        os.unlink(path)


def test_key_computer_expand() -> None:
    kc = km.KeyComputer(model_name="qwen", tp_size=2)
    keys = kc.expand(["h0"])
    assert len(keys) == 2
    assert parse_key_rank(keys[0]) == 0
    # 异构 TP：peer 更大则按 peer 展开
    kc_h = km.KeyComputer(model_name="qwen", tp_size=2, peer_tp_size=4)
    assert kc_h.effective_tp == 4
    assert len(kc_h.expand(["h0"])) == 4


@pytest.mark.skipif(
    km.upstream_key_classes() is None,
    reason="vllm_ascend not importable (offline)",
)
def test_upstream_parity() -> None:
    """容器内哨兵：镜像必须与上游 PoolKey 逐字节一致。"""
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
    assert up_l == mir_l, f"DRIFT (layerwise): upstream={up_l}\nmirror={mir_l}"
