"""路由与调度 — 无状态决策器（详见 docs/architecture/scheduling.md）。

本原型实现请求级路由的核心逻辑：
  1. 前缀解析（查 KV Pool 已有 block）
  2. Prefill 节点选择（亲和性 + 负载）
  3. 触发 prefill → 写回 KV → decode 续推
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .compute import PrefillPool, DecodePool
from .kv_pool import KVBlockID, KVPool, block_hash


@dataclass
class Request:
    model_id: str
    prompt_tokens: List[int]
    max_tokens: int = 128
    block_size: int = 16
    n_layers: int = 4


@dataclass
class RouteDecision:
    reused_blocks: int       # 前缀复用命中的 block 数
    prefill_blocks: int      # 需要新计算的 block 数
    decode_ready: bool


class Router:
    def __init__(self, kv_pool: KVPool, prefill_pool: PrefillPool,
                 decode_pool: DecodePool) -> None:
        self.kv_pool = kv_pool
        self.prefill_pool = prefill_pool
        self.decode_pool = decode_pool

    def _prefix_blocks(self, req: Request) -> List[KVBlockID]:
        """计算 prompt 在第 0 层的 block ID（前缀复用判定用第 0 层为代表）。"""
        ids = []
        for i in range(0, len(req.prompt_tokens), req.block_size):
            chunk = req.prompt_tokens[i : i + req.block_size]
            ids.append(KVBlockID(req.model_id, 0, block_hash(chunk)))
        return ids

    def route(self, req: Request) -> RouteDecision:
        prefix_ids = self._prefix_blocks(req)
        reused = sum(1 for bid in prefix_ids if self.kv_pool.get(bid) is not None)

        # 只 prefill 未命中的部分（真实实现需精细切分；此处简化为整体 prefill）
        if reused < len(prefix_ids):
            self.prefill_pool.prefill(req.prompt_tokens, req.n_layers)

        # Decode 端拉取前缀 KV
        all_blocks = self._prefix_blocks(req)  # 简化：仅第 0 层
        self.decode_pool.load_prefix(all_blocks)

        return RouteDecision(
            reused_blocks=reused,
            prefill_blocks=max(0, len(prefix_ids) - reused),
            decode_ready=True,
        )
