"""mooncake 主线 HTTP 管理面的只读客户端（标准库 urllib + json）。

两条端点都只在主线有、且这里只读：

- ``GET /batch_query_keys``：``BatchGetReplicaListForAdmin``，零副作用
  （不授 lease、不触发 promote、不计命中）。副本核验走这里，用内存副本
  的 ``transport_endpoint_`` 和段名单比。提回不用这条：提回要累计命中，
  走绑定的 ``GetReplicaList``。
- ``GET /api/v1/tenant_quotas``：租户配额快照（requested / effective /
  charged）。这是租户记账，不是段剩余空间，不能拿来当段剩余容量。

0.3.11 没有零副作用核验，也没有租户配额；本模块不服务那个版本。
写类端点（配额 PUT/DELETE）不在这里。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Sequence


class AdminMainError(RuntimeError):
    """主线管理端点调用失败，或响应不是约定的 JSON。"""


# 单批 GET 的请求行上限。长前缀把全部 key 拼进一条 URL 会到几十 KB。
_MAX_BATCH_QUERY_REQUEST_BYTES = 8192


@dataclass(frozen=True)
class MemoryReplicaView:
    """一条内存副本里核验用得上的字段。

    ``transport_endpoint`` 是传输端点，不是 seg 名。核验用它和计划里的
    段比，判断这份副本还在不在那个端点上。
    """

    transport_endpoint: str
    size: int | None = None
    protocol: str | None = None


@dataclass(frozen=True)
class KeyReplicaQuery:
    """一个 key 的零副作用查询结果。"""

    key: str
    ok: bool
    error: str | None = None
    memory: tuple[MemoryReplicaView, ...] = ()
    # disk_values 与 local_disk_values 的条数。降层只看有没有盘副本，
    # 不当段剩余空间。
    disk_replicas: int = 0
    # local_disk 的传输端点（offload RPC 端口）。和挂段时的 seg 名不是同一套。
    # 软钉看到它非空就拒绝空写。提回不用它去对 seg 名。
    local_disk_endpoints: tuple[str, ...] = ()


@dataclass(frozen=True)
class TenantQuotaSnapshot:
    """一个租户的配额快照。

    字段与主线 ``HttpTenantQuotaSnapshot`` 对齐。没有「段剩余字节」：
    ``charged_bytes`` 是该租户已记账的量，不是某条 seg 还剩多少。
    """

    tenant_id: str
    requested_quota_bytes: int
    effective_quota_bytes: int
    charged_bytes: int
    admission_closed: bool
    over_quota: bool
    has_explicit_policy: bool


def parse_batch_query_keys(body: str) -> dict[str, KeyReplicaQuery]:
    """解析 ``GET /batch_query_keys`` 的 JSON 正文。

    成功体：``{"success": true, "data": {key: {ok, error?, values?}}}``。
    ``values`` 与 ``nof_values`` 都是可读副本描述（``transport_endpoint_``），
    合并进内存端点列表，供核验使用。NOF 不算盘副本。盘副本只计条数
    （``disk_values`` + ``local_disk_values``），不进内存端点列表。
    """
    payload = _load_object(body)
    _require_success(payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AdminMainError("batch_query_keys: data is not an object")
    out: dict[str, KeyReplicaQuery] = {}
    for key, item in data.items():
        if not isinstance(item, dict):
            raise AdminMainError(f"batch_query_keys: result for {key!r} is not an object")
        ok = bool(item.get("ok"))
        error = item.get("error")
        if error is not None and not isinstance(error, str):
            raise AdminMainError(f"batch_query_keys: error for {key!r} is not a string")
        memory: tuple[MemoryReplicaView, ...] = ()
        disks = 0
        local_disk: tuple[str, ...] = ()
        if ok:
            memory = _memory_views(key, item.get("values")) + _memory_views(
                key, item.get("nof_values")
            )
            disks = _replica_count(item.get("disk_values")) + _replica_count(
                item.get("local_disk_values")
            )
            local_disk = _local_disk_endpoints(key, item.get("local_disk_values"))
        out[str(key)] = KeyReplicaQuery(
            key=str(key),
            ok=ok,
            error=error,
            memory=memory,
            disk_replicas=disks,
            local_disk_endpoints=local_disk,
        )
    return out


def parse_tenant_quotas(body: str) -> tuple[TenantQuotaSnapshot, ...]:
    """解析 ``GET /api/v1/tenant_quotas`` 的 JSON 正文。

    不带 ``tenant_id`` 时 ``data`` 是数组；带了则 ``data`` 是单个对象。
    两种都收成元组。
    """
    payload = _load_object(body)
    _require_success(payload)
    data = payload.get("data")
    if isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        raise AdminMainError("tenant_quotas: data is neither object nor array")
    return tuple(_quota(row) for row in rows)


class MainlineAdmin:
    """主线 master 管理端点的只读客户端。

    ``fetcher`` 供测试注入：给定完整 URL，返回响应正文。不注入则用
    ``urllib.request``。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        fetcher: Callable[[str], str] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._fetcher = fetcher

    def batch_query_keys(self, keys: Sequence[str]) -> dict[str, KeyReplicaQuery]:
        """``GET /batch_query_keys?keys=k1,k2``。key 逐个百分号编码，逗号作分隔。

        按请求行长度分批。单批 ``GET {path} HTTP/1.1`` 不超过 8KB，
        各批结果合并后返回。一把 key 自己就超过上限时仍单独发出
        （切不开）。空列表报错。
        """
        if not keys:
            raise AdminMainError("batch_query_keys requires at least one key")
        merged: dict[str, KeyReplicaQuery] = {}
        batch: list[str] = []
        for key in keys:
            candidate = [*batch, str(key)]
            if batch and self._request_line_len(candidate) > _MAX_BATCH_QUERY_REQUEST_BYTES:
                merged.update(self._fetch_batch_query(batch))
                batch = [str(key)]
            else:
                batch = candidate
        merged.update(self._fetch_batch_query(batch))
        return merged

    def _batch_query_path(self, keys: Sequence[str]) -> str:
        joined = ",".join(urllib.parse.quote(k, safe="") for k in keys)
        return f"/batch_query_keys?keys={joined}"

    def _request_line_len(self, keys: Sequence[str]) -> int:
        """``GET {path} HTTP/1.1`` 的字节数。path 含已编码的 key。"""
        path = self._batch_query_path(keys)
        return len(b"GET ") + len(path.encode()) + len(b" HTTP/1.1")

    def _fetch_batch_query(self, keys: Sequence[str]) -> dict[str, KeyReplicaQuery]:
        return parse_batch_query_keys(self._get(self._batch_query_path(keys)))

    def get_tenant_quotas(
        self, tenant_id: str | None = None
    ) -> tuple[TenantQuotaSnapshot, ...]:
        """``GET /api/v1/tenant_quotas``，可带 ``tenant_id`` 查一个。"""
        path = "/api/v1/tenant_quotas"
        if tenant_id is not None:
            if tenant_id == "":
                raise AdminMainError("tenant_id must not be empty")
            path += "?tenant_id=" + urllib.parse.quote(tenant_id, safe="")
        return parse_tenant_quotas(self._get(path))

    def _get(self, path: str) -> str:
        url = f"{self._base}{path}"
        if self._fetcher is not None:
            return self._fetcher(url)
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            raise AdminMainError(f"GET {url} HTTP {e.code}: {detail}") from e
        except Exception as e:
            raise AdminMainError(
                f"GET {url} failed ({type(e).__name__}: {e})"
            ) from e


