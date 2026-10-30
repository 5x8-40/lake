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


def test_capability_records_endpoint_without_switching_verify() -> None:
    main = mooncake_main()
    note = main.has_admin_query.note
    assert main.has_admin_query.channel is Support.YES
    assert "不把核验调用换过去" in note
    assert "不会自动接上" in note
    assert main.has_tenant_quota.channel is Support.YES
    assert "段剩余" in main.has_tenant_quota.note

    deployed = mooncake_v0311()
    assert deployed.has_tenant_quota.channel is Support.NO
    assert deployed.has_tenant_quota.rpc is Support.NO
    assert deployed.has_admin_query.channel is Support.NO
