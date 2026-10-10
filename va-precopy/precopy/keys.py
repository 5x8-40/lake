#!/usr/bin/env python3
"""Key computation for va-precopy: prompt -> block hashes -> PoolKey strings.

Everything about the key format or hash algorithm is IMPORTED from upstream,
nothing reimplemented:

- block hash chain: vllm ``kv_cache_utils.hash_block_tokens`` +
  ``get_hash_fn_by_name`` (``_hash_prompt``; container-only)
- PoolKey string format: vllm-ascend ``PoolKey.to_string()``
  (rc1: ``.../ascend_store/config_data.py``; main: ``.../ascend_store/metadata.py``)

Inside a vllm-ascend container this module IMPORTS the upstream classes and
constructs keys **by field name** (``dataclasses.fields``), so upstream format
changes — e.g. rc1 ``KeyMetadata.pcp_rank`` / ``@pcp:`` in to_string, both
removed on main — are followed automatically instead of silently drifting.

Offline (vllm_ascend not importable): falls back to the built-in rc1 mirror
``KeySpec`` with a loud stderr warning. Drift sentinels in-container:
``--check-master`` (batch_is_exist must be 100%) plus
``tools/test_keys.py::test_upstream_parity``.

CLI input modes (mutually exclusive):
  --model PATH --prefix ...   prompt mode: tokenize + hash chain + expand
  --chunk-hashes h0,h1,...    offline: expand known hexes
  --keys-file FILE            post-hoc forensics: full PoolKey strings from a
                              dump file; only meaningful with --check-master
"""

from __future__ import annotations

import argparse
import json
import os
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


# ---------------------------------------------------------------------------
# Prompt mode: hash chain imported from vllm (container-only)
# ---------------------------------------------------------------------------


