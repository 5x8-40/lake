"""主线管理端点的解析：假响应，不打真实 master。"""

from __future__ import annotations

from dataclasses import fields

import pytest

from kvcm.backends.mooncake.admin_main import (
    AdminMainError,
    MainlineAdmin,
    parse_batch_query_keys,
    parse_tenant_quotas,
)
from kvcm.backends.mooncake.capability import mooncake_main, mooncake_v0311
from kvcm.ops.capability import Support

_BATCH = """
{
  "success": true,
  "data": {
    "k-hot": {
      "ok": true,
      "values": [
        {
          "size_": 4096,
          "buffer_address_": 1,
          "protocol_": "tcp",
          "transport_endpoint_": "10.0.0.2:5000"
        }
      ]
    },
    "k-miss": {
      "ok": false,
      "error": "OBJECT_NOT_FOUND"
    }
  }
}
"""

_QUOTAS = """
{
  "success": true,
  "data": [
    {
      "tenant_id": "tenant-a",
      "requested_quota_bytes": 800,
      "effective_quota_bytes": 800,
      "charged_bytes": 100,
      "admission_closed": false,
      "over_quota": false,
      "has_explicit_policy": true
    }
  ]
}
"""

_ONE = """
{
  "success": true,
  "data": {
    "tenant_id": "tenant-a",
    "requested_quota_bytes": 800,
    "effective_quota_bytes": 700,
    "charged_bytes": 100,
    "admission_closed": false,
    "over_quota": false,
    "has_explicit_policy": true
  }
}
"""


def test_parse_batch_query_keys_memory_endpoint_and_miss() -> None:
    parsed = parse_batch_query_keys(_BATCH)
    hot = parsed["k-hot"]
    assert hot.ok is True
    assert hot.memory[0].transport_endpoint == "10.0.0.2:5000"
    assert hot.memory[0].size == 4096
    assert hot.memory[0].protocol == "tcp"
    miss = parsed["k-miss"]
    assert miss.ok is False
    assert miss.error == "OBJECT_NOT_FOUND"
    assert miss.memory == ()
    assert hot.disk_replicas == 0
    with_disk = parse_batch_query_keys(
        '{"success": true, "data": {"k": {"ok": true, "values": [],'
        ' "local_disk_values": [{"client_id": "c", "object_size": 1,'
        ' "transport_endpoint": "10.0.0.1:1"}]}}}'
    )
    assert with_disk["k"].disk_replicas == 1
    assert with_disk["k"].memory == ()


def test_parse_tenant_quotas_list_and_single() -> None:
    rows = parse_tenant_quotas(_QUOTAS)
    assert len(rows) == 1
    snap = rows[0]
    assert snap.tenant_id == "tenant-a"
    assert snap.requested_quota_bytes == 800
    assert snap.effective_quota_bytes == 800
    assert snap.charged_bytes == 100
    assert snap.admission_closed is False
    assert snap.has_explicit_policy is True
    # 配额记账，不是段剩余空间
    names = {f.name for f in fields(type(snap))}
    assert "charged_bytes" in names
    assert not any("segment" in name or "free" in name for name in names)

    one = parse_tenant_quotas(_ONE)
    assert one[0].effective_quota_bytes == 700
    assert one[0].charged_bytes == 100


def test_parse_rejects_unsuccessful_and_incomplete() -> None:
    with pytest.raises(AdminMainError, match="not JSON"):
        parse_batch_query_keys("not-json")
    with pytest.raises(AdminMainError, match="success"):
        parse_tenant_quotas('{"success": false, "data": []}')
    with pytest.raises(AdminMainError, match="missing"):
        parse_tenant_quotas(
            '{"success": true, "data": [{"tenant_id": "t"}]}'
        )


def test_mainline_admin_paths_with_fake_body() -> None:
    seen: list[str] = []

    def fetch(url: str) -> str:
        seen.append(url)
        if "batch_query_keys" in url:
            return _BATCH
        return _ONE

    admin = MainlineAdmin("http://127.0.0.1:9003/", fetcher=fetch)
    queried = admin.batch_query_keys(["k-hot", "a/b"])
    assert queried["k-hot"].ok is True
    assert seen[0] == (
        "http://127.0.0.1:9003/batch_query_keys?keys=k-hot,a%2Fb"
    )
    quotas = admin.get_tenant_quotas("tenant-a")
    assert quotas[0].tenant_id == "tenant-a"
    assert seen[1] == (
        "http://127.0.0.1:9003/api/v1/tenant_quotas?tenant_id=tenant-a"
    )
    admin.get_tenant_quotas()
    assert seen[2] == "http://127.0.0.1:9003/api/v1/tenant_quotas"


def test_batch_query_keys_splits_long_request_line() -> None:
    """长前缀按请求行 8KB 分批，结果合并。空 key 仍报错。"""
    import json
    from urllib.parse import parse_qs, urlparse

    from kvcm.backends.mooncake.admin_main import _MAX_BATCH_QUERY_REQUEST_BYTES

    seen: list[str] = []

    def fetch(url: str) -> str:
        seen.append(url)
        raw = parse_qs(urlparse(url).query)["keys"][0]
        data = {key: {"ok": True, "values": []} for key in raw.split(",")}
        return json.dumps({"success": True, "data": data})

    keys = [f"k{i:04d}" + ("x" * 400) for i in range(30)]
    admin = MainlineAdmin("http://127.0.0.1:9003", fetcher=fetch)
    queried = admin.batch_query_keys(keys)
    assert len(seen) >= 2
    assert set(queried) == set(keys)
    base = "http://127.0.0.1:9003"
    for url in seen:
        path = url[len(base):]
        line = len(b"GET ") + len(path.encode()) + len(b" HTTP/1.1")
        assert line <= _MAX_BATCH_QUERY_REQUEST_BYTES, line
    with pytest.raises(AdminMainError, match="at least one"):
        admin.batch_query_keys([])


def test_capability_verify_uses_batch_query_keys() -> None:
    main = mooncake_main()
    note = main.has_admin_query.note
    assert main.has_admin_query.channel is Support.YES
    assert "/batch_query_keys" in note
    assert "GetReplicaList" in note
    assert main.has_tenant_quota.channel is Support.YES
    assert "段剩余" in main.has_tenant_quota.note

    deployed = mooncake_v0311()
    assert deployed.has_tenant_quota.channel is Support.NO
    assert deployed.has_tenant_quota.rpc is Support.NO
    assert deployed.has_admin_query.channel is Support.NO
