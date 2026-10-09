#!/usr/bin/env python3
"""Build prefix_keys.txt from a warm prompt (vllm-ascend container).

Recomputes engine block hashes with PYTHONHASHSEED-aligned NONE_HASH, then
expands PoolKeys for all head_or_tp_rank (homogeneous TP), optionally checks
Mooncake batch_is_exist.

Requires: transformers + vllm + vllm_ascend (inside vllm-ascend image).
For offline key formatting from known hexes, use keys.py instead.

Examples:
  PYTHONHASHSEED=0 python3 collect_prefix_keys.py \\
      --model /data/models/Qwen3-VL-8B-w8a8c16 \\
      --model-name Qwen3-VL-8B-w8a8c16 \\
      --tp-size 4 \\
      --prefix-repeat 80 \\
      --out prefix_keys.txt \\
      --check-master 127.0.0.1:50088
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from keys import expand_store_keys, group_keys_by_rank


def _hash_prompt(model_path: str, prefix: str, block_size: int) -> tuple[list[str], int]:
    from transformers import AutoTokenizer
    from vllm.utils.hashing import get_hash_fn_by_name
    from vllm.v1.core import kv_cache_utils as ku
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.config_data import (
        block_hash_to_str,
    )

    hash_fn = get_hash_fn_by_name("sha256")
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


def main() -> int:
    p = argparse.ArgumentParser(description="Collect Mooncake prefix keys after warm")
    p.add_argument("--model", required=True, help="HF model path (tokenizer)")
    p.add_argument(
        "--model-name",
        default="",
        help="PoolKey model_name (default: basename of --model)",
    )
    p.add_argument(
        "--prefix",
        default="va-precopy shared prefix for store warmup. ",
        help="Base prefix string before repeat",
    )
    p.add_argument("--prefix-repeat", type=int, default=80)
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument(
        "--tp-size",
        type=int,
        default=int(os.environ.get("TP", "1")),
        help="local TP size (default $TP)",
    )
    p.add_argument(
        "--peer-tp-size",
        type=int,
        default=int(os.environ.get("PEER_TP_SIZE", "0")) or None,
        help="peer TP size for tp_mismatch; effective_tp = max(local, peer)",
    )
    p.add_argument(
        "--put-step",
        type=int,
        default=1,
        help="AscendStore put_step (tp_size//num_kv_head when kv_heads < tp)",
    )
    p.add_argument("--out", default="prefix_keys.txt")
    p.add_argument(
        "--check-master",
        default="",
        help="If set, batch_is_exist against this mooncake_master",
    )
    p.add_argument(
        "--protocol",
        default=os.environ.get("MOONCAKE_PROTOCOL", "ascend"),
    )
    p.add_argument(
        "--coord-hostname",
        default="",
        help="local_hostname for check client (default: LOCAL_IP:13021)",
    )
    p.add_argument("--device", type=int, default=0, help="NPU for ascend setup_store")
    args = p.parse_args()

    if os.environ.get("PYTHONHASHSEED") is None:
        print(
            "[collect_prefix_keys] WARN: PYTHONHASHSEED unset; "
            "engine used PYTHONHASHSEED=0 — set the same or hashes will miss",
            file=sys.stderr,
        )

    model_name = args.model_name or os.path.basename(args.model.rstrip("/"))
    prefix = args.prefix * args.prefix_repeat
    hexes, n_tokens = _hash_prompt(args.model, prefix, args.block_size)
    effective_tp = args.tp_size
    if args.peer_tp_size and args.peer_tp_size != args.tp_size:
        effective_tp = max(args.tp_size, args.peer_tp_size)
    keys = expand_store_keys(
        model_name=model_name,
        chunk_hashes=hexes,
        tp_size=effective_tp,
        put_step=args.put_step,
    )
    by_rank = group_keys_by_rank(keys)
    print(
        f"n_tokens={n_tokens} n_full_blocks={len(hexes)} "
        f"tp_size={args.tp_size} peer_tp_size={args.peer_tp_size} "
        f"effective_tp={effective_tp} put_step={args.put_step} "
        f"ranks={sorted(by_rank)} keys={len(keys)} model_name={model_name}"
    )

    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(keys) + ("\n" if keys else ""))
    print(f"wrote {len(keys)} keys -> {out_path}")
    for r, rkeys in by_rank.items():
        print(f"  rank {r}: {len(rkeys)} keys")

    if not args.check_master:
        return 0

    # Ascend transport needs a real device context.
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
        present = [
            k
            for k, e in zip(keys, ex)
            if e == 1 or e is True or (isinstance(e, int) and e > 0)
        ]
        print(f"exists={ex}")
        print(f"present={len(present)}/{len(keys)}")
        for k in present[: min(8, len(present))]:
            print("replicas", k[-40:], memory_replica_endpoints(store, k))
        if len(present) != len(keys):
            print(
                "[collect_prefix_keys] ERROR: some keys missing in store",
                file=sys.stderr,
            )
            return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[collect_prefix_keys] {type(e).__name__}: {e}", file=sys.stderr)
        raise
