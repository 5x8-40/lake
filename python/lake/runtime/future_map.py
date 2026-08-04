"""FutureMap 等价物（D10 骨架）：device 侧 token 接力的 host mock。

参考:SGLang `managers/overlap_utils.py::FutureMap`（`stash` / `publish` / `output_tokens_buf`）。
生产路径：GPU buffer + gather；C1 仅在 host 字典上验证 overlap 时序契约。
不中继整份 Req——只中继 token ids。
"""

from __future__ import annotations

from typing import Dict, Optional


class FutureMap:
    def __init__(self) -> None:
        self._staged: Dict[str, int] = {}
        self._buf: Dict[str, int] = {}

    def stash(self, req_id: str, token_id: int) -> None:
        self._staged[req_id] = token_id

    def publish(self) -> None:
        self._buf.update(self._staged)
        self._staged.clear()

    def resolve(self, req_id: str) -> Optional[int]:
        return self._buf.get(req_id)

    def clear(self, req_id: str) -> None:
        self._staged.pop(req_id, None)
        self._buf.pop(req_id, None)
