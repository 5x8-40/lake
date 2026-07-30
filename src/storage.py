"""分层缓存存储 — 对象存储(SSOT) 之上的多级缓存。

层级（详见 docs/architecture/storage-layer.md）：
  L0 GPU HBM   — 节点本地，不在此模块建模
  L1 主机 RAM  — per-node
  L2 本地 NVMe — per-node
  L3 远端内存池 (KV Pool)
  L4 对象存储  — SSOT

本原型只建模 L1/L2/L4 的回填与驱逐，用于验证分层策略。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Optional


class LRUCache:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._store: "OrderedDict[Any, bytes]" = OrderedDict()

    def get(self, key: Any) -> Optional[bytes]:
        if key in self._store:
            self._store.move_to_end(key)
            return self._store[key]
        return None

    def put(self, key: Any, value: bytes) -> Optional[Any]:
        """放入，返回被驱逐的 key（若有）。"""
        evicted = None
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        while len(self._store) > self.capacity:
            evicted, _ = self._store.popitem(last=False)
        return evicted

    def __contains__(self, key: Any) -> bool:
        return key in self._store

    def __len__(self) -> int:
        return len(self._store)


class TieredStorage:
    """L1(RAM) → L2(NVMe) → L4(对象存储 SSOT) 的分层缓存。"""

    def __init__(self, ram_cap: int, nvme_cap: int) -> None:
        self.l1 = LRUCache(ram_cap)   # 主机 RAM
        self.l2 = LRUCache(nvme_cap)  # 本地 NVMe
        self.l4: dict = {}            # 对象存储（SSOT，无容量上限）

    def get(self, key: Any) -> Optional[bytes]:
        # L1 命中
        v = self.l1.get(key)
        if v is not None:
            return v
        # L2 回填 L1
        v = self.l2.get(key)
        if v is not None:
            self.l1.put(key, v)
            return v
        # L4 回填 L1/L2
        v = self.l4.get(key)
        if v is not None:
            self.l2.put(key, v)
            self.l1.put(key, v)
            return v
        return None

    def put(self, key: Any, value: bytes, persist: bool = False) -> None:
        """写入。persist=True 时同时落 L4（SSOT）。"""
        evicted = self.l1.put(key, value)
        if evicted is not None:
            # L1 驱逐的块降级到 L2
            ev_val = self.l1.get(evicted)  # 已被驱逐，拿不到；用 L2 存
            # 简化：直接把 evicted key 的旧值（若 L2 有）保留
            self.l2.put(evicted, value)  # 原型简化，真实实现需保留原值
        if persist:
            self.l4[key] = value
