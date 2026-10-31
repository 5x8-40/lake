"""自封装 .so 的 Python 加载器（路径 A）。

软钉空写和保留身份都从这里进。模块不在就报错，调用方不能把这次调用
当成已经钉上或已经保留。
"""

from kvcm.native.loader import NativeMissing, load_native

__all__ = ["NativeMissing", "load_native"]
