#!/usr/bin/env python3
"""Build Mooncake object keys matching vllm-ascend AscendStoreConnector.

Key string format is owned by vllm-ascend ``PoolKey.to_string()`` /
``LayerPoolKey.to_string()`` in ``config_data.py`` (0.26). This module
reimplements that format in pure Python so the prefetcher does not
import torch / vllm.

Prefer importing the real helpers when running inside a vllm-ascend
container (``--prefer-upstream``). Offline / no-NPU: use this builder
with known block-hash hex strings.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class KeySpec:
    model_name: str
    chunk_hash: str
    head_or_tp_rank: int = 0
    pcp_rank: int = 0
    dcp_rank: int = 0
    pp_rank: int = 0
    kv_cache_group_id: int = 0
    cache_role: str = "kv"
    cache_family: str = "default"
    layer_id: int | None = None

    def to_string(self) -> str:
        # Mirror vllm_ascend...config_data.PoolKey / LayerPoolKey.to_string
        if self.layer_id is None:
            return (
                f"{self.model_name}"
                f"@pcp:{self.pcp_rank}@dcp:{self.dcp_rank}"
                f"@head_or_tp_rank:{self.head_or_tp_rank}"
                f"@pp_rank:{self.pp_rank}"
                f"@group:{self.kv_cache_group_id}"
                f"@cache_role:{self.cache_role}"
                f"@cache_family:{self.cache_family}"
                f"@{self.chunk_hash}"
            )
        return (
            f"{self.model_name}"
            f"@pcp:{self.pcp_rank}@dcp:{self.dcp_rank}"
            f"@head_or_tp_rank:{self.head_or_tp_rank}"
            f"@group:{self.kv_cache_group_id}"
            f"@cache_role:{self.cache_role}"
            f"@cache_family:{self.cache_family}"
            f"@layer_id:{self.layer_id}"
            f"@{self.chunk_hash}"
        )


def expand_store_keys(
    *,
    model_name: str,
    chunk_hashes: list[str],
    tp_size: int = 1,
    put_step: int = 1,
    pcp_size: int = 1,
    dcp_size: int = 1,
    pp_size: int = 1,
    include_layers: bool = False,
    num_layers: int = 0,
    kv_cache_group_id: int = 0,
    cache_family: str = "default",
) -> list[str]:
    """Same enumeration as ``PoolScheduler._generate_store_query_keys``."""
    head_or_tp_ranks = max(tp_size // max(put_step, 1), 1)
    keys: list[str] = []
    for chunk_hash in chunk_hashes:
        for pcp_rank in range(pcp_size):
            for dcp_rank in range(dcp_size):
                for head_or_tp_rank in range(head_or_tp_ranks):
                    for pp_rank in range(pp_size):
                        base = KeySpec(
                            model_name=model_name,
                            chunk_hash=chunk_hash,
                            head_or_tp_rank=head_or_tp_rank,
                            pcp_rank=pcp_rank,
                            dcp_rank=dcp_rank,
                            pp_rank=pp_rank,
                            kv_cache_group_id=kv_cache_group_id,
                            cache_family=cache_family,
                        )
                        if include_layers:
                            if num_layers <= 0:
                                raise ValueError(
                                    "include_layers requires num_layers > 0"
                                )
                            for layer_id in range(num_layers):
                                # LayerPoolKey.to_string omits pp_rank
                                keys.append(
                                    KeySpec(
                                        model_name=model_name,
                                        chunk_hash=chunk_hash,
                                        head_or_tp_rank=head_or_tp_rank,
                                        pcp_rank=pcp_rank,
                                        dcp_rank=dcp_rank,
                                        pp_rank=0,
                                        kv_cache_group_id=kv_cache_group_id,
                                        cache_family=cache_family,
                                        layer_id=layer_id,
                                    ).to_string()
                                )
                        else:
                            keys.append(base.to_string())
    return keys


def try_upstream_expand(args: argparse.Namespace) -> list[str] | None:
    """Optional: call real vllm-ascend helpers when installed."""
    try:
        from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.config_data import (  # type: ignore
            KeyMetadata,
            PoolKey,
        )
    except Exception:
        return None

    head_or_tp_ranks = max(args.tp_size // max(args.put_step, 1), 1)
    keys: list[str] = []
    for chunk_hash in args.chunk_hashes:
        for pcp_rank in range(args.pcp_size):
            for dcp_rank in range(args.dcp_size):
                for head_or_tp_rank in range(head_or_tp_ranks):
                    for pp_rank in range(args.pp_size):
                        pk = PoolKey(
                            KeyMetadata(
                                args.model_name,
                                head_or_tp_rank,
                                pcp_rank,
                                dcp_rank,
                                pp_rank,
                                kv_cache_group_id=args.group_id,
                                cache_family=args.cache_family,
                            ),
                            chunk_hash,
                        )
                        if args.include_layers:
                            keys.extend(
                                lk.to_string()
                                for lk in pk.split_layers(args.num_layers)
                            )
                        else:
                            keys.append(pk.to_string())
    return keys


def main() -> int:
    p = argparse.ArgumentParser(description="Emit AscendStore Mooncake keys")
    p.add_argument("--model-name", required=True, help="served model name in keys")
    p.add_argument(
        "--chunk-hashes",
        required=True,
        help="comma-separated block hash hex (no 0x prefix)",
    )
    p.add_argument("--tp-size", type=int, default=1)
    p.add_argument("--put-step", type=int, default=1)
    p.add_argument("--pcp-size", type=int, default=1)
    p.add_argument("--dcp-size", type=int, default=1)
    p.add_argument("--pp-size", type=int, default=1)
    p.add_argument("--group-id", type=int, default=0)
    p.add_argument("--cache-family", default="default")
    p.add_argument("--include-layers", action="store_true")
    p.add_argument("--num-layers", type=int, default=0)
    p.add_argument("--prefer-upstream", action="store_true")
    p.add_argument("--out", default="", help="write keys one per line; default stdout")
    p.add_argument("--json", action="store_true", help="print JSON array")
    args = p.parse_args()
    args.chunk_hashes = [h.strip() for h in args.chunk_hashes.split(",") if h.strip()]

    keys: list[str] | None = None
    if args.prefer_upstream:
        keys = try_upstream_expand(args)
        if keys is None:
            print(
                "WARN: vllm_ascend not importable; using local PoolKey mirror",
                file=sys.stderr,
            )
    if keys is None:
        keys = expand_store_keys(
            model_name=args.model_name,
            chunk_hashes=args.chunk_hashes,
            tp_size=args.tp_size,
            put_step=args.put_step,
            pcp_size=args.pcp_size,
            dcp_size=args.dcp_size,
            pp_size=args.pp_size,
            include_layers=args.include_layers,
            num_layers=args.num_layers,
            kv_cache_group_id=args.group_id,
            cache_family=args.cache_family,
        )

    if args.json:
        text = json.dumps(keys, ensure_ascii=False, indent=2)
    else:
        text = "\n".join(keys) + ("\n" if keys else "")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {len(keys)} keys -> {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
