"""StorageAgent 协议 —— Python↔Rust agent FFI 的 Python 侧草签（D2）。

不进 protobuf（FFI / PyO3）。生产实现：`lake-storage-agent` `.so`。
参考:vLLM `KVConnectorBase_V1`（start_load_kv / wait_for_save / request_finished）；
关键差异：必经路径、表组装归 agent、无引擎权威 block_ids。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lake.engine.pool_types import FinishRequest, PreparePlan, ReadyHandle


@runtime_checkable
class StorageAgent(Protocol):
    def prepare_step(self, plan: PreparePlan) -> ReadyHandle:
        """ready fence：保证 read 在 L0、分配 write slot、冻结、组表。

        超时 / 容量不足抛 `PoolError`。
        """
        ...

    def done(self, step_id: int) -> None:
        """done fence：解冻 / 满块写回触发 / 允许与下一 prepare 交错时延迟 free。"""
        ...

    def on_request_finished(self, finish: FinishRequest) -> None:
        """请求结束唯一 KV 收尾（尾块路 + 屏障 + 本地 ref--）。

        overlap 下可延迟到无 in-flight 引用后再归还槽（类 SGLang free_group）。
        """
        ...

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        """回收本步未实际产出的 write extent。

        TARGET_VERIFY / stop 等路径可能预留多于实际接受 token；生产 agent 以
        device-side 会计为准，不能用旧 host commit 压掉更新的 prepare 高水位。
        """
        ...
