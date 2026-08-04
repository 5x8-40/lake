"""C15 runtime executor 草案。

单卡路径先实现为 `SingleProcessExecutor`；未来 TP/PP 时在同一接口下把
一份 `SchedulerOutput` 扇出到多个 worker，不把 Host Req 权威复制进 runner。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from lake.engine.model_runner import ModelRunner, ModelRunnerOutput
from lake.engine.pool_types import ReadyHandle
from lake.runtime.req import Req
from lake.runtime.scheduler_output import SchedulerOutput


@dataclass(frozen=True)
class ExecutorInput:
    output: SchedulerOutput
    ready: ReadyHandle
    host_reqs: Mapping[str, Req]


class RuntimeExecutor(Protocol):
    def execute_model(self, inp: ExecutorInput) -> ModelRunnerOutput:
        """执行一份调度决策；多卡实现会在这里扇出同一个 SchedulerOutput。"""

        ...


class SingleProcessExecutor:
    """单进程 / 单 runner executor，占位 vLLM Executor.collective_rpc 边界。"""

    def __init__(self, runner: ModelRunner) -> None:
        self._runner = runner

    def execute_model(self, inp: ExecutorInput) -> ModelRunnerOutput:
        return self._runner.execute_model(inp.output, inp.ready, inp.host_reqs)
