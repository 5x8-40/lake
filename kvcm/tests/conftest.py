"""pytest 共享配置：kvcm 包根入 sys.path（仓库根 = 本目录上一级）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
