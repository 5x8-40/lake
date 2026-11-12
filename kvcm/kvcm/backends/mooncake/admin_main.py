"""mooncake 主线 HTTP 管理面的只读客户端（标准库 urllib + json）。

两条端点都只在主线有、且这里只读：

- ``GET /batch_query_keys``：``BatchGetReplicaListForAdmin``，零副作用
  （不授 lease、不触发 promote、不计命中）。副本核验走这里，用内存副本
  的 ``transport_endpoint_`` 和段名单比。提回不用这条：提回要累计命中，
  走绑定的 ``GetReplicaList``。
- ``GET /api/v1/tenant_quotas``：租户配额快照（requested / effective /
  charged）。这是租户记账，不是段剩余空间，不能拿来当段剩余容量。
- ``GET /get_segments_detail``：全池段明细（``segment_name`` /
  ``te_endpoint`` 等）。启动时用来核对 ``--instance-segs`` 是否和
  传输端点同名（核验比的是端点，不是另一套外号）。

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


# 单批 GET 的请求行上限（只计 ``GET {path} HTTP/1.1``，不含 header）。
# 长前缀把全部 key 拼进一条 URL 会到几十 KB。8KB 是保守值，未与 master
# 真实限制校准；要改就按目标版本实测或改 POST。
_MAX_BATCH_QUERY_REQUEST_BYTES = 8192


@dataclass(frozen=True)
class MemoryReplicaView:
    """一条内存副本里核验用得上的字段。

    ``transport_endpoint`` 是传输端点，不是 seg 名。核验用它和计划里的
    段比，判断这份副本还在不在那个端点上。``size`` / ``buffer_address``
    一并解析出来；core 核验只用端点，地址字段留给后续 soft pin PR。
    """

    transport_endpoint: str
    size: int | None = None
    protocol: str | None = None
    buffer_address: int | None = None


@dataclass(frozen=True)
class KeyReplicaQuery:
    """一个 key 的零副作用查询结果。"""

    key: str
    ok: bool
    error: str | None = None
    # DRAM 内存副本（values）。核验 READY 只认这一组。
    memory: tuple[MemoryReplicaView, ...] = ()
    # NoF 副本（nof_values）。不是 DRAM，不参与 READY 核验；软钉也不把它
    # 当 memory。单独留字段，避免和 memory 混在一起造成假 READY / 假单副本。
    nof: tuple[MemoryReplicaView, ...] = ()
    # disk_values + local_disk_values 的条数。软钉看到任一非零就拒绝空写。
    disk_replicas: int = 0
    # 其中 LOCAL_DISK 副本的条数。demote 只认这个：DFS 副本不能生成
    # 可提回的内存副本（promotion 的 source 必须 LOCAL_DISK）。
    local_disk_replicas: int = 0
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


@dataclass(frozen=True)
class SegmentDetail:
    """一条段明细（``GET /get_segments_detail`` 的一项）。

    核验契约：``--instance-segs`` 填的字符串必须等于 ``te_endpoint``，
    且 master 上 ``segment_name`` 与之相同（正确挂段姿势下二者本就同名）。
    """

    segment_name: str
    te_endpoint: str
    protocol: str = ""
    status: str = ""


def parse_batch_query_keys(body: str) -> dict[str, KeyReplicaQuery]:
    """解析 ``GET /batch_query_keys`` 的 JSON 正文。

    成功体：``{"success": true, "data": {key: {ok, error?, values?}}}``。
    ``values`` 是 DRAM 内存副本，进 ``memory``；``nof_values`` 是 NoF 副本，
    单独进 ``nof``，不并进 ``memory``。核验 READY 只认 ``memory``，避免 NoF
    被当成 DRAM。盘副本只计条数（``disk_values`` + ``local_disk_values``），
    不进内存端点列表。
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
        nof: tuple[MemoryReplicaView, ...] = ()
        disks = 0
        local_disks = 0
        local_disk: tuple[str, ...] = ()
        if ok:
            memory = _memory_views(key, item.get("values"))
            nof = _memory_views(key, item.get("nof_values"))
            local_disks = _replica_count(item.get("local_disk_values"))
            disks = _replica_count(item.get("disk_values")) + local_disks
            local_disk = _local_disk_endpoints(key, item.get("local_disk_values"))
        out[str(key)] = KeyReplicaQuery(
            key=str(key),
            ok=ok,
            error=error,
            memory=memory,
            nof=nof,
            disk_replicas=disks,
            local_disk_replicas=local_disks,
            local_disk_endpoints=local_disk,
        )
    return out


