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
