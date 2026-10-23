"""master HTTP 管理端点的最小封装（urllib，无第三方依赖）。

查询/观测类端点走这一面：无需 client 身份，调用方进程死了也不在
master 留任何状态。写类操作不在这面（HTTP 无写类端点）。
"""

from __future__ import annotations

import urllib.request


class AdminError(RuntimeError):
    """HTTP 管理端点调用失败（连接、超时、非 2xx）。"""


class AdminClient:
    """master 管理端点客户端。"""

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def get_text(self, path: str) -> str:
        """GET 端点返回文本（如 /get_all_segments 的行列表）。"""
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                return resp.read().decode()
        except Exception as e:
            raise AdminError(
                f"GET {url} failed ({type(e).__name__}: {e}) — master admin down?"
            ) from e