def _load_object(body: str) -> dict:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise AdminMainError(f"admin response is not JSON: {e}") from e
    if not isinstance(payload, dict):
        raise AdminMainError("admin response is not a JSON object")
    return payload


def _require_success(payload: dict) -> None:
    if payload.get("success") is not True:
        raise AdminMainError(f"admin response success is not true: {payload!r}")


def _local_disk_endpoints(key: str, values: object) -> tuple[str, ...]:
    """local_disk 描述里的传输端点。没有端点字段的条目不计入。"""
    if values is None:
        return ()
    if not isinstance(values, list):
        raise AdminMainError("batch_query_keys: disk replica field is not a list")
    found: list[str] = []
    for desc in values:
        if not isinstance(desc, dict):
            raise AdminMainError(
                f"batch_query_keys: replica of {key!r} is not an object"
            )
        endpoint = desc.get("transport_endpoint")
        if endpoint is None:
            endpoint = desc.get("transport_endpoint_")
        if isinstance(endpoint, str) and endpoint:
            found.append(endpoint)
    return tuple(found)


def _replica_count(values: object) -> int:
    if values is None:
        return 0
    if not isinstance(values, list):
        raise AdminMainError("batch_query_keys: disk replica field is not a list")
    return len(values)


def _memory_views(key: str, values: object) -> tuple[MemoryReplicaView, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        raise AdminMainError(f"batch_query_keys: values for {key!r} is not a list")
    views: list[MemoryReplicaView] = []
    for desc in values:
        if not isinstance(desc, dict):
            raise AdminMainError(
                f"batch_query_keys: replica of {key!r} is not an object"
            )
        endpoint = desc.get("transport_endpoint_")
        if not isinstance(endpoint, str) or not endpoint:
            raise AdminMainError(
                f"batch_query_keys: replica of {key!r} has no transport_endpoint_"
            )
        size = desc.get("size_")
        protocol = desc.get("protocol_")
        views.append(
            MemoryReplicaView(
                transport_endpoint=endpoint,
                size=int(size) if isinstance(size, int) and not isinstance(size, bool) else None,
                protocol=protocol if isinstance(protocol, str) else None,
            )
        )
    return tuple(views)


def _quota(row: object) -> TenantQuotaSnapshot:
    if not isinstance(row, dict):
        raise AdminMainError("tenant_quotas: row is not an object")
    try:
        return TenantQuotaSnapshot(
            tenant_id=str(row["tenant_id"]),
            requested_quota_bytes=_u64(row["requested_quota_bytes"]),
            effective_quota_bytes=_u64(row["effective_quota_bytes"]),
            charged_bytes=_u64(row["charged_bytes"]),
            admission_closed=bool(row["admission_closed"]),
            over_quota=bool(row["over_quota"]),
            has_explicit_policy=bool(row["has_explicit_policy"]),
        )
    except KeyError as e:
        raise AdminMainError(f"tenant_quotas: missing {e.args[0]}") from e
    except (TypeError, ValueError) as e:
        raise AdminMainError(f"tenant_quotas: bad field: {e}") from e


def _u64(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected integer byte count, got {value!r}")
    if value < 0:
        raise ValueError(f"byte count is negative: {value}")
    return value
