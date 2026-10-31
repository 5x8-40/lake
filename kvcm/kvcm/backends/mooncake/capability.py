"""mooncake 各版本的能力集填充。

通道 = Python 绑定（store_py）∪ HTTP 管理端点。目标基线 = 最新主线；
0.3.11 仅为当前部署版本的能力记录，部署升级后删除。
"""

from __future__ import annotations

from kvcm.ops.capability import BackendCapability, Capability, Support


def mooncake_v0311() -> BackendCapability:
    """mooncake 0.3.11（当前部署版本）。"""

    return BackendCapability(
        has_lease=Capability(
            Support.YES,
            Support.YES,
            "通道 = is_exist，读即续租（预热前置检查的 exists 调用同样续租——保活免费搭车）",
        ),
        soft_pin_after=Capability(
            Support.YES,
            Support.NO,
            "空写扩展对着主线 SoftPinAction。这个能力集不把通道标成已有",
        ),
        hard_pin_after=Capability(
            Support.NO,
            Support.NO,
            "标记创建时固化；pin(HARD) 的控制效果是登记保活，不是设置这个位。"
            "lease 时长是 master 的 default_kv_lease_ttl，kvcm 不改这个配置",
        ),
        can_copy_with_source=Capability(Support.NO, Support.NO, "move 才能指定源"),
        has_offload=Capability(
            Support.NO,
            Support.NO,
            "0.3.11 的 /batch_query_keys 只有内存副本，没有 disk_values / "
            "local_disk_values（主线 #2685 才加）。按它查盘会定判成没有盘副本。"
            "通道不具备，demote 在停保活前拒绝",
        ),
        has_promotion=Capability(
            Support.YES,
            Support.YES,
            "打 GetReplicaList 累计命中，再用内存副本传输端点对 locate(target)。"
            "不拿 offload 端点对 seg 名：两套端口不同",
        ),
        has_per_key_demote=Capability(Support.NO, Support.NO, "无确定性立即降层"),
        has_task_cancel=Capability(Support.NO, Support.NO, "后端无任务取消能力"),
        has_retained_client=Capability(Support.NO, Support.NO, "本版本无此接口"),
        has_dfs_replica=Capability(Support.NO, Support.NO, "本版本无此接口"),
        has_admin_query=Capability(
            Support.NO, Support.NO, "HTTP 查询有副作用（续租 + 计命中）"
        ),
        has_tenant_quota=Capability(
            Support.NO,
            Support.NO,
            "0.3.11 无租户配额接口；不是段剩余空间",
        ),
    )


def mooncake_main() -> BackendCapability:
    """mooncake 最新主线（目标基线）。"""

    return BackendCapability(
        has_lease=Capability(
            Support.YES,
            Support.YES,
            "通道 = is_exist，读即续租（预热前置检查的 exists 调用同样续租——保活免费搭车）",
        ),
        soft_pin_after=Capability(
            Support.YES,
            Support.YES,
            "空写：_get_pyclient_capsule 拿回已建 client，"
            "扩展调 MasterClient::UpsertStart/UpsertEnd，不 TransferWrite，"
            "不第二次 RealClient::create",
        ),
        hard_pin_after=Capability(
            Support.NO,
            Support.NO,
            "标记创建时固化；pin(HARD) 的控制效果是登记保活，不是设置这个位。"
            "lease 时长是 master 的 default_kv_lease_ttl，kvcm 不改这个配置",
        ),
        can_copy_with_source=Capability(Support.NO, Support.NO, "move 才能指定源"),
        has_offload=Capability(
            Support.YES,
            Support.YES,
            "主线：停保活、等满 lease TTL、再用 GET /batch_query_keys 查盘副本；没有就失败",
        ),
        has_promotion=Capability(
            Support.YES,
            Support.YES,
            "打 GetReplicaList，默认 2 次，再用内存副本传输端点对 locate(target)。"
            "不能在打次数之前证明 target 是盘副本登记实例："
            "offload 端点和 seg 名不是同一套。同步核验，不轮询，不做读删写",
        ),
        has_per_key_demote=Capability(Support.NO, Support.NO, "无确定性立即降层"),
        has_task_cancel=Capability(Support.NO, Support.NO, "后端无任务取消能力"),
        has_retained_client=Capability(
            Support.NO,
            Support.NO,
            "这份主线树没有 AddReplicaForRetainedClient。dummy_client.h 的 kRetained 是外部 buffer 引用计数。先不做",
        ),
        has_dfs_replica=Capability(Support.YES, Support.UNKNOWN, "写入路径待核对"),
        has_admin_query=Capability(
            Support.YES,
            Support.YES,
            "副本核验走 HTTP GET /batch_query_keys，用传输端点比段还在不在。"
            "提回删掉了用 offload 端点对 seg 名的检查，仍打 GetReplicaList",
        ),
        has_tenant_quota=Capability(
            Support.YES,
            Support.YES,
            "HTTP GET /api/v1/tenant_quotas 读租户配额"
            "（requested/effective/charged）；不是段剩余空间，不能拿来当段剩余容量",
        ),
    )
