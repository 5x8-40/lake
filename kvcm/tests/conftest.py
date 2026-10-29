"""pytest 共享配置：kvcm 包根入 sys.path（仓库根 = 本目录上一级）。

为什么不 pip install -e .：kvcm 暂无独立打包配置（pyproject.toml），
包消费方目前只有本测试目录与 demo 脚本，sys.path 注入是最小可用方案。
代价：IDE / 类型检查器对 ``import kvcm`` 的解析依赖编辑器自身的
rootdir 推断。日后 kvcm 打包（加 pyproject.toml）时删本文件、CI 改
``pip install -e .``。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(coro):
    """跑一个协程到完成（测试用）。

    kvcm 的执行模型是协程（E11），测试需要 await。不引入
    pytest-asyncio：那会给 CI 加一个插件依赖，而这里的用法只是「跑完
    一个协程、拿结果或异常」，`asyncio.run` 就够——每个测试各自一个
    干净的事件循环，互不干扰。
    """
    import asyncio

    return asyncio.run(coro)
