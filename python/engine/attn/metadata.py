"""AttentionMetadata（D4 / C8）。

对照 vLLM `AttentionMetadata` / `AttentionMetadataBuilder`：
runner 填 seq/query 几何；**block table 由 agent 经 ReadyHandle 挂载**，引擎只读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AttentionMetadata:
    """本步 attention 输入（无物理地址权威）。"""

    # 每请求：已有 KV 长度（token），半开上界 = num_computed
    seq_lens: Dict[str, int] = field(default_factory=dict)
    # 每请求：本步 query 区间 [query_start, query_end)
    query_start: Dict[str, int] = field(default_factory=dict)
    query_end: Dict[str, int] = field(default_factory=dict)
    # agent 组装的逻辑 block table（slot 索引）；生产为固定地址 tensor 的镜像/句柄
    block_tables: Dict[str, List[int]] = field(default_factory=dict)
    # 批级：ragged 拼接时的前缀和（预留真批融合）
    query_start_loc: List[int] = field(default_factory=list)
    max_seq_len: int = 0
    max_query_len: int = 0


def build_attn_metadata(
    *,
    seq_lens: Dict[str, int],
    query_start: Dict[str, int],
    query_end: Dict[str, int],
    block_tables: Optional[Dict[str, List[int]]] = None,
    req_order: Optional[List[str]] = None,
) -> AttentionMetadata:
    order = req_order or list(seq_lens.keys())
    qsl = [0]
    max_seq = 0
    max_q = 0
    for rid in order:
        qs = query_start.get(rid, 0)
        qe = query_end.get(rid, qs)
        qsl.append(qsl[-1] + max(0, qe - qs))
        max_seq = max(max_seq, seq_lens.get(rid, 0))
        max_q = max(max_q, max(0, qe - qs))
    return AttentionMetadata(
        seq_lens=dict(seq_lens),
        query_start=dict(query_start),
        query_end=dict(query_end),
        block_tables=dict(block_tables or {}),
        query_start_loc=qsl,
        max_seq_len=max_seq,
        max_query_len=max_q,
    )
