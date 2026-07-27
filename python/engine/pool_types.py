"""pool_iface / StorageAgent 共享类型与错误码（D2）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from runtime.scheduler_output import ForwardMode, ReqIoSet


class PoolErrorCode(str, Enum):
    OK = "OK"
    TIMEOUT = "TIMEOUT"  # 补拉超过 pull_budget_ms
    CAPACITY = "CAPACITY"  # 硬配额 / 无空闲 L0 slot
    PROTOCOL_ERROR = "PROTOCOL_ERROR"  # ready/done fence 错配或重入 prepare
    DOWNSTREAM = "DOWNSTREAM"  # 控制面 / KV 后端失败
    INVALID_ARG = "INVALID_ARG"


class PoolError(Exception):
    def __init__(self, code: PoolErrorCode, message: str = "") -> None:
        self.code = code
        super().__init__(message or code.value)

    def __str__(self) -> str:
        return f"{self.code.value}: {super().__str__()}"


@dataclass
class StepStats:
    reused_blocks: int = 0
    prefill_blocks: int = 0
    pulled_blocks: int = 0  # 本步补拉块数（本地未命中）


@dataclass
class ReadyHandle:
    """ready fence 完成信号。

    C2：无 device 指针；生产由 agent 填固定地址 block table 后只回 step_id + 统计。
    C8/D4：`block_table_by_req` 为逻辑 slot 表占位（引擎只读，不组装）。
    """

    step_id: int
    stats_by_req: Dict[str, StepStats] = field(default_factory=dict)
    # 缩批语义：None = agent 未填（FakePool/旧 agent）→ 未缩批，按原 plan 执行；
    # [] = agent 显式缩批至空（allow_partial_hit 把全批丢掉）→ 调度器须降为 IDLE。
    # 非 None 时为缩批后的实际集合（默认与 plan 相同）。
    effective_read_set: Optional[List[ReqIoSet]] = None
    effective_write_set: Optional[List[ReqIoSet]] = None
    # D4：agent 出表；key=req_id → block slot 索引列表（逻辑，非物理指针）
    block_table_by_req: Dict[str, List[int]] = field(default_factory=dict)


@dataclass
class PreparePlan:
    """node_scheduler → agent 的一步准备计划（D2/D5）。

    不含物理 block_ids；agent 据视图 + arena 填表。
    """

    step_id: int
    forward_mode: ForwardMode
    read_set: List[ReqIoSet]
    write_set: List[ReqIoSet]
    num_scheduled_tokens: Dict[str, int]
    # D5
    pull_budget_ms: int = 0  # 0 = 同步等到齐（mock 默认）；>0 超时 → TIMEOUT 或 partial
    allow_partial_hit: bool = False  # False：缺块不可进批（all-or-nothing）


@dataclass
class FinishRequest:
    req_id: str
    node_id: str
    model_id: str = ""
