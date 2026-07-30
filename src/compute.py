"""计算层 — Prefill / Decode / Draft 算力池的抽象。

节点无状态（详见 docs/architecture/compute-layer.md）。
本原型用伪前向（不调用真实模型）验证池间协作与 KV 流转。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from .kv_pool import KVBlockID, KVPool, block_hash


class NodeState(str, Enum):
    IDLE = "idle"
    WARM = "warm"       # 权重预加载完成
    READY = "ready"
    SERVING = "serving"
    DRAIN = "drain"


@dataclass
class ComputeNode:
    node_id: str
    pool: str  # "prefill" | "decode" | "draft"
    state: NodeState = NodeState.IDLE
    inflight: int = 0

    def accept(self) -> bool:
        return self.state in (NodeState.READY, NodeState.SERVING)

    def drain(self) -> None:
        self.state = NodeState.DRAIN


@dataclass
class PrefillResult:
    """Prefill 产出的 KV block 列表。"""
    blocks: List[KVBlockID]


class PrefillPool:
    """Prefill 池：处理 prompt，产出 KV 并写入 KV Pool。"""

    def __init__(self, kv_pool: KVPool, model_id: str, block_size: int = 16) -> None:
        self.kv_pool = kv_pool
        self.model_id = model_id
        self.block_size = block_size
        self.nodes: List[ComputeNode] = []

    def prefill(self, token_ids: List[int], n_layers: int) -> PrefillResult:
        """伪 prefill：把 prompt 切成 block，逐 block 注册到 KV Pool。

        返回产出的 block ID 列表。真实实现此处为模型前向 + KV 写回。
        """
        blocks: List[KVBlockID] = []
        for layer in range(n_layers):
            for i in range(0, len(token_ids), self.block_size):
                chunk = token_ids[i : i + self.block_size]
                bid = KVBlockID(self.model_id, layer, block_hash(chunk))
                self.kv_pool.put(bid, data=b"\x00" * (len(chunk) * 8))  # 占位
                blocks.append(bid)
        return PrefillResult(blocks=blocks)


class DecodePool:
    """Decode 池：从 KV Pool 拉取前缀 KV，逐 token 生成。"""

    def __init__(self, kv_pool: KVPool, model_id: str) -> None:
        self.kv_pool = kv_pool
        self.model_id = model_id
        self.nodes: List[ComputeNode] = []

    def load_prefix(self, block_ids: List[KVBlockID]) -> int:
        """从 KV Pool 拉取前缀 KV。返回命中的 block 数。"""
        hit = 0
        for bid in block_ids:
            if self.kv_pool.acquire(bid) is not None:
                hit += 1
        return hit

    def decode_step(self, n_new_tokens: int) -> int:
        """伪 decode：每步产出 n_new_tokens 个新 token 的 KV（占位）。"""
        return n_new_tokens