def parse_segments_detail(body: str) -> tuple[SegmentDetail, ...]:
    """解析 ``GET /get_segments_detail`` 的 JSON 正文。

    主线直接序列化 ``{total_segments, segments:[...]}``，没有
    ``success/data`` 信封（与 batch_query_keys 不同）。
    """
    payload = _load_object(body)
    rows = payload.get("segments")
    if not isinstance(rows, list):
        raise AdminMainError("get_segments_detail: segments is not a list")
    out: list[SegmentDetail] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AdminMainError("get_segments_detail: segment row is not an object")
        name = row.get("segment_name")
        endpoint = row.get("te_endpoint")
        if not isinstance(name, str) or not name.strip():
            raise AdminMainError(
                f"get_segments_detail: bad segment_name: {name!r}"
            )
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise AdminMainError(
                f"get_segments_detail: bad te_endpoint for {name!r}: {endpoint!r}"
            )
        protocol = row.get("protocol")
        status = row.get("status")
        out.append(
            SegmentDetail(
                segment_name=name.strip(),
                te_endpoint=endpoint.strip(),
                protocol=protocol if isinstance(protocol, str) else "",
                status=status if isinstance(status, str) else "",
            )
        )
    return tuple(out)


def validate_configured_segs(
    configured: Sequence[str], details: Sequence[SegmentDetail]
) -> None:
    """核对启动登记的 seg 能否用于副本核验。

    对每个配置名 ``S``：master 上必须有 ``segment_name == S``，且该行
    ``te_endpoint == S``。否则 copy 认段名、核验认端点，会一直缺副本。
    """
    by_name = {d.segment_name: d for d in details}
    missing: list[str] = []
    mismatched: list[str] = []
    seen: set[str] = set()
    for raw in configured:
        seg = str(raw).strip()
        if not seg or seg in seen:
            continue
        seen.add(seg)
        row = by_name.get(seg)
        if row is None:
            missing.append(seg)
            continue
        if row.te_endpoint != seg:
            mismatched.append(
                f"{seg!r} te_endpoint={row.te_endpoint!r}"
            )
    if not missing and not mismatched:
        return
    parts: list[str] = []
    if missing:
        parts.append(
            "master 上没有这些 segment_name: " + ", ".join(repr(s) for s in missing)
        )
    if mismatched:
        parts.append(
            "segment_name 与 te_endpoint 不一致（核验会失败）: "
            + "; ".join(mismatched)
        )
    parts.append(
        "请把 --instance-segs 写成与传输端点相同的字符串"
        "（正确姿势：挂段名 = ip:rpc_port = te_endpoint）"
    )
    raise AdminMainError("; ".join(parts))


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

        按请求行长度分批。单批 ``GET {path} HTTP/1.1`` 不超过 8KB
        （见模块常量注释），各批结果合并后返回。一把 key 自己就超过
        上限时仍单独发出（切不开）。空列表报错。
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

    def get_segments_detail(self) -> tuple[SegmentDetail, ...]:
        """``GET /get_segments_detail``：段名与 ``te_endpoint`` 等明细。"""
        return parse_segments_detail(self._get("/get_segments_detail"))

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
        address = desc.get("buffer_address_")
        views.append(
            MemoryReplicaView(
                transport_endpoint=endpoint,
                size=int(size) if isinstance(size, int) and not isinstance(size, bool) else None,
                protocol=protocol if isinstance(protocol, str) else None,
                buffer_address=(
                    int(address)
                    if isinstance(address, int) and not isinstance(address, bool)
                    else None
                ),
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
