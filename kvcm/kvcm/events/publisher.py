"""按目标实例补发一条 BlockStored。

复制不经过引擎，引擎不会发「这块 KV 已经在目标上」。kvcm 在 READY
之后用上游 ``ZmqEventPublisher`` 补一条。wire 格式（topic +
seq + msgpack）在上游发布器里，这里不另写一套。真正的发布器对
``_event_queue`` 调 ``put_nowait``，避免 ``publish`` 里的 ``Queue.put``
在队列满时卡住事件循环；没有该队列的发布器仍走 ``publish``。

身份 = 端点：每个目标实例一个端点，``data_parallel_rank`` 固定 0，
不做 DP 端口偏移。第一次要给该实例补发时才构造发布器；本进程内不因
实例下线而关。``shutdown`` 只在进程退出时调用。

vllm 懒导入：本模块被 import 时不背 vllm，测试注入假发布器即可。
"""

from __future__ import annotations

import logging
import queue
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
# 上游 maybe_convert_block_hash。测试替换，不装 vllm。
ConvertFn = Callable[[object], object]


def load_vllm_kv_events() -> tuple:
    """懒导入上游事件类型与 ZmqEventPublisher。测试替换本函数，不装 vllm。"""
    from vllm.distributed.kv_events import (
        MEDIUM_CPU,
        BlockStored,
        EventBatch,
        ZmqEventPublisher,
    )

    return BlockStored, EventBatch, MEDIUM_CPU, ZmqEventPublisher


def convert_block_hash(block_hash: object) -> object:
    """懒导入上游 ``maybe_convert_block_hash``。测试替换本函数，不装 vllm。

    默认 ``VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES`` 为 True 时，上游把 bytes
    收成大端整数的低 64 位。这里不重写掩码。
    """
    from vllm.v1.core.kv_cache_utils import maybe_convert_block_hash

    return maybe_convert_block_hash(block_hash)


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
        convert_fn: ConvertFn | None = None,
    ) -> None:
        self._endpoints = dict(endpoints)
        self._block_size = block_size
        self._hash_algo = hash_algo
        self._chain_fn = chain_fn or hash_token_block_chain
        self._factory = publisher_factory
        self._events_fn = events_fn or load_vllm_kv_events
        self._convert_fn = convert_fn or convert_block_hash
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
        # 不满一块：hash_token_block_chain 给出空 block_hashes，不发。
        if not chain.block_hashes:
            return
        BlockStored, EventBatch, medium_cpu, _ = self._events_fn()
        convert = self._convert_fn
        parent = chain.parent_block_hash
        if parent is not None:
            parent = convert(parent)
        # MEDIUM_CPU 是 "CPU"。连接器里有小写字面量 "cpu"，不跟它。
        event = BlockStored(
            block_hashes=[convert(h) for h in chain.block_hashes],
            parent_block_hash=parent,
            token_ids=list(chain.token_ids),
            block_size=chain.block_size,
            lora_id=None,
            medium=medium_cpu,
            lora_name=None,
            group_idx=0,
            # GroupIdx 非空时 llm-d 先查 kind。默认 None 被 omit_defaults 丢掉，
            # 空 kind 会被判 unsupported_cache_kind。单组全注意力填这个字符串。
            kv_cache_spec_kind="full_attention",
        )
        batch = EventBatch(
            ts=time.time(),
            events=[event],
            data_parallel_rank=0,
        )
        self._enqueue(instance_id, self._publisher_for(instance_id, endpoint), batch)

    def _enqueue(self, instance_id: str, pub: Any, batch: Any) -> None:
        """真正的 ZmqEventPublisher 走 put_nowait；假发布器没有队列，走 publish。

        ``Queue.put`` 在 maxsize 满时会阻塞事件循环。满了只记日志并返回，
        不把已经 READY 的预热打成失败。``data_parallel_rank`` 已在 batch 上设为 0。
        """
        event_queue = getattr(pub, "_event_queue", None)
        if event_queue is None:
            pub.publish(batch)
            return
        try:
            event_queue.put_nowait(batch)
        except queue.Full:
            logger.warning(
                "kvcm 补发：实例 %s 的事件队列已满，"
                "跳过 BlockStored（预热仍成功）",
                instance_id,
            )

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
