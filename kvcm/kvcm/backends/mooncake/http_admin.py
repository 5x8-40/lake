"""master HTTP 管理端点的最小封装（urllib，无第三方依赖）。

查询/观测类端点走这一面：无需 client 身份，调用方进程死了也不在
master 留任何状态。写类操作不在这面（HTTP 无写类端点）。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from urllib.parse import quote

# Capacity 那一行冒号前有一个空格（0.3.11 rpc_service.cpp 与主线
# master_admin_service.cpp::HandleQuerySegment 正文相同）。\s* 把
# 「有空格 / 没空格」都吃掉，避免只认一种写法。
_USED_RE = re.compile(r"^Used\(bytes\):\s*(\d+)\s*$", re.MULTILINE)
_CAP_RE = re.compile(r"^Capacity\(bytes\)\s*:\s*(\d+)\s*$", re.MULTILINE)
_NOT_FOUND_BODY = "Failed to query segment"


class AdminError(RuntimeError):
    """HTTP 管理端点调用失败（连接、超时、非 2xx）。

    status 为空 = 连都没连上（对端拒绝、超时、DNS）。有状态码 = HTTP
    已经应答。段不存在不是本类的「暂时失败」，见 SegmentNotFound。
    """

    def __init__(
        self, message: str, *, status: int | None = None, body: str = ""
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class SegmentNotFound(AdminError):
    """目标段不存在。

    0.3.11 / 主线都是：allocator 容量之和为 0 时 QuerySegments 返回
    SEGMENT_NOT_FOUND，HTTP 端点以 500 正文「Failed to query segment」
    失败。这不是剩余 0——段根本不在，调用方应立即失败，不要空等。
    """


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
                f"GET {url} failed ({type(e).__name__}: {e}) — master admin down?",
                status=None,
            ) from e

    def query_segment(self, segment: str) -> tuple[int, int]:
        """GET /query_segment?segment=名字，返回 (used, capacity)。

        这两个数是该段 allocator 的 size()/capacity() 之和。剩余由调用方
        算 capacity - used。段不存在（含容量为 0）抛 SegmentNotFound。
        连不上抛 AdminError（status 为空）；其它 5xx 抛 AdminError 并带上
        状态码——那是暂时失败，由调用方重试，本方法不把失败当成剩余 0。
        """
        # 段名是 ip:port，冒号和方括号留着，避免依赖服务端是否解码 %3A。
        path = "/query_segment?segment=" + quote(segment, safe=":/[]")
        status, body = self._read(path)
        if body.strip() == _NOT_FOUND_BODY or status == 404:
            raise SegmentNotFound(
                f"segment not found: {segment}",
                status=status,
                body=body,
            )
        if status >= 500:
            raise AdminError(
                f"GET {path} status={status} body={body!r}",
                status=status,
                body=body,
            )
        parsed = _parse_query_segment(body)
        if parsed is None:
            raise AdminError(
                f"query_segment unparseable status={status} body={body!r}",
                status=status,
                body=body,
            )
        used, capacity = parsed
        if capacity == 0:
            raise SegmentNotFound(
                f"segment not found: {segment} (capacity 0)",
                status=status,
                body=body,
            )
        return used, capacity

    def _read(self, path: str) -> tuple[int, str]:
        """GET，返回 (status, body)。HTTP 错误不抛，把状态码交回调用方。

        连不上才抛 AdminError（status 为空）。段不存在在 0.3.11 是 500
        正文，和网关暂时 5xx 必须分开，所以不能在这里把非 2xx 一概包掉。
        """
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                status = resp.getcode()
                return int(status), resp.read().decode()
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp is not None else b""
            return int(e.code), raw.decode(errors="replace")
        except Exception as e:
            raise AdminError(
                f"GET {url} failed ({type(e).__name__}: {e}) — master admin down?",
                status=None,
            ) from e


def _parse_query_segment(body: str) -> tuple[int, int] | None:
    used_m = _USED_RE.search(body)
    cap_m = _CAP_RE.search(body)
    if used_m is None or cap_m is None:
        return None
    return int(used_m.group(1)), int(cap_m.group(1))
