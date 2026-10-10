#!/usr/bin/env python3
"""Build Mooncake object keys matching vllm-ascend AscendStoreConnector.

Key string format is OWNED by vllm-ascend ``PoolKey.to_string()``
(rc1: ``.../ascend_store/config_data.py``; main: ``.../ascend_store/metadata.py``).
Inside a vllm-ascend container this module IMPORTS the upstream classes and
constructs keys **by field name** (``dataclasses.fields``), so upstream format
changes — e.g. rc1 ``KeyMetadata.pcp_rank`` / ``@pcp:`` in to_string, both
removed on main — are followed automatically instead of silently drifting.

Offline (vllm_ascend not importable): falls back to the built-in rc1 mirror
``KeySpec`` with a loud stderr warning. Drift sentinel in-container:
``collect_prefix_keys.py --check-master`` (batch_is_exist must be 100%),
plus ``test_keys.py::test_upstream_parity``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass

_HEAD_OR_TP_RANK_RE = re.compile(r"@head_or_tp_rank:(\d+)@")


def parse_head_or_tp_rank(key: str) -> int:
    """Extract head_or_tp_rank from an AscendStore PoolKey string."""
    m = _HEAD_OR_TP_RANK_RE.search(key)
    if not m:
        raise ValueError(f"key missing @head_or_tp_rank:<n>@: {key!r}")
    return int(m.group(1))


def group_keys_by_rank(keys: list[str]) -> dict[int, list[str]]:
    """Group object keys by head_or_tp_rank (stable order within each rank)."""
    out: dict[int, list[str]] = defaultdict(list)
    for k in keys:
        out[parse_head_or_tp_rank(k)].append(k)
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Upstream import (preferred path)
# ---------------------------------------------------------------------------

_UPSTREAM: tuple | bool | None = None  # (KeyMetadata, PoolKey) | False


def upstream_key_classes() -> tuple | None:
    """Lazy-import vllm-ascend (KeyMetadata, PoolKey); None when unavailable."""
    global _UPSTREAM
    if _UPSTREAM is not None:
        return _UPSTREAM or None
    try:
        try:  # v0.26 rc1/rc2 layout
            from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.config_data import (
                KeyMetadata,
                PoolKey,
            )
        except ImportError:  # main layout (config_data.py renamed metadata.py)
            from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.metadata import (
                KeyMetadata,
                PoolKey,
            )
        _UPSTREAM = (KeyMetadata, PoolKey)
    except Exception:
        _UPSTREAM = False
    return _UPSTREAM or None


def _make_keys_upstream(
    model_name: str,
    chunk_hash: str,
    *,
    head_or_tp_rank: int = 0,
    pcp_rank: int = 0,
    dcp_rank: int = 0,
    pp_rank: int = 0,
    kv_cache_group_id: int = 0,
    cache_role: str = "kv",
    cache_family: str = "default",
    num_layers: int = 0,
) -> list[str]:
    """Construct via upstream PoolKey, passing only fields this version has.

    rc1 KeyMetadata has ``pcp_rank`` (and ``@pcp:`` in to_string); main does
    not. Filtering by ``dataclasses.fields`` keeps both working.
    """
    from dataclasses import fields

    KeyMetadata, PoolKey = upstream_key_classes()  # type: ignore[misc]
    known = {f.name for f in fields(KeyMetadata)}
    wanted = {
        "model_name": model_name,
        "head_or_tp_rank": head_or_tp_rank,
        "pcp_rank": pcp_rank,
        "dcp_rank": dcp_rank,
        "pp_rank": pp_rank,
        "kv_cache_group_id": kv_cache_group_id,
        "cache_role": cache_role,
        "cache_family": cache_family,
    }
    km = KeyMetadata(**{k: v for k, v in wanted.items() if k in known})
    pk = PoolKey(km, chunk_hash)
    if num_layers > 0:
        return [lk.to_string() for lk in pk.split_layers(num_layers)]
    return [pk.to_string()]


# ---------------------------------------------------------------------------
# Offline mirror (fallback only; rc1 format)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySpec:
    """Pure-python rc1-format mirror. OFFLINE FALLBACK ONLY — the container
    path imports upstream PoolKey instead. If upstream changes the format,
    this mirror drifts; test_keys.py::test_upstream_parity goes red."""

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
        # Mirror of rc1 config_data.PoolKey / LayerPoolKey.to_string
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


_WARNED_MIRROR = False


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
    prefer_upstream: bool = True,
) -> list[str]:
    """Same enumeration as ``PoolScheduler._generate_store_query_keys``.

    Key strings come from upstream PoolKey when importable (default);
    the built-in rc1 mirror is only an offline fallback.
    """
    global _WARNED_MIRROR
    if include_layers and num_layers <= 0:
        raise ValueError("include_layers requires num_layers > 0")
    use_upstream = prefer_upstream and upstream_key_classes() is not None
    if prefer_upstream and not use_upstream and not _WARNED_MIRROR:
        print(
            "WARN: vllm_ascend not importable; using built-in rc1 key mirror "
            "(OFFLINE fallback — format may drift from engine)",
            file=sys.stderr,
        )
        _WARNED_MIRROR = True
    head_or_tp_ranks = max(tp_size // max(put_step, 1), 1)
    keys: list[str] = []
    for chunk_hash in chunk_hashes:
        for pcp_rank in range(pcp_size):
            for dcp_rank in range(dcp_size):
                for head_or_tp_rank in range(head_or_tp_ranks):
                    for pp_rank in range(pp_size):
                        if use_upstream:
                            keys.extend(
                                _make_keys_upstream(
                                    model_name,
                                    chunk_hash,
                                    head_or_tp_rank=head_or_tp_rank,
                                    pcp_rank=pcp_rank,
                                    dcp_rank=dcp_rank,
                                    pp_rank=pp_rank,
                                    kv_cache_group_id=kv_cache_group_id,
                                    cache_family=cache_family,
                                    num_layers=num_layers if include_layers else 0,
                                )
                            )
                        elif include_layers:
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
                            keys.append(
                                KeySpec(
                                    model_name=model_name,
                                    chunk_hash=chunk_hash,
                                    head_or_tp_rank=head_or_tp_rank,
                                    pcp_rank=pcp_rank,
                                    dcp_rank=dcp_rank,
                                    pp_rank=pp_rank,
                                    kv_cache_group_id=kv_cache_group_id,
                                    cache_family=cache_family,
                                ).to_string()
                            )
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
    p.add_argument(
        "--prefer-upstream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="import vllm-ascend PoolKey (default; --no-prefer-upstream forces mirror)",
    )
    p.add_argument("--out", default="", help="write keys one per line; default stdout")
    p.add_argument("--json", action="store_true", help="print JSON array")
    args = p.parse_args()
    args.chunk_hashes = [h.strip() for h in args.chunk_hashes.split(",") if h.strip()]

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
        prefer_upstream=args.prefer_upstream,
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
