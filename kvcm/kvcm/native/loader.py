"""加载 kvcm_native（自封装 .so）。

路径 A：.so 接收 store_py 已经建好的 client，不另起一个 UUID。
本仓库不编译这个 .so，也不改 3rdparty/mooncake。测试注入假模块。
"""

from __future__ import annotations

import importlib


class NativeMissing(RuntimeError):
    """自封装 .so 不在。软钉没有钉上，保留身份也没有注册。"""


def load_native(module_name: str = "kvcm_native"):
    """导入扩展模块。ImportError 收成 NativeMissing，不返回空成功。"""
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise NativeMissing(
            "kvcm_native 未加载：软钉空写和保留身份都没有执行"
            "（没有钉上，也没有注册 retained client）"
        ) from e
