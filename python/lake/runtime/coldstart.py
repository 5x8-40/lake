"""P6.6 冷启动压缩:权重预加载 + layer-async serve + KV prefetch。

参考实现:Dynamo ModelExpress(NIXL/NVLink GPU 间流式传权重,~7x 冷启动;
`docs/research/dynamo/overview.md:31`)。借鉴点:边传边服务——不等全量权重
落盘/落 HBM 再 Ready,而是前若干层就绪即开 serve 门,其余层后台流式续传。
关键差异:ModelExpress 的加速在真实 P2P 权重传输;lake 权重所有权归存储池
(`model_runner.load_model` 只记状态、pin 回调),本模块编排"加载→开门→预热"
三阶段的**时序**,传输层(真 NIXL/RDMA)归 P5/P7——此处用注入的
LayerSource/KVPrefetcher 做单进程闭环与 micro-bench(issue #52 defer:
P6 各阶段先用单进程/单测模拟闭环)。

三阶段语义:
- 权重预加载:进程启动即后台开载(不等首个请求),layer-async 的前奏。
- layer-async serve:前 `serve_after_layers` 层就绪 → 开 serve 门
  (time_to_serve_gate),其余层后台续传(time_to_weights_done)。
- KV prefetch:热门前缀与权重加载**重叠**拉取(独立线程),
  首个请求即命中热 KV,避免"权重 Ready 但 KV 冷"的二次冷启动。

判据(issue #52):冷启动时延 micro-bench;目标值待 P7 校准。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class LayerSource(Protocol):
    """权重层来源(真实=存储池/对象存储;mock=定时 sleep)。"""

    @property
    def num_layers(self) -> int: ...

    def load_layer(self, index: int) -> None:
        """阻塞式加载一层(耗时由实现决定)。"""
        ...


@runtime_checkable
class KVPrefetcher(Protocol):
    """热 KV 预取器(真实=存储池 PlaceBlocks/Pull;mock=定时 sleep + 记录)。"""

    def prefetch(self, block_ids: Sequence[bytes]) -> None: ...


@dataclass
class ColdStartMetrics:
    """冷启动时延分解(秒,time.monotonic 口径)。"""

    strategy: str
    time_to_serve_gate: float = 0.0  # serve 门开(可接第一个请求)
    time_to_weights_done: float = 0.0  # 全部层加载完
    time_to_kv_warm: float = 0.0  # 热 KV prefetch 完(无 prefetch = 0)
    time_to_fully_ready: float = 0.0  # max(weights_done, kv_warm)
    layers_loaded_at_gate: int = 0  # 开门时已就绪层数
    extra: dict = field(default_factory=dict)


def run_sequential(
    source: LayerSource,
    prefetcher: KVPrefetcher | None = None,
    hot_blocks: Sequence[bytes] = (),
) -> ColdStartMetrics:
    """基线:全层加载完才 Ready;KV prefetch 串行其后(无重叠)。"""
    t0 = time.monotonic()
    for i in range(source.num_layers):
        source.load_layer(i)
    weights_done = time.monotonic() - t0
    kv_warm = 0.0
    if prefetcher is not None and hot_blocks:
        prefetcher.prefetch(hot_blocks)
        kv_warm = time.monotonic() - t0
    fully = max(weights_done, kv_warm)
    return ColdStartMetrics(
        strategy="sequential",
        time_to_serve_gate=weights_done,  # 基线:门=全量加载完
        time_to_weights_done=weights_done,
        time_to_kv_warm=kv_warm,
        time_to_fully_ready=fully,
        layers_loaded_at_gate=source.num_layers,
    )


def run_layer_async(
    source: LayerSource,
    serve_after_layers: int,
    prefetcher: KVPrefetcher | None = None,
    hot_blocks: Sequence[bytes] = (),
) -> ColdStartMetrics:
    """layer-async:前 serve_after_layers 层就绪即开门;KV prefetch 重叠进行。

    权重预加载语义:调用方在进程启动即调本函数(后台线程),不等首个请求。
    """
    if serve_after_layers <= 0 or serve_after_layers > source.num_layers:
        raise ValueError("serve_after_layers 须在 (0, num_layers]")

    t0 = time.monotonic()
    kv_done = threading.Event()
    kv_warm_at = 0.0

    def _prefetch() -> None:
        nonlocal kv_warm_at
        if prefetcher is not None and hot_blocks:
            prefetcher.prefetch(hot_blocks)
        kv_warm_at = time.monotonic() - t0
        kv_done.set()

    # KV prefetch 与权重加载重叠(独立线程;真实实现=存储池后台带宽池 <10%)
    pt = threading.Thread(target=_prefetch, daemon=True)
    pt.start()

    gate_at = 0.0
    for i in range(source.num_layers):
        source.load_layer(i)
        if i + 1 == serve_after_layers:
            gate_at = time.monotonic() - t0  # 门开:其余层继续(本线程续传)
    weights_done = time.monotonic() - t0
    pt.join()
    fully = max(weights_done, kv_warm_at)
    return ColdStartMetrics(
        strategy="layer_async+kv_prefetch",
        time_to_serve_gate=gate_at,
        time_to_weights_done=weights_done,
        time_to_kv_warm=kv_warm_at,
        time_to_fully_ready=fully,
        layers_loaded_at_gate=serve_after_layers,
    )
