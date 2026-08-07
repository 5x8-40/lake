"""P7.1 测量基座:Python 探针。

跑:`LAKE_BENCH_OUT=bench/results/python.jsonl PYTHONPATH=python python3 -m lake.runtime.bench`

采集:
- 冷启动两策略(sequential / layer_async+prefetch)分段时延(复用 P6.6 harness)
- mock WorkerEngine e2e 请求时延(prefill+decode 闭环,mock backend)

JSONL schema 见 bench/README.md。mock 计算 + sleep 模拟 I/O → 原型相对值,
真 GPU/介质绝对值校准 defer(issue #61)。
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Sequence

from lake.engine.model_runner import ModelRunner
from lake.engine.pool_iface import ReadyHandle, StepStats
from lake.runtime.coldstart import run_layer_async, run_sequential, waterfall_layer_async
from lake.runtime.node_scheduler import build_req_from_generate
from lake.runtime.role import RoleConfig
from lake.runtime.scheduler_output import ForwardMode, SchedulerOutput
from lake.runtime.worker_engine import WorkerEngine

_ENV = {
    "compute": "mock-backend",
    "io": "sleep-simulated",
    "note": "原型相对值;真 GPU/介质绝对值校准 defer(issue #61)",
}


def _emit(name: str, latency_ms: Dict[str, float], counters: Dict[str, int]) -> None:
    out = os.environ.get("LAKE_BENCH_OUT")
    if not out:
        return
    rec = {
        "name": name,
        "lang": "python",
        "phase": "P7.1",
        "env": _ENV,
        "latency_ms": latency_ms,
        "counters": counters,
    }
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _percentile(samples: List[float], q: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    return s[int(q * (len(s) - 1))]


class _MockLayerSource:
    def __init__(self, num_layers: int, per_layer_ms: float) -> None:
        self._n = num_layers
        self._ms = per_layer_ms

    @property
    def num_layers(self) -> int:
        return self._n

    def load_layer(self, index: int) -> None:
        time.sleep(self._ms / 1000.0)


class _MockPrefetcher:
    def __init__(self, per_block_ms: float) -> None:
        self._ms = per_block_ms

    def prefetch(self, block_ids: Sequence[bytes]) -> None:
        time.sleep(self._ms * len(block_ids) / 1000.0)


def _bench_coldstart() -> None:
    layers, per_layer_ms, hot_blocks = 28, 5.0, 32
    src = _MockLayerSource(layers, per_layer_ms)
    seq = run_sequential(src, _MockPrefetcher(1.0), [bytes([i]) for i in range(hot_blocks)])
    _emit(
        "coldstart_sequential",
        {
            "serve_gate": seq.time_to_serve_gate * 1000,
            "fully_ready": seq.time_to_fully_ready * 1000,
        },
        {"layers": layers, "hot_blocks": hot_blocks},
    )
    src2 = _MockLayerSource(layers, per_layer_ms)
    async_m = run_layer_async(
        src2, serve_after_layers=4, prefetcher=_MockPrefetcher(1.0),
        hot_blocks=[bytes([i]) for i in range(hot_blocks)],
    )
    _emit(
        "coldstart_layer_async",
        {
            "serve_gate": async_m.time_to_serve_gate * 1000,
            "fully_ready": async_m.time_to_fully_ready * 1000,
        },
        {"layers": layers, "hot_blocks": hot_blocks, "layers_at_gate": async_m.layers_loaded_at_gate},
    )


class _FakePool:
    """与 test_worker_engine 同形的最小 mock:prepare 即返回块统计。"""

    def prepare_step(self, output: SchedulerOutput, reqs: Dict) -> ReadyHandle:
        stats = {}
        for rid in output.num_scheduled_tokens:
            req = reqs[rid]
            phase = output.req_forward_modes.get(rid, output.forward_mode)
            if phase == ForwardMode.EXTEND:
                nblocks = (len(req.prompt_token_ids) + 7) // 8
                stats[rid] = StepStats(reused_blocks=0, prefill_blocks=nblocks)
            else:
                stats[rid] = StepStats(reused_blocks=0, prefill_blocks=0)
        return ReadyHandle(step_id=output.step_id, stats_by_req=stats)

    def done(self, step_id: int) -> None:
        return None

    def on_request_finished(self, req) -> None:
        return None

    def commit_write_extent(self, req_id: str, token_end: int) -> None:
        return None


def _bench_engine_e2e() -> None:
    pool = _FakePool()
    runner = ModelRunner(pool, model_backend="mock")  # type: ignore[arg-type]
    role = RoleConfig(enable_overlap=True, max_running_reqs=4, model_backend="mock")
    eng = WorkerEngine(pool, runner, role, coalesce_s=0.002)  # type: ignore[arg-type]
    eng.start()
    samples_ms: List[float] = []
    try:
        for i in range(32):
            req = build_req_from_generate(f"bench-{i}", "m", list(range(16)), 4, "n0")
            t0 = time.monotonic()
            done = eng.submit(req)
            samples_ms.append((time.monotonic() - t0) * 1000)
            assert done.finished
    finally:
        eng.stop()
    _emit(
        "engine_e2e_mock",
        {"p50": _percentile(samples_ms, 0.50), "p99": _percentile(samples_ms, 0.99)},
        {"requests": len(samples_ms)},
    )


def _bench_coldstart_sweep() -> None:
    """P7.4:冷启动参数扫描——层数 × 单层成本 × gate 层数 → serve-gate / fully-ready。

    校准 SLO「扩容决策→Ready <10s」的预算分配:Ready=serve gate,
    关键路径 = provision + serve_after_layers × per_layer;KV prefetch 与
    gate 后权重在后台,不进 Ready 预算。
    """
    provision_s = 0.05  # mock provision(真实调度/拉起待真机)
    for layers, per_layer_ms in [(28, 50.0), (28, 200.0), (80, 50.0), (80, 200.0)]:
        for gate_layers in [2, 4, 8]:
            src = _MockLayerSource(layers, per_layer_ms)
            m, _segs = waterfall_layer_async(
                src, gate_layers, _MockPrefetcher(0.5),
                [bytes([i]) for i in range(64)], provision_s=provision_s,
            )
            _emit(
                "coldstart_sweep",
                {
                    "serve_gate": m.time_to_serve_gate * 1000,
                    "fully_ready": m.time_to_fully_ready * 1000,
                    "kv_warm": m.time_to_kv_warm * 1000,
                },
                {
                    "layers": layers,
                    "per_layer_ms_x10": int(per_layer_ms * 10),
                    "gate_layers": gate_layers,
                    "hot_blocks": 64,
                    "meets_10s_slo": int(m.time_to_serve_gate < 10.0),
                },
            )


def main() -> None:
    _bench_coldstart()
    _bench_coldstart_sweep()
    _bench_engine_e2e()
    print("python probes done")


if __name__ == "__main__":
    main()
