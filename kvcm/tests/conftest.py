"""pytest 共享配置：kvcm 包根入 sys.path（仓库根 = 本目录上一级）。

为什么不 pip install -e .：kvcm 暂无独立打包配置（pyproject.toml），
包消费方目前只有本测试目录与 demo 脚本，sys.path 注入是最小可用方案。
代价：IDE / 类型检查器对 ``import kvcm`` 的解析依赖编辑器自身的
rootdir 推断。日后 kvcm 打包（加 pyproject.toml）时删本文件、CI 改
``pip install -e .``。

标记：
- ``smoke``：无 NPU 核心单测，CI / 本地都可跑
- ``npu``：真机冒烟；未设 ``KVCM_NPU_SMOKE=1`` 时自动 skip（CI 不跑）
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_collection_modifyitems(config, items):
    """未显式打开时跳过 ``npu``，避免 GitHub Actions 误拉真机床。"""
    if os.environ.get("KVCM_NPU_SMOKE") == "1":
        return
    skip_npu = pytest.mark.skip(
        reason="NPU 真机冒烟：本地设 KVCM_NPU_SMOKE=1 后 pytest -m npu"
    )
    for item in items:
        if "npu" in item.keywords:
            item.add_marker(skip_npu)


def run(coro):
    """跑一个协程到完成（测试用）。

    kvcm 的执行模型是协程（E11），测试需要 await。不引入
    pytest-asyncio：那会给 CI 加一个插件依赖，而这里的用法只是「跑完
    一个协程、拿结果或异常」，`asyncio.run` 就够——每个测试各自一个
    干净的事件循环，互不干扰。
    """
    import asyncio

    return asyncio.run(coro)
