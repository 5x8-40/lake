"""按目标实例补发一条 BlockStored。

复制不经过引擎，引擎不会发「这块 KV 已经在目标上」。kvcm 在 READY
之后用上游 ``ZmqEventPublisher.publish`` 补一条。wire 格式（topic +
seq + msgpack）在上游发布器里，这里不另写一套。

身份 = 端点：每个目标实例一个端点，``data_parallel_rank`` 固定 0，
不做 DP 端口偏移。第一次要给该实例补发时才构造发布器；本进程内不因
实例下线而关。``shutdown`` 只在进程退出时调用。

vllm 懒导入：本模块被 import 时不背 vllm，测试注入假发布器即可。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kvcm.engines.vllm.keys import TokenBlockChain, hash_token_block_chain
from kvcm.ops.types import InstanceId, Prefix

logger = logging.getLogger(__name__)

# (token_ids, block_size, hash_algo) → 满块链原值
ChainFn = Callable[[tuple[int, ...], int, str], TokenBlockChain]
# endpoint → 有 publish(EventBatch) / shutdown() 的发布器
PublisherFactory = Callable[[str], Any]
# () → (BlockStored, EventBatch, MEDIUM_CPU, ZmqEventPublisher)
EventsFn = Callable[[], tuple]


def load_vllm_kv_events() -> tuple:
    """懒导入上游事件类型与 ZmqEventPublisher。测试替换本函数，不装 vllm。"""
    from vllm.distributed.kv_events import (
        MEDIUM_CPU,
        BlockStored,
        EventBatch,
        ZmqEventPublisher,
    )

    return BlockStored, EventBatch, MEDIUM_CPU, ZmqEventPublisher


def parse_kv_event_endpoints(items: Sequence[str] | None) -> dict[str, str]:
    """``--kv-event-endpoint`` 的重复值：``instance_id=tcp://*:port``。"""
    out: dict[str, str] = {}
    for raw in items or ():
        instance_id, sep, endpoint = raw.partition("=")
        if not sep or not instance_id or not endpoint:
            raise ValueError(
                "--kv-event-endpoint 须为 instance_id=tcp://*:port，"
                f"得到 {raw!r}"
            )
        if instance_id in out:
            raise ValueError(f"重复的补发端点实例 {instance_id}")
        out[instance_id] = endpoint
    return out


class BlockStoredPublisher:
    """每个目标实例一个上游发布器，READY 时补一条 BlockStored。"""

    def __init__(
        self,
        endpoints: Mapping[str, str],
        *,
        block_size: int,
        hash_algo: str,
        chain_fn: ChainFn | None = None,
        publisher_factory: PublisherFactory | None = None,
        events_fn: EventsFn | None = None,
    ) -> None:
        self._endpoints = dict(endpoints)
        self._block_size = block_size
        self._hash_algo = hash_algo
        self._chain_fn = chain_fn or hash_token_block_chain
        self._factory = publisher_factory
        self._events_fn = events_fn or load_vllm_kv_events
        # instance_id → 已构造的发布器。不因实例下线而删。
        self._pubs: dict[str, Any] = {}
        self._missing: set[str] = set()

    def publish_ready(self, prefix: Prefix, target: InstanceId) -> None:
        """向该实例的端点补发一条 BlockStored。

        没有端点：打一次日志并返回。不抛——调用方已经核验通过，
        不能把预热打成失败。
        """
        instance_id = str(target)
        endpoint = self._endpoints.get(instance_id)
        if endpoint is None:
            if instance_id not in self._missing:
                self._missing.add(instance_id)
                logger.warning(
                    "kvcm 补发：实例 %s 没有 --kv-event-endpoint，"
                    "跳过 BlockStored（预热仍成功）",
                    instance_id,
                )
            return
        chain = self._chain_fn(prefix.token_ids, self._block_size, self._hash_algo)
        BlockStored, EventBatch, medium_cpu, _ = self._events_fn()
        # MEDIUM_CPU 是 "CPU"。连接器里有小写字面量 "cpu"，不跟它。
        event = BlockStored(
            block_hashes=list(chain.block_hashes),
            parent_block_hash=chain.parent_block_hash,
            token_ids=list(chain.token_ids),
            block_size=chain.block_size,
            lora_id=None,
            medium=medium_cpu,
            lora_name=None,
        )
        batch = EventBatch(
            ts=time.time(),
            events=[event],
            data_parallel_rank=0,
        )
        self._publisher_for(instance_id, endpoint).publish(batch)

    def shutdown(self) -> None:
        """进程退出时关掉已构造的发布器。不是实例下线。"""
        pubs = list(self._pubs.values())
        self._pubs.clear()
        for pub in pubs:
            pub.shutdown()

    def _publisher_for(self, instance_id: str, endpoint: str) -> Any:
        """同一实例只构造一次。rank 固定 0，端点字符串原样交给上游。"""
        pub = self._pubs.get(instance_id)
        if pub is None:
            pub = self._make_publisher(endpoint)
            self._pubs[instance_id] = pub
        return pub

    def _make_publisher(self, endpoint: str) -> Any:
        if self._factory is not None:
            return self._factory(endpoint)
        _, _, _, zmq_publisher = self._events_fn()
        # 0：上游 offset_endpoint_port 在 rank 0 时不改端口。这里不再偏移。
        return zmq_publisher(0, endpoint=endpoint)
