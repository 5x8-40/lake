"""P6.6 判据:冷启动时延 micro-bench(目标值待 P7 校准,本测断言相对收益)。

模拟口径:8 层 × 5ms/层 + KV prefetch 20ms(真实实现归 P5/P7 传输层)。
断言用宽松比率(CI 稳定),实测数值打印供 P7 参考。
"""

import time

from lake.runtime.coldstart import (
    format_waterfall,
    run_layer_async,
    run_sequential,
    waterfall_layer_async,
)

NUM_LAYERS = 8
LAYER_COST = 0.005  # 5ms/层
PREFETCH_COST = 0.020  # 20ms 热 KV
HOT = [b"hot0", b"hot1", b"hot2"]


class MockLayerSource:
    @property
    def num_layers(self) -> int:
        return NUM_LAYERS

    def load_layer(self, index: int) -> None:
        time.sleep(LAYER_COST)


class MockPrefetcher:
    def __init__(self) -> None:
        self.got: list[bytes] = []

    def prefetch(self, block_ids) -> None:
        time.sleep(PREFETCH_COST)
        self.got = list(block_ids)


def test_layer_async_gate_opens_far_earlier_than_sequential() -> None:
    seq = run_sequential(MockLayerSource())
    async_ = run_layer_async(MockLayerSource(), serve_after_layers=2)

    # 基线:门 = 全量加载(8 层)
    assert seq.time_to_serve_gate >= NUM_LAYERS * LAYER_COST * 0.9
    assert seq.layers_loaded_at_gate == NUM_LAYERS
    # layer-async:2 层即开门,门时延 < 基线一半(理论 10ms vs 40ms)
    assert async_.layers_loaded_at_gate == 2
    assert async_.time_to_serve_gate < seq.time_to_serve_gate * 0.5
    # 全量就绪时延两者相当(layer-async 不为开门牺牲总时长)
    assert async_.time_to_weights_done >= NUM_LAYERS * LAYER_COST * 0.9
    print(
        f"\n[micro-bench] gate: sequential={seq.time_to_serve_gate*1e3:.1f}ms "
        f"vs layer_async={async_.time_to_serve_gate*1e3:.1f}ms "
        f"({seq.time_to_serve_gate/async_.time_to_serve_gate:.1f}x earlier)"
    )


def test_kv_prefetch_overlapped_with_weight_load() -> None:
    pf_seq, pf_async = MockPrefetcher(), MockPrefetcher()
    seq = run_sequential(MockLayerSource(), pf_seq, HOT)
    async_ = run_layer_async(MockLayerSource(), 2, pf_async, HOT)

    # 串行基线:fully = 40ms 权重 + 20ms prefetch ≈ 60ms
    assert seq.time_to_fully_ready >= (NUM_LAYERS * LAYER_COST + PREFETCH_COST) * 0.9
    # 重叠:fully ≈ max(40, 20) ≈ 40ms < 串行 80%
    assert async_.time_to_fully_ready < seq.time_to_fully_ready * 0.8
    # 热块确实被 prefetch(首个请求即命中热 KV)
    assert pf_seq.got == HOT and pf_async.got == HOT
    print(
        f"\n[micro-bench] fully_ready: sequential+prefetch={seq.time_to_fully_ready*1e3:.1f}ms "
        f"vs overlapped={async_.time_to_fully_ready*1e3:.1f}ms"
    )


def test_serve_after_layers_validated() -> None:
    import pytest

    with pytest.raises(ValueError):
        run_layer_async(MockLayerSource(), 0)
    with pytest.raises(ValueError):
        run_layer_async(MockLayerSource(), NUM_LAYERS + 1)


def test_waterfall_segments_cover_timeline() -> None:
    """P7.4:瀑布分解——段覆盖完整时间线,critical 段之和 = serve gate。"""
    m, segs = waterfall_layer_async(
        MockLayerSource(), serve_after_layers=2,
        prefetcher=MockPrefetcher(), hot_blocks=HOT, provision_s=0.01,
    )
    names = [s.name for s in segs]
    assert names[0] == "provision"
    assert "kv_prefetch" in names
    weight_segs = [s for s in segs if s.name.startswith("weight_layer_")]
    assert len(weight_segs) == NUM_LAYERS
    # critical = provision + 前 2 层;其余层与 kv_prefetch 在后台
    critical = [s for s in segs if s.critical]
    assert len(critical) == 3  # provision + layer_0 + layer_1
    critical_end = max(s.end_s for s in critical)
    assert abs(critical_end - m.time_to_serve_gate) < 0.01
    # 段总和 ≈ fully_ready 时间线内(无段超出)
    assert all(s.end_s <= m.time_to_fully_ready + 0.01 for s in segs)
    # 甘特输出包含关键/后台两种标记
    gantt = format_waterfall(segs, m.time_to_fully_ready)
    assert "#" in gantt and "." in gantt
    print(f"\n[waterfall]\n{gantt}")
