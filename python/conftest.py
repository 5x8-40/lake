"""共享测试守卫(issue #72):Qwen3-0.6B 本地可用性检测,三处拷贝收口于此。

测试文件分布在 `lake/engine/tests/` 与 `lake/runtime/tests/`,conftest 放
公共祖先 `python/` 根(PYTHONPATH=. 时 `from conftest import ...` 可用)。
"""

from __future__ import annotations

import os

import pytest

QWEN3_0_6B_MODEL_ID = os.path.expanduser(
    os.environ.get("LAKE_TEST_QWEN3_MODEL_PATH", "Qwen/Qwen3-0.6B")
)


def qwen3_available() -> bool:
    """显式路径(env)存在,或默认 hub id 已在本地缓存;否则 False(跳过,不翻墙拉取)。

    env 覆盖优先且不再回退 hub 缓存:显式给错路径时应跳过而非误用缓存模型。
    """

    override = os.environ.get("LAKE_TEST_QWEN3_MODEL_PATH")
    if override:
        return os.path.exists(os.path.expanduser(override))
    try:
        from huggingface_hub import try_to_load_from_cache

        return try_to_load_from_cache("Qwen/Qwen3-0.6B", "config.json") is not None
    except Exception:
        return False


requires_qwen3 = pytest.mark.skipif(
    not qwen3_available(),
    reason="Qwen3-0.6B 不在本地缓存且未设 LAKE_TEST_QWEN3_MODEL_PATH(离线环境跳过)",
)
