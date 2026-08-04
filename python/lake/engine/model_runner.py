"""薄 ModelRunner：consume ready → prepare → forward → sample。

对齐 vLLM `GPUModelRunner.execute_model`：统一入口，按本步 token 几何执行
（`num_scheduled_tokens` / `req_num_computed_at_schedule`），不按 SGLang 分相状态机。
C8：`prepare_inputs` / `prepare_attn` / `sample_tokens` 拆步；残差 query 路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from lake.engine.model_executor.layers.attentions import AttentionMetadata, build_attn_metadata
from lake.engine.input_batch import InputBatch, InputBuffers
from lake.engine.model_executor.models.registry import load_registered_model
from lake.engine.pool_iface import PoolIface
from lake.engine.pool_types import ReadyHandle
from lake.engine.sample.greedy import greedy_sample
from lake.engine.sample.grammar import apply_token_bitmask
from lake.runtime.req import Req
from lake.runtime.scheduler_output import ForwardMode, SamplingParams, SchedulerOutput

@dataclass
class ModelRunnerOutput:
    step_id: int
    next_token_ids: Dict[str, List[int]] = field(default_factory=dict)
    next_draft_tokens: Dict[str, List[int]] = field(default_factory=dict)
    model_backend: str = "qwen3"


@dataclass(frozen=True)
class ModelLoadInfo:
    model_path: str
    served_model_name: str
    revision: str
    backend: str
    load_format: str = "dummy"
    load_dummy_weights: bool = False
    weight_pinned: bool = False


@dataclass(frozen=True)
class ModelRunnerStatus:
    model_path: str
    served_model_name: str
    revision: str
    backend: str
    loaded: bool
    warmed: bool


class ModelRunner:
    def __init__(
        self,
        pool: PoolIface,
        *,
        model_backend: str = "qwen3",
        model_config: Optional[Any] = None,
        weight_pin_callback: Optional[Callable[[ModelLoadInfo], None]] = None,
    ) -> None:
        self._pool = pool
        self._input_batch = InputBatch()
        self._input_buffers = InputBuffers(max_num_reqs=64, max_num_tokens=8192)
        self._attn_meta: Optional[AttentionMetadata] = None
        self.model_backend = model_backend
        self._config_override = model_config
        self._model: Optional[Any] = None
        self._weight_pin_callback = weight_pin_callback
        self._model_path = ""
        self._served_model_name = "model"
        self._model_revision = ""
        self._model_loaded = False
        self._model_warmed = False

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    @property
    def model_warmed(self) -> bool:
        return self._model_warmed

    @property
    def model_path(self) -> str:
        return self._model_path

    @property
    def served_model_name(self) -> str:
        return self._served_model_name

    def status(self) -> ModelRunnerStatus:
        return ModelRunnerStatus(
            model_path=self._model_path,
            served_model_name=self._served_model_name,
            revision=self._model_revision,
            backend=self.model_backend,
            loaded=self._model_loaded,
            warmed=self._model_warmed,
        )

    def load_model(
        self,
        *,
        model_path: str = "",
        served_model_name: str = "model",
        revision: str = "",
        load_format: str = "dummy",
        load_dummy_weights: bool = False,
        pin_weights: bool = True,
    ) -> ModelLoadInfo:
        """C12：真实模型加载骨架。

        对齐 vLLM `GPUModelRunner.load_model` 的阶段边界：先建立模型对象，
        再初始化依赖模型的执行组件。lake 只记录状态并触发权重 pin 回调；
        权重所有权仍归存储池。
        """

        if self.model_backend == "mock":
            self._model = None
            self._model_path = model_path
            self._served_model_name = served_model_name or "model"
            self._model_revision = revision
            self._model_loaded = True
            self._model_warmed = False
            return ModelLoadInfo(
                model_path=self._model_path,
                served_model_name=self._served_model_name,
                revision=self._model_revision,
                backend=self.model_backend,
                load_format=load_format,
                load_dummy_weights=load_dummy_weights,
                weight_pinned=pin_weights,
            )

        loaded = load_registered_model(
            backend=self.model_backend,
            model_path=model_path,
            revision=revision,
            load_format=load_format,
            config_override=self._config_override,
        )
        self._model = loaded.model
        self._model_path = loaded.model_path
        self._served_model_name = served_model_name or "model"
        self._model_revision = loaded.revision
        self._model_loaded = True
        self._model_warmed = False
        info = ModelLoadInfo(
            model_path=self._model_path,
            served_model_name=self._served_model_name,
            revision=self._model_revision,
            backend=self.model_backend,
            load_format=loaded.load_format,
            load_dummy_weights=load_dummy_weights or loaded.load_dummy_weights,
            weight_pinned=pin_weights,
        )
        if pin_weights and self._weight_pin_callback is not None:
            self._weight_pin_callback(info)
        return info

    def warmup(
        self,
        *,
        num_reqs: int = 1,
        tokens_per_req: int = 1,
    ) -> ModelRunnerOutput:
        """C12：warmup 复用生产 dummy 入口，但跳过 pool.done。"""

        if not self._model_loaded:
            self.load_model(
                model_path=self._model_path,
                served_model_name=self._served_model_name,
            )
        out = self.dummy_run(
            num_reqs=num_reqs,
            tokens_per_req=tokens_per_req,
            step_id=-1,
        )
        self._model_warmed = True
        return out

    def prepare_inputs(
        self,
        output: SchedulerOutput,
        host_reqs: Mapping[str, Req],
    ) -> InputBatch:
        """对齐 vLLM `prepare_inputs`：组本步 InputBatch（无跨步 RequestState）。"""
        batch = InputBatch()
        spec_map = output.scheduled_spec_decode_tokens or {}
        for req_id, n in output.num_scheduled_tokens.items():
            if n <= 0:
                continue
            req = host_reqs.get(req_id)
            if req is None:
                continue
            prompt_len = len(req.prompt_token_ids)
            computed = output.req_num_computed_at_schedule.get(req_id, req.num_computed_tokens)
            query_start = output.req_query_start.get(req_id)
            query_end = output.req_query_end.get(req_id)
            batch.req_ids.append(req_id)
            batch.num_scheduled_tokens[req_id] = n
            batch.num_computed_tokens[req_id] = computed
            if computed < prompt_len:
                start = computed if query_start is None else query_start
                end = min(prompt_len, query_end if query_end is not None else computed + n)
                batch.token_ids[req_id] = list(req.prompt_token_ids[:end])
                batch.query_start[req_id] = start
                batch.query_end[req_id] = end
                batch.is_prompt_phase[req_id] = True
            else:
                ctx = list(req.all_token_ids)
                draft = list(spec_map.get(req_id) or [])[: max(0, n - 1)]
                qs = query_start if query_start is not None else max(0, len(ctx) - 1)
                qe = query_end if query_end is not None else max(qs, qs + n)
                if draft:
                    # TARGET_VERIFY：输入 last_ctx + draft，产生 draft 校验 + bonus。
                    tokens = ctx + draft
                else:
                    tokens = ctx
                # overlap 下 scheduler 的 query 几何可能已包含 device-side inflight token；
                # Python 骨架尚无真实 device token 接力，用最后已知 token 占位以保持位置/slot 几何。
                if len(tokens) < qe:
                    pad = tokens[-1] if tokens else 0
                    tokens.extend([pad] * (qe - len(tokens)))
                batch.token_ids[req_id] = tokens[:qe]
                batch.query_start[req_id] = qs
                batch.query_end[req_id] = qe
                batch.is_prompt_phase[req_id] = False
        self._input_batch = batch
        return batch

    def prepare_attn(self, batch: InputBatch, ready: ReadyHandle) -> AttentionMetadata:
        """对齐 vLLM `prepare_attn`：几何 + agent block table（只读）。"""
        seq_lens = {}
        for rid in batch.req_ids:
            seq_lens[rid] = batch.query_end[rid]
        self._input_buffers.materialize(
            batch, slot_mapping_by_req=ready.slot_mapping_by_req
        )
        meta = build_attn_metadata(
            seq_lens=seq_lens,
            query_start=batch.query_start,
            query_end=batch.query_end,
            block_tables=ready.block_table_by_req,
            buffers=self._input_buffers,
            req_order=batch.req_ids,
        )
        self._attn_meta = meta
        return meta

    def sample_tokens(
        self,
        output: SchedulerOutput,
        host_reqs: Mapping[str, Req],
        last_logits: Dict[str, List[float]],
    ) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
        """从本步 logits 采样；接口预留与 `execute_model` 拆分（对齐 V2）。"""
        out: Dict[str, List[int]] = {}
        drafts_out: Dict[str, List[int]] = {}
        if output.scheduled_spec_decode_tokens:
            raise NotImplementedError("speculative verification requires a draft model")
        grammar = output.grammar_output
        deferred = set(grammar.deferred_req_ids if grammar is not None else [])
        bitmasks = grammar.token_bitmask_by_req if grammar is not None else {}
        for req_id, logits in last_logits.items():
            if req_id in deferred:
                continue
            req = host_reqs.get(req_id)
            if req is None:
                continue
            masked_logits = logits
            if req_id in bitmasks:
                masked_logits = apply_token_bitmask(logits, bitmasks[req_id])
            out[req_id] = [greedy_sample(masked_logits)]
        return out, drafts_out

    def execute_model(
        self,
        output: SchedulerOutput,
        ready: ReadyHandle,
        host_reqs: Optional[Mapping[str, Req]] = None,
    ) -> ModelRunnerOutput:
        if ready.step_id != output.step_id:
            raise RuntimeError(f"ready/output step mismatch: {ready.step_id} vs {output.step_id}")

        host = host_reqs or {}
        return self._execute_prepared(output, ready, host)

    def _execute_prepared(
        self,
        output: SchedulerOutput,
        ready: ReadyHandle,
        host: Mapping[str, Req],
    ) -> ModelRunnerOutput:
        """执行已 ready 的一步；不负责 pool.done。

        `execute_model` 和 `dummy_run` 共用本路径；真实 ready/done 生命周期
        由 NodeScheduler/RuntimeExecutor 边界统一收口，runner 不 ack pool。
        """

        next_tokens: Dict[str, List[int]] = {}
        next_drafts: Dict[str, List[int]] = {}

        batch = self.prepare_inputs(output, host)
        _meta = self.prepare_attn(batch, ready)
        if self.model_backend == "qwen3":
            next_tokens, next_drafts = self._forward_model(output, host, batch)
        else:
            for req_id, n in output.num_scheduled_tokens.items():
                if n <= 0:
                    continue
                req = host.get(req_id)
                if req is None:
                    continue
                if not batch.is_prompt_phase.get(req_id, False):
                    next_tokens[req_id] = [0]

        return ModelRunnerOutput(
            step_id=output.step_id,
            next_token_ids=next_tokens,
            next_draft_tokens=next_drafts,
            model_backend=self.model_backend,
        )

    def _forward_model(
        self,
        output: SchedulerOutput,
        host_reqs: Mapping[str, Req],
        batch: InputBatch,
    ) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
        assert self._model is not None
        last_logits: Dict[str, List[float]] = {}
        for req_id in batch.req_ids:
            if batch.is_prompt_phase.get(req_id, False):
                continue
            req = host_reqs.get(req_id)
            if req is None:
                continue
            last_logits[req_id] = self._dummy_model_logits(req.all_token_ids)
        return self.sample_tokens(output, host_reqs, last_logits)

    def _dummy_model_logits(self, context: List[int]) -> List[float]:
        assert self._model is not None
        cfg = self._model.config
        logits = [0.0] * cfg.vocab_size
        logits[self._dummy_model_token(context)] = 1.0
        return logits

    def _dummy_model_token(self, context: List[int]) -> int:
        assert self._model is not None
        cfg = self._model.config
        seed = context[-1] if context else cfg.bos_token_id
        return (int(seed) + len(context) + cfg.num_hidden_layers) % cfg.vocab_size

    def clear_drafter(self, req_id: str) -> None:
        return None

    def dummy_run(
        self,
        *,
        num_reqs: int = 1,
        tokens_per_req: int = 1,
        step_id: int = 0,
    ) -> ModelRunnerOutput:
        """对齐 vLLM `GPUModelRunner._dummy_run`：造假 SchedulerOutput 走生产入口。

        用于 warmup / graph capture 占位；构造 dummy host req / ready，不触发
        pool.prepare_step 或 pool.done。
        """
        if not self._model_loaded:
            raise ValueError("model must be loaded before dummy_run")
        num_reqs = max(1, int(num_reqs))
        tokens_per_req = max(1, int(tokens_per_req))
        num_tokens = {f"dummy-{i}": 1 for i in range(num_reqs)}
        host_reqs = {
            rid: Req(
                req_id=rid,
                served_model_name=self._served_model_name,
                prompt_token_ids=list(range(tokens_per_req)),
                sampling_params=SamplingParams(max_new_tokens=1),
            )
            for rid in num_tokens
        }
        output = SchedulerOutput(
            step_id=step_id,
            forward_mode=ForwardMode.DECODE,
            num_scheduled_tokens=num_tokens,
            total_num_scheduled_tokens=sum(num_tokens.values()),
            req_forward_modes={rid: ForwardMode.DECODE for rid in num_tokens},
            req_num_computed_at_schedule={rid: tokens_per_req for rid in num_tokens},
            req_query_start={rid: max(0, tokens_per_req - 1) for rid in num_tokens},
            req_query_end={rid: tokens_per_req for rid in num_tokens},
            can_run_graph=True,
        )
        ready = ReadyHandle(
            step_id=step_id,
            block_table_by_req={
                rid: list(range((tokens_per_req + 7) // 8)) for rid in num_tokens
            },
            slot_mapping_by_req={
                rid: [max(0, tokens_per_req - 1)] for rid in num_tokens
            },
        )
        return self._execute_prepared(output, ready, host_reqs)
