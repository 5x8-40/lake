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
            "绑定只有一步式 upsert_tensor，HTTP 无写类端点；自封装 .so 候选",
        ),
        hard_pin_after=Capability(Support.NO, Support.NO, "pin 写入时固化、无 unpin"),
        can_copy_with_source=Capability(Support.NO, Support.NO, "move 才能指定源"),
        has_offload=Capability(
            Support.YES, Support.YES, "降层 = 停保活即可，无需调后端"
        ),
        has_promotion=Capability(
            Support.YES,
            Support.YES,
            "提回 = HTTP /query_key，其续租副作用即提回语义",
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
            Support.YES, Support.NO, "同 0.3.11；自封装 .so 候选"
        ),
        hard_pin_after=Capability(Support.NO, Support.NO, "pin 写入时固化、无 unpin"),
        can_copy_with_source=Capability(Support.NO, Support.NO, "move 才能指定源"),
        has_offload=Capability(Support.YES, Support.YES, "同 0.3.11"),
        has_promotion=Capability(Support.YES, Support.YES, "同 0.3.11"),
        has_per_key_demote=Capability(Support.NO, Support.NO, "无确定性立即降层"),
        has_task_cancel=Capability(Support.NO, Support.NO, "后端无任务取消能力"),
        has_retained_client=Capability(
            Support.YES,
            Support.NO,
            "Python 绑定与 HTTP 均无入口；自封装 .so 候选；替代 = move 到存活节点",
        ),
        has_dfs_replica=Capability(Support.YES, Support.UNKNOWN, "写入路径待核对"),
        has_admin_query=Capability(
            Support.YES,
            Support.YES,
            "通道记录的是主线有 HTTP GET /batch_query_keys 这个端点，"
            "本次不把核验调用换过去；换包之后也不会自动接上",
        ),
        has_tenant_quota=Capability(
            Support.YES,
            Support.YES,
            "HTTP GET /api/v1/tenant_quotas 读租户配额"
            "（requested/effective/charged）；不是段剩余空间，不能拿来当段剩余容量",
        ),
    )
