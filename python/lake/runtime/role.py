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
    enable_drafter: bool = False  # 保留给后续真实 draft/spec 模型；当前未接入
    num_draft_tokens: int = 2  # C4：MTP 宽度（当前未接入）
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
    # qwen3=Qwen3 load/forward 骨架；mock 仅测试用
    model_backend: str = "qwen3"  # qwen3 | mock
    # C12：模型加载 / warmup 骨架。model_path 是加载源，不作为 API 名称暴露。
    model_path: str = ""
    served_model_name: str = "model"
    model_revision: str = ""
    warmup_num_reqs: int = 1
    warmup_tokens_per_req: int = 1
    # arena / TP / 指标标签等留 D3

    def __post_init__(self) -> None:
        if self.max_num_scheduled_tokens <= 0:
            raise ValueError(
                f"max_num_scheduled_tokens must be > 0, got {self.max_num_scheduled_tokens}"
            )
        if self.max_running_reqs <= 0:
            raise ValueError(f"max_running_reqs must be > 0, got {self.max_running_reqs}")
        if self.warmup_num_reqs <= 0:
            raise ValueError(f"warmup_num_reqs must be > 0, got {self.warmup_num_reqs}")
        if self.warmup_tokens_per_req <= 0:
            raise ValueError(
                f"warmup_tokens_per_req must be > 0, got {self.warmup_tokens_per_req}"
            )

    @classmethod
    def from_env(cls) -> "RoleConfig":
        """C6/D3 + C7：从环境变量读启动配置。

        LAKE_WORKER_ROLE=prefill|decode|hybrid
        LAKE_MODEL_BACKEND=qwen3|mock
        LAKE_ENABLE_DRAFTER / LAKE_ENABLE_OVERLAP / LAKE_ALLOW_PARTIAL_HIT
        LAKE_NUM_DRAFT_TOKENS / LAKE_MAX_RUNNING_REQS / LAKE_PULL_BUDGET_MS
        LAKE_MAX_NUM_SCHEDULED_TOKENS / LAKE_LONG_PREFILL_TOKEN_THRESHOLD
        LAKE_MAX_MODEL_LENGTH
        LAKE_MODEL_PATH / LAKE_SERVED_MODEL_NAME / LAKE_MODEL_REVISION
        LAKE_WARMUP_NUM_REQS / LAKE_WARMUP_TOKENS_PER_REQ
        """
        role_raw = os.environ.get("LAKE_WORKER_ROLE", "hybrid").strip().lower()
        try:
            role = WorkerRole(role_raw)
        except ValueError:
            role = WorkerRole.HYBRID
        backend = os.environ.get("LAKE_MODEL_BACKEND", "qwen3").strip().lower()
        backend = backend or "qwen3"
        return cls(
            role=role,
            model_backend=backend,
            model_path=os.environ.get("LAKE_MODEL_PATH", "").strip(),
            served_model_name=os.environ.get("LAKE_SERVED_MODEL_NAME", "model").strip()
            or "model",
            model_revision=os.environ.get("LAKE_MODEL_REVISION", "").strip(),
            enable_drafter=_env_bool("LAKE_ENABLE_DRAFTER", False),
            enable_overlap=_env_bool("LAKE_ENABLE_OVERLAP", True),
            allow_partial_hit=_env_bool("LAKE_ALLOW_PARTIAL_HIT", False),
            num_draft_tokens=_env_int("LAKE_NUM_DRAFT_TOKENS", 2),
            max_running_reqs=_env_int("LAKE_MAX_RUNNING_REQS", 8),
            max_num_scheduled_tokens=_env_int("LAKE_MAX_NUM_SCHEDULED_TOKENS", 8192),
            long_prefill_token_threshold=_env_int("LAKE_LONG_PREFILL_TOKEN_THRESHOLD", 0),
            max_model_length=_env_int("LAKE_MAX_MODEL_LENGTH", 0),
            pull_budget_ms=_env_int("LAKE_PULL_BUDGET_MS", 0),
            warmup_num_reqs=_env_int("LAKE_WARMUP_NUM_REQS", 1),
            warmup_tokens_per_req=_env_int("LAKE_WARMUP_TOKENS_PER_REQ", 1),
        )