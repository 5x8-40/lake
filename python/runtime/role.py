"""进程级角色配置（D3 最小子集；完整 schema 待补）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class WorkerRole(str, Enum):
    PREFILL = "prefill"
    DECODE = "decode"
    HYBRID = "hybrid"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass
class RoleConfig:
    role: WorkerRole = WorkerRole.HYBRID
    enable_drafter: bool = False
    num_draft_tokens: int = 2  # C4：MTP 宽度
    enable_overlap: bool = True  # 默认开；对齐 SGLang event_loop_overlap
    max_running_reqs: int = 8  # continuous batching 上限（C1）
    # C7：对齐 vLLM Scheduler.token_budget / long_prefill_token_threshold
    max_num_scheduled_tokens: int = 8192  # 本步总 token 上限；必须 >0
    # >0 时 prompt 残差按此切块；0=只受 max_num_scheduled_tokens 约束
    long_prefill_token_threshold: int = 0
    # >0 时 admission：len(prompt)+max_new 不得超过；0=不限制（骨架默认）
    max_model_length: int = 0
    # D5：prepare 补拉预算；0=同步等到齐（P3 mock）
    pull_budget_ms: int = 0
    allow_partial_hit: bool = False  # False=缺块整批失败（all-or-nothing）
    # C3：mock=P3 可复现递推；tiny_lm=纯 Python 最小因果 LM
    model_backend: str = "mock"  # mock | tiny_lm
    # arena / TP / 指标标签等留 D3

    def __post_init__(self) -> None:
        if self.max_num_scheduled_tokens <= 0:
            raise ValueError(
                f"max_num_scheduled_tokens must be > 0, got {self.max_num_scheduled_tokens}"
            )
        if self.max_running_reqs <= 0:
            raise ValueError(f"max_running_reqs must be > 0, got {self.max_running_reqs}")

    @classmethod
    def from_env(cls) -> "RoleConfig":
        """C6/D3 + C7：从环境变量读启动配置。

        LAKE_WORKER_ROLE=prefill|decode|hybrid
        LAKE_MODEL_BACKEND=mock|tiny_lm
        LAKE_ENABLE_DRAFTER / LAKE_ENABLE_OVERLAP / LAKE_ALLOW_PARTIAL_HIT
        LAKE_NUM_DRAFT_TOKENS / LAKE_MAX_RUNNING_REQS / LAKE_PULL_BUDGET_MS
        LAKE_MAX_NUM_SCHEDULED_TOKENS / LAKE_LONG_PREFILL_TOKEN_THRESHOLD
        LAKE_MAX_MODEL_LENGTH
        """
        role_raw = os.environ.get("LAKE_WORKER_ROLE", "hybrid").strip().lower()
        try:
            role = WorkerRole(role_raw)
        except ValueError:
            role = WorkerRole.HYBRID
        backend = os.environ.get("LAKE_MODEL_BACKEND", "mock").strip().lower() or "mock"
        return cls(
            role=role,
            model_backend=backend,
            enable_drafter=_env_bool("LAKE_ENABLE_DRAFTER", False),
            enable_overlap=_env_bool("LAKE_ENABLE_OVERLAP", True),
            allow_partial_hit=_env_bool("LAKE_ALLOW_PARTIAL_HIT", False),
            num_draft_tokens=_env_int("LAKE_NUM_DRAFT_TOKENS", 2),
            max_running_reqs=_env_int("LAKE_MAX_RUNNING_REQS", 8),
            max_num_scheduled_tokens=_env_int("LAKE_MAX_NUM_SCHEDULED_TOKENS", 8192),
            long_prefill_token_threshold=_env_int("LAKE_LONG_PREFILL_TOKEN_THRESHOLD", 0),
            max_model_length=_env_int("LAKE_MAX_MODEL_LENGTH", 0),
            pull_budget_ms=_env_int("LAKE_PULL_BUDGET_MS", 0),
        )