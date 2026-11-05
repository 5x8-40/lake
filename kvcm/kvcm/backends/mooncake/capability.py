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
            "标记创建时固化；pin(HARD) 拒绝，要 lease 保护请显式调 register_keepalive()。"
            "lease 时长是 master 的 default_kv_lease_ttl，kvcm 不改这个配置",
        ),
        can_copy_with_source=Capability(Support.NO, Support.NO, "move 才能指定源"),
        has_offload=Capability(
            Support.NO,
            Support.NO,
            "0.3.11 的 /batch_query_keys 只有内存副本，没有 disk_values / "
            "local_disk_values（主线 #2685 才加）。按它查盘会判定成没有盘副本。"
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
        has_local_segment=Capability(
            Support.YES, Support.YES, "setup(global_segment_size) 启动时挂段"
        ),
        has_local_disk_mount=Capability(
            Support.YES, Support.YES, "setup(enable_ssd_offload, ssd_offload_path) 启动时挂盘"
        ),
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
    """本 PR 目标版本的能力集（代码沿用 main 名字；版本基线见文档）。"""

    return BackendCapability(
        has_lease=Capability(
            Support.YES,
            Support.YES,
            "通道 = is_exist，读即续租（预热前置检查的 exists 调用同样续租——保活免费搭车）",
        ),
        soft_pin_after=Capability(
            Support.NO,
            Support.NO,
            "soft pin 未接线；native 空写见后续 soft pin PR",
        ),
        hard_pin_after=Capability(
            Support.NO,
            Support.NO,
            "标记创建时固化；pin(HARD) 拒绝，要 lease 保护请显式调 register_keepalive()。"
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
            "打 GetReplicaList（默认 2）触发；check_promote 用内存副本传输端点对 "
            "locate(target)。offload 端点与 seg 不是一套；无实例→client_id 时触发前 "
            "证不了落点（R05）。L3 单次核验，轮询在 PromoteFlow；不做读删写",
        ),
        has_per_key_demote=Capability(Support.NO, Support.NO, "无确定性立即降层"),
        has_task_cancel=Capability(Support.NO, Support.NO, "后端无任务取消能力"),
        has_local_segment=Capability(
            Support.YES, Support.YES, "同 0.3.11"
        ),
        has_local_disk_mount=Capability(
            Support.YES, Support.YES, "同 0.3.11"
        ),
        has_dfs_replica=Capability(Support.YES, Support.UNKNOWN, "写入路径待核对"),
        has_admin_query=Capability(
            Support.YES,
            Support.YES,
            "副本核验走 HTTP GET /batch_query_keys，用传输端点比段还在不在。"
            "提回用 GetReplicaList + 内存副本传输端点；不用 offload 端点对 seg",
        ),
        has_tenant_quota=Capability(
            Support.YES,
            Support.YES,
            "HTTP GET /api/v1/tenant_quotas 读租户配额"
            "（requested/effective/charged）；不是段剩余空间，不能拿来当段剩余容量",
        ),
    )