def _hash_prompt(
    model_path: str, prefix: str, block_size: int, hash_algo: str
) -> tuple[list[str], int]:
    from transformers import AutoTokenizer
    from vllm.utils.hashing import get_hash_fn_by_name
    from vllm.v1.core import kv_cache_utils as ku

    try:  # v0.26 rc1/rc2 layout
        from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.config_data import (
            block_hash_to_str,
        )
    except ImportError:  # main layout (config_data.py renamed metadata.py)
        from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.metadata import (
            block_hash_to_str,
        )

    hash_fn = get_hash_fn_by_name(hash_algo)
    ku.init_none_hash(hash_fn)

    tok = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    ids = tok.encode(prefix)
    hashes: list = []
    parent = None
    n_full = (len(ids) // block_size) * block_size
    for i in range(0, n_full, block_size):
        chunk = ids[i : i + block_size]
        h = ku.hash_block_tokens(hash_fn, parent, chunk, None)
        hashes.append(h)
        parent = h
    return [block_hash_to_str(h) for h in hashes], len(ids)


def collect_keys(
    *,
    model_path: str,
    prefix: str,
    model_name: str = "",
    block_size: int = 128,
    tp_size: int = 1,
    peer_tp_size: int | None = None,
    put_step: int = 1,
    hash_algo: str = "sha256",
) -> tuple[list[str], dict]:
    """prompt -> expanded PoolKey list (in-process API; precopy.py calls this).

    Returns (keys, info). Hash chain and PoolKey format are imported from
    vllm / vllm-ascend — nothing reimplemented. Container-only (needs
    transformers + vllm + vllm_ascend).
    """
    model_name = model_name or os.path.basename(model_path.rstrip("/"))
    hexes, n_tokens = _hash_prompt(model_path, prefix, block_size, hash_algo)
    effective_tp = tp_size
    if peer_tp_size and peer_tp_size != tp_size:
        effective_tp = max(tp_size, peer_tp_size)
    keys = expand_store_keys(
        model_name=model_name,
        chunk_hashes=hexes,
        tp_size=effective_tp,
        put_step=put_step,
    )
    info = {
        "n_tokens": n_tokens,
        "n_full_blocks": len(hexes),
        "tp_size": tp_size,
        "peer_tp_size": peer_tp_size,
        "effective_tp": effective_tp,
        "put_step": put_step,
        "model_name": model_name,
        "hash_algo": hash_algo,
        "key_format": (
            "upstream(vllm_ascend.PoolKey)"
            if upstream_key_classes() is not None
            else "builtin-mirror(OFFLINE, drift risk!)"
        ),
    }
    return keys, info


def _check_master(keys: list[str], args: argparse.Namespace) -> int:
    """batch_is_exist all keys against the pool (drift sentinel / forensics).

    Prints one line per key: rank / exists / hash tail / replica endpoints."""
    if args.protocol == "ascend":
        import torch
        import torch_npu  # noqa: F401

        torch.npu.set_device(args.device)

    from common import memory_replica_endpoints, setup_store

    local_ip = os.environ.get("LOCAL_IP") or os.environ.get("HCCL_IF_IP") or "127.0.0.1"
    host = args.coord_hostname or f"{local_ip}:13021"
    store = setup_store(
        local_hostname=host,
        master_server_address=args.check_master,
        global_segment_size=0,
        local_buffer_size=32 * 1024 * 1024,
        protocol=args.protocol,
        device_name="",
    )
    try:
        ex = store.batch_is_exist(keys)
        n_ok = 0
        for k, e in zip(keys, ex):
            ok = e == 1 or e is True or (isinstance(e, int) and e > 0)
            n_ok += bool(ok)
            try:
                rank = str(parse_head_or_tp_rank(k))
            except ValueError:
                rank = "?"
            reps = memory_replica_endpoints(store, k) if ok else []
            print(f"rank={rank} exists={1 if ok else 0} hash=..{k[-12:]} replicas={reps}")
        print(f"present={n_ok}/{len(keys)}")
        if n_ok != len(keys):
            print("[keys] ERROR: some keys missing in store", file=sys.stderr)
            return 2
    finally:
        store.close()
    return 0


def _load_keys_file(path: str) -> list[str]:
    """Full PoolKey strings from a dump file (one per line; # comments ok)."""
    keys: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                keys.append(line)
    return keys


def main() -> int:
    p = argparse.ArgumentParser(description="Compute AscendStore Mooncake keys")
    # Input mode (exactly one required)
    p.add_argument(
        "--chunk-hashes",
        default="",
        help="offline mode: comma-separated block hash hex (no 0x prefix)",
    )
    p.add_argument("--model", default="", help="prompt mode: HF model path (tokenizer)")
    p.add_argument(
        "--keys-file",
        default="",
        help="forensics mode: file with one full PoolKey per line (use with --check-master)",
    )
    p.add_argument(
        "--prefix",
        default="va-precopy shared prefix for store warmup. ",
        help="prompt mode: base prefix string before repeat",
    )
    p.add_argument("--prefix-repeat", type=int, default=80)
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument(
        "--hash-algo",
        default=os.environ.get("PREFIX_CACHING_HASH_ALGO", "sha256"),
        help="must match engine prefix_caching_hash_algo (vllm default: sha256)",
    )
    # Key expansion
    p.add_argument(
        "--model-name",
        default="",
        help="PoolKey model_name (default: basename of --model)",
    )
    p.add_argument("--tp-size", type=int, default=int(os.environ.get("TP", "1")))
    p.add_argument(
        "--peer-tp-size",
        type=int,
        default=int(os.environ.get("PEER_TP_SIZE", "0")) or None,
        help="peer TP size for tp_mismatch; effective_tp = max(local, peer)",
    )
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
    # Output / verification
    p.add_argument("--out", default="", help="write keys one per line; default stdout")
    p.add_argument("--json", action="store_true", help="print JSON array")
    p.add_argument(
        "--check-master",
        default="",
        help="if set, batch_is_exist all keys against this mooncake_master",
    )
    p.add_argument("--protocol", default=os.environ.get("MOONCAKE_PROTOCOL", "ascend"))
    p.add_argument(
        "--coord-hostname",
        default="",
        help="local_hostname for check client (default: LOCAL_IP:13021)",
    )
    p.add_argument("--device", type=int, default=0, help="NPU for ascend setup_store")
    args = p.parse_args()

    prompt_mode = bool(args.model)
    file_mode = bool(args.keys_file)
    if sum([prompt_mode, bool(args.chunk_hashes), file_mode]) != 1:
        print(
            "ERROR: exactly one of --model (prompt) / --chunk-hashes (offline) / --keys-file (forensics)",
            file=sys.stderr,
        )
        return 2
    if prompt_mode and not args.model_name:
        args.model_name = os.path.basename(args.model.rstrip("/"))
    if not file_mode and not args.model_name:
        print("ERROR: --model-name required in --chunk-hashes mode", file=sys.stderr)
        return 2
    if args.hash_algo == "builtin" and os.environ.get("PYTHONHASHSEED") is None:
        print(
            "[keys] WARN: hash-algo=builtin but PYTHONHASHSEED unset; "
            "engine used PYTHONHASHSEED=0 — set the same or hashes will miss",
            file=sys.stderr,
        )

    if prompt_mode:
        keys, info = collect_keys(
            model_path=args.model,
            model_name=args.model_name,
            prefix=args.prefix * args.prefix_repeat,
            block_size=args.block_size,
            tp_size=args.tp_size,
            peer_tp_size=args.peer_tp_size,
            put_step=args.put_step,
            hash_algo=args.hash_algo,
        )
        print(" ".join(f"{k}={v}" for k, v in info.items()), file=sys.stderr)
    elif file_mode:
        keys = _load_keys_file(args.keys_file)
        if not keys:
            print(f"ERROR: no keys in {args.keys_file}", file=sys.stderr)
            return 2
    else:
        keys = expand_store_keys(
            model_name=args.model_name,
            chunk_hashes=[h.strip() for h in args.chunk_hashes.split(",") if h.strip()],
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

    by_rank = group_keys_by_rank(keys)
    print(f"keys={len(keys)} ranks={sorted(by_rank)}", file=sys.stderr)
    for r, rkeys in by_rank.items():
        print(f"  rank {r}: {len(rkeys)} keys", file=sys.stderr)

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

    if args.check_master:
        return _check_master(keys, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
