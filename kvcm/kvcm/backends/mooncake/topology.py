"""SegTopology：mooncake 段拓扑——seg → client/实例 映射与 seg 语义。

数据源（demo 环境）：master HTTP 管理端点 ``/get_all_segments``
（纯文本，一行一个 ``ip:port``）。生产权威源 = 实例注册（接口层
InstanceWatcher）：本类只保留 seg 侧语义与查询入口，注册数据到达后换
数据源即可，调用方不变。
"""

from __future__ import annotations

from kvcm.backends.mooncake.http_admin import AdminClient


class SegTopologyError(RuntimeError):
    """段拓扑查询失败（fail loud：静默拿错的 seg 映射 = 复制到错误的段）。"""


class SegTopology:
    """mooncake 段拓扑视图。"""

    def __init__(self, admin: AdminClient | str) -> None:
        if isinstance(admin, str):
            admin = AdminClient(admin)
        self._admin = admin

    def segments(self) -> list[str]:
        """全部已挂载 seg（``ip:port`` 列表）。"""
        body = self._admin.get_text("/get_all_segments")
        segs = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if not segs:
            raise SegTopologyError("empty segment list from /get_all_segments")
        return segs
