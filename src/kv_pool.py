"""KV Cache Pool — 把 KV cache 作为全局可寻址的分布式资源。

核心抽象：
  - KVBlockID: 内容寻址的 block 标识 (model_id, layer_idx, block_hash)
  - KVBlock: 实际的 KV 张量数据（此处用 bytes 占位）
  - KVPool: block 的注册 / 查找 / 引用计数 / 驱逐

这是单进程内存版原型，用于验证前缀复用与引用计数逻辑。
真实实现见 docs/architecture/kv-cache-pool.md（RDMA + 分片 KV Node）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def block_hash(token_ids: List[int]) -> str:
    """由 token 内容计算 block hash，相同内容 → 相同 KV。"""
    h = hashlib.sha256()
    for t in token_ids:
        h.update(t.to_bytes(8, "little"))
    return h.hexdigest()


@dataclass(frozen=True)
class KVBlockID:
    model_id: str
    layer_idx: int
    block_hash: str

    def __str__(self) -> str:
        return f"{self.model_id}/L{self.layer_idx}/{self.block_hash[:12]}"


@dataclass
class KVBlock:
    block_id: KVBlockID
    data: bytes  # 真实实现中是 GPU tensor，跨节点通过 RDMA 传输
    ref_count: int = 0
    access_count: int = 0  # 用于 LRU 热度


class KVPool:
    """单进程版 KV Pool（原型）。

    TODO: 分片到多个 KVNode、内容寻址的 radix tree 索引、
    RDMA 数据平面、引用计数与驱逐策略。
    """

    def __init__(self) -> None:
        self._blocks: Dict[KVBlockID, KVBlock] = {}

    def put(self, block_id: KVBlockID, data: bytes) -> None:
        if block_id not in self._blocks:
            self._blocks[block_id] = KVBlock(block_id=block_id, data=data)
        # 内容寻址：相同 block 复用，ref 不重复增加
        self._blocks[block_id].access_count += 1

    def get(self, block_id: KVBlockID) -> Optional[KVBlock]:
        blk = self._blocks.get(block_id)
        if blk is not None:
            blk.access_count += 1
        return blk

    def acquire(self, block_id: KVBlockID) -> Optional[KVBlock]:
        blk = self.get(block_id)
        if blk is not None:
            blk.ref_count += 1
        return blk

    def release(self, block_id: KVBlockID) -> None:
        blk = self._blocks.get(block_id)
        if blk is not None and blk.ref_count > 0:
            blk.ref_count -= 1

    def evict_cold(self, max_blocks: int) -> int:
        """驱逐引用为 0 且热度最低的 block，回到 max_blocks 以内。"""
        if len(self._blocks) <= max_blocks:
            return 0
        candidates = [b for b in self._blocks.values() if b.ref_count == 0]
        candidates.sort(key=lambda b: b.access_count)
        n_evict = len(self._blocks) - max_blocks
        for blk in candidates[:n_evict]:
            self._blocks.pop(blk.block_id, None)
        return min(n_evict, len(candidates))

    def __len__(self) -> int:
        return len(self._blocks)
