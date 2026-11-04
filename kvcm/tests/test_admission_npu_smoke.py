"""段容量准入真机冒烟（NPU + Mooncake + 真 /query_segment）。

不进 GitHub Actions。本地有床时::

    KVCM_NPU_SMOKE=1 python -m pytest tests/test_admission_npu_smoke.py -m npu -q

脚本本体：``demo/tools/admission_real_smoke.py``（可用 ``ADMIT_*`` 环境变量）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.npu

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "demo"
    / "tools"
    / "admission_real_smoke.py"
)


def test_admission_blocks_when_need_exceeds_remaining() -> None:
    assert _SCRIPT.is_file(), f"missing {_SCRIPT}"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(_SCRIPT.parents[2]),  # kvcm/
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"admission smoke exit={proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
