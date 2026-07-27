"""薄 ModelRunner：consume ready → prepare → forward → done → sample。

对齐 vLLM `GPUModelRunner.execute_model`：统一入口，按本步 token 几何执行
（`num_scheduled_tokens` / `req_num_computed_at_schedule`），不按 SGLang 分相状态机。
C8：`prepare_inputs` / `prepare_attn` / `sample_tokens` 拆步；残差 query 路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from engine.attn.metadata import AttentionMetadata, build_attn_metadata
from engine.drafter.tiny_mtp import TinyMTPDrafter
from engine.input_batch import InputBatch
from engine.models.tiny_lm import TinyLM
from engine.pool_iface import PoolIface
from engine.pool_types import ReadyHandle
from engine.sample.greedy import greedy_sample
from engine.sample.reject import chain_reject_sample
from runtime.req import Req
from runtime.scheduler_output import SchedulerOutput


@dataclass
class ModelRunnerOutput:
    step_id: int
    next_token_ids: Dict[str, List[int]] = field(default_factory=dict)
    next_draft_tokens: Dict[str, List[int]] = field(default_factory=dict)
    model_backend: str = "mock"


class ModelRunner:
    def __init__(
        self,
        pool: PoolIface,
        *,
        model_backend: str = "mock",
        tiny_lm: Optional[TinyLM] = None,
        enable_drafter: bool = False,
        num_draft_tokens: int = 2,
        drafter: Optional[TinyMTPDrafter] = None,
    ) -> None:
        self._pool = pool
        self._input_batch = InputBatch()
        self._attn_meta: Optional[AttentionMetadata] = None
        self.model_backend = model_backend
        self._tiny: Optional[TinyLM] = tiny_lm
        if model_backend == "tiny_lm" and self._tiny is None:
            self._tiny = TinyLM()
        self.enable_drafter = enable_drafter
        self._drafter: Optional[TinyMTPDrafter] = drafter
        if enable_drafter and self._drafter is None and self._tiny is not None:
            self._drafter = TinyMTPDrafter(
                num_draft_tokens=num_draft_tokens,
                vocab_size=self._tiny.vocab_size,
                d_model=self._tiny.d_model,
                n_heads=self._tiny.n_heads,
            )

    def prepare_inputs(
        self,
        output: SchedulerOutput,
        host_reqs: Mapping[str, Req],
    ) -> InputBatch:
        """对齐 vLLM `prepare_inputs`：组本步 InputBatch（无跨步 RequestState）。"""
        batch = InputBatch()
        for req_id, n in output.num_scheduled_tokens.items():
            if n <= 0:
                continue
            req = host_reqs.get(req_id)
            if req is None:
                continue
            prompt_len = len(req.prompt_token_ids)
            computed = output.req_num_computed_at_schedule.get(req_id, req.num_computed_tokens)
            batch.req_ids.append(req_id)
            batch.num_scheduled_tokens[req_id] = n
            batch.num_computed_tokens[req_id] = computed
            if computed < prompt_len:
                end = min(prompt_len, computed + n)
                batch.token_ids[req_id] = list(req.prompt_token_ids[:end])
                batch.query_start[req_id] = computed
                batch.query_end[req_id] = end
                batch.is_prompt_phase[req_id] = True
            else:
                ctx = list(req.all_token_ids)
                batch.token_ids[req_id] = ctx
                # decode：以最后已有 token 为 query，预测下一 token
                qs = max(0, len(ctx) - 1)
                batch.query_start[req_id] = qs
                batch.query_end[req_id] = len(ctx)
                batch.is_prompt_phase[req_id] = False
        self._input_batch = batch
        return batch

    def prepare_attn(self, batch: InputBatch, ready: ReadyHandle) -> AttentionMetadata:
        """对齐 vLLM `prepare_attn`：几何 + agent block table（只读）。"""
        seq_lens = {}
        for rid in batch.req_ids:
            seq_lens[rid] = batch.query_end[rid]
        meta = build_attn_metadata(
            seq_lens=seq_lens,
            query_start=batch.query_start,
            query_end=batch.query_end,
            block_tables=ready.block_table_by_req,
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
        spec_map = output.scheduled_spec_decode_tokens or {}
        for req_id, logits in last_logits.items():
            req = host_reqs.get(req_id)
            if req is None:
                continue
            draft = list(spec_map.get(req_id) or [])
            if draft:
                assert self._tiny is not None
                accepted = chain_reject_sample(
                    req.all_token_ids, draft, self._tiny.greedy_token
                )
                out[req_id] = accepted
            else:
                out[req_id] = [greedy_sample(logits)]
            if self._drafter is not None and req_id in out:
                new_ctx = list(req.all_token_ids) + list(out[req_id])
                self._drafter.post_forward(req_id, new_ctx)
                drafts_out[req_id] = self._drafter.pre_forward(req_id)
        return out, drafts_out

    def execute_model(
        self,
        output: SchedulerOutput,
        ready: ReadyHandle,
        host_reqs: Optional[Mapping[str, Req]] = None,
        *,
        dummy_run: bool = False,
    ) -> ModelRunnerOutput:
        if ready.step_id != output.step_id:
            raise RuntimeError(f"ready/output step mismatch: {ready.step_id} vs {output.step_id}")

        host = host_reqs or {}
        next_tokens: Dict[str, List[int]] = {}
        next_drafts: Dict[str, List[int]] = {}

        try:
            if dummy_run:
                return ModelRunnerOutput(
                    step_id=output.step_id, model_backend=self.model_backend
                )
            batch = self.prepare_inputs(output, host)
            _meta = self.prepare_attn(batch, ready)
            if self.model_backend == "tiny_lm":
                next_tokens, next_drafts = self._forward_tiny(output, host, batch)
            else:
                for req_id, n in output.num_scheduled_tokens.items():
                    if n <= 0:
                        continue
                    req = host.get(req_id)
                    if req is None:
                        continue
                    if not batch.is_prompt_phase.get(req_id, False):
                        next_tokens[req_id] = [0]
        finally:
            if not dummy_run:
                self._pool.done(output.step_id)

        return ModelRunnerOutput(
            step_id=output.step_id,
            next_token_ids=next_tokens,
            next_draft_tokens=next_drafts,
            model_backend=self.model_backend,
        )

    def _forward_tiny(
        self,
        output: SchedulerOutput,
        host_reqs: Mapping[str, Req],
        batch: InputBatch,
    ) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
        assert self._tiny is not None
        last_logits: Dict[str, List[float]] = {}
        drafts_prompt: Dict[str, List[int]] = {}

        for req_id in batch.req_ids:
            tokens = batch.token_ids[req_id]
            qs = batch.query_start[req_id]
            qe = batch.query_end[req_id]
            logits_rows = self._tiny.forward_query_logits(tokens, qs, qe)
            if batch.is_prompt_phase.get(req_id, False):
                # EXTEND：不产出 user token；整段 prompt 算完后挂 draft
                req = host_reqs[req_id]
                prompt_len = len(req.prompt_token_ids)
                computed = batch.num_computed_tokens[req_id]
                n = batch.num_scheduled_tokens[req_id]
                if self._drafter is not None and computed + n >= prompt_len:
                    self._drafter.post_forward(req_id, req.prompt_token_ids)
                    drafts_prompt[req_id] = self._drafter.pre_forward(req_id)
                continue
            if logits_rows:
                last_logits[req_id] = logits_rows[-1]

        # TARGET_VERIFY：走 sample 内 reject（可能无 last_logits）
        spec_map = output.scheduled_spec_decode_tokens or {}
        for req_id, draft in spec_map.items():
            if draft and req_id not in last_logits and req_id in batch.req_ids:
                last_logits[req_id] = [0.0] * self._tiny.vocab_size

        sampled, drafts = self.sample_tokens(output, host_reqs, last_logits)
        drafts.update(drafts_prompt)
        return sampled, drafts

    def clear_drafter(self, req_id: str) -> None:
        if self._drafter is not None:
            self._drafter.clear(req_id)

    def dummy_run(
        self,
        *,
        num_reqs: int = 1,
        tokens_per_req: int = 1,
        step_id: int = 0,
    ) -> ModelRunnerOutput:
        """对齐 vLLM `GPUModelRunner._dummy_run`：造假 SchedulerOutput 走生产入口。

        用于 warmup / graph capture 占位；跳过真实 add/update 与 pool.done。
        """
        from runtime.scheduler_output import ForwardMode

        num_tokens = {
            f"dummy-{i}": tokens_per_req for i in range(num_reqs)
        }
        output = SchedulerOutput(
            step_id=step_id,
            forward_mode=ForwardMode.DECODE,
            num_scheduled_tokens=num_tokens,
            total_num_scheduled_tokens=sum(num_tokens.values()),
            req_forward_modes={rid: ForwardMode.DECODE for rid in num_tokens},
            req_num_computed_at_schedule={rid: 1 for rid in num_tokens},
            can_run_graph=True,
        )
        ready = ReadyHandle(step_id=step_id)
        return self.execute_model(output, ready, host_reqs={}, dummy_run=True)
