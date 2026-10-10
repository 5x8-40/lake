#!/usr/bin/env python3
"""va-precopy control plane against a live Mooncake + vllm-ascend cluster.

Workers already contribute segments via AscendStoreConnector. This process
does NOT put KV; it only:

  create_copy_task(key, [target_segment]) -> poll -> verify placement

Homogeneous TP: use --targets seg0,seg1,... so head_or_tp_rank:i keys go only
to targets[i] (not the same key broadcast to every seg).

Finish copy BEFORE the target worker opens a get session for the real
request. After READY, send traffic to that worker.

Single entry (prompt mode): hand it the warm prompt — key computation
(import vllm/vllm-ascend), batch_is_exist check, copy and READY all happen
in-process; keys never touch a file unless --dump-keys is given:

  python3 precopy.py --master 127.0.0.1:50088 --protocol ascend \\
      --targets "B:p0,B:p1" \\
      --model /data/models/Qwen3-VL-8B-w8a8c16 \\
      --prefix "shared prefix. " --prefix-repeat 80 --tp-size 2

Debug paths (keys from outside): --keys / --keys-file, or standalone
collect_prefix_keys.py + test_keys.py.

Examples:
  # TP=1
  python3 precopy.py --master 127.0.0.1:50088 --protocol ascend \\
      --target "$(hostname):50088" --keys-file prefix_keys.txt

  # TP=N (rank i → targets[i])
  python3 precopy.py --master 127.0.0.1:50088 --protocol ascend \\
      --targets "B:p0,B:p1,B:p2,B:p3" --keys-file prefix_keys.txt
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from keys import group_keys_by_rank, parse_head_or_tp_rank

# NOTE: common (mooncake client) is imported lazily in main() AFTER arg
# validation and key computation, so --dump-keys and usage errors work
# without a mooncake install.


def _load_keys(args: argparse.Namespace) -> list[str]:
    keys: list[str] = []
    if args.keys:
        keys.extend(k.strip() for k in args.keys.split(",") if k.strip())
    if args.keys_file:
        with open(args.keys_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                keys.append(line)
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _parse_targets(args: argparse.Namespace) -> list[str]:
    if args.targets:
        targets = [t.strip() for t in args.targets.split(",") if t.strip()]
        if not targets:
            raise SystemExit("ERROR: --targets is empty")
        return targets
    if args.target:
        return [args.target]
    raise SystemExit("ERROR: provide --target (TP=1) or --targets (homogeneous TP)")


def main() -> int:
    p = argparse.ArgumentParser(description="va-precopy: Mooncake create_copy_task")
    p.add_argument(
        "--master",
        default=os.environ.get("MC_MASTER_ADDRESS", "127.0.0.1:50088"),
    )
    p.add_argument(
        "--protocol",
        default=os.environ.get("MOONCAKE_PROTOCOL", "ascend"),
        help="Must match mooncake.json (NPU cluster: ascend)",
    )
    p.add_argument("--device-name", default=os.environ.get("MOONCAKE_DEVICE", ""))
    p.add_argument(
        "--target",
        default="",
        help="single target segment (TP=1); all keys copy here",
    )
    p.add_argument(
        "--targets",
        default=os.environ.get("TARGET_SEGMENTS", ""),
        help="comma-separated B local_seg list; key rank i → targets[i]",
    )
    p.add_argument("--keys", default="", help="comma-separated object keys (debug)")
    p.add_argument("--keys-file", default="", help="file with one key per line (debug)")
    # Prompt mode: compute keys in-process from the warm prompt (product path).
    p.add_argument("--prefix", default=os.environ.get("PREFIX", ""),
                   help="base warm prompt string (needs --model)")
    p.add_argument("--prefix-repeat", type=int, default=int(os.environ.get("PREFIX_REPEAT", "1")))
    p.add_argument("--model", default=os.environ.get("MODEL", ""),
                   help="HF model path for tokenizer (prompt mode)")
    p.add_argument("--model-name", default=os.environ.get("MODEL_NAME", ""),
                   help="PoolKey model_name (default: basename of --model)")
    p.add_argument("--block-size", type=int, default=int(os.environ.get("BLOCK_SIZE", "128")))
    p.add_argument("--tp-size", type=int, default=int(os.environ.get("TP_SIZE", os.environ.get("TP", "1"))),
                   help="target (B) TP size for rank expansion")
    p.add_argument("--peer-tp-size", type=int,
                   default=int(os.environ.get("PEER_TP_SIZE", "0")) or None,
                   help="source (A) TP size for tp_mismatch; effective_tp=max(local,peer)")
    p.add_argument("--put-step", type=int, default=int(os.environ.get("PUT_STEP", "1")))
    p.add_argument("--hash-algo", default=os.environ.get("PREFIX_CACHING_HASH_ALGO", "sha256"),
                   help="must match engine prefix_caching_hash_algo")
    p.add_argument("--dump-keys", default="",
                   help="optional: write computed keys to this file (debug artifact)")
    p.add_argument("--skip-exist-check", action="store_true",
                   help="prompt mode: skip batch_is_exist before copy")
    p.add_argument(
        "--coord-hostname",
        default="",
        help="local_hostname for coordinator client (default: LOCAL_IP/HCCL_IF_IP:13021)",
    )
    p.add_argument(
        "--device",
        type=int,
        default=int(os.environ.get("PRECOPY_DEVICE", "0")),
        help="NPU id for ascend protocol setup_store (torch.npu.set_device)",
    )
    p.add_argument("--timeout-s", type=float, default=120.0)
    p.add_argument(
        "--dry-show-before",
        action="store_true",
        help="print replica placement before copy",
    )
    args = p.parse_args()

    master = args.master
    if master.count(":") == 0:
        master = f"{master}:50088"

    computed = bool(args.prefix and args.model)
    if computed and (args.keys or args.keys_file):
        print(
            "ERROR: --prefix/--model (compute) and --keys/--keys-file (debug) "
            "are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if computed:
        from collect_prefix_keys import collect_keys

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
        print("[precopy] collect: " + " ".join(f"{k}={v}" for k, v in info.items()))
        if args.dump_keys:
            with open(args.dump_keys, "w", encoding="utf-8") as f:
                f.write("\n".join(keys) + "\n")
            print(f"[precopy] dumped {len(keys)} keys -> {args.dump_keys}")
    else:
        keys = _load_keys(args)
    if not keys:
        print(
            "ERROR: provide --keys/--keys-file (debug) or --prefix + --model (compute)",
            file=sys.stderr,
        )
        return 2

    targets = _parse_targets(args)
    by_rank = group_keys_by_rank(keys)
    max_rank = max(by_rank) if by_rank else -1
    if len(targets) == 1 and max_rank <= 0:
        # TP=1 / single-seg: every key → the one target
        plan: list[tuple[str, str]] = [(k, targets[0]) for k in keys]
    else:
        if max_rank >= len(targets):
            print(
                f"ERROR: key head_or_tp_rank max={max_rank} but only "
                f"{len(targets)} --targets (need targets[0..{max_rank}])",
                file=sys.stderr,
            )
            return 2
        unknown = [r for r in by_rank if r < 0 or r >= len(targets)]
        if unknown:
            print(f"ERROR: ranks out of range for --targets: {unknown}", file=sys.stderr)
            return 2
        plan = [(k, targets[parse_head_or_tp_rank(k)]) for k in keys]

    local_ip = (
        os.environ.get("LOCAL_IP")
        or os.environ.get("HCCL_IF_IP")
        or ""
    )
    coord = args.coord_hostname or (
        f"{local_ip}:13021" if local_ip else "va-precopy:0"
    )

    print(f"[precopy] master={master} protocol={args.protocol}")
    print(f"[precopy] targets ({len(targets)}): {targets}")
    print(f"[precopy] coord_hostname={coord}")
    print(
        f"[precopy] keys={len(keys)} ranks={sorted(by_rank)} "
        f"plan={len(plan)} (rank i → targets[i]; serial per key)"
    )
    for r, rkeys in by_rank.items():
        seg = targets[r] if r < len(targets) else "?"
        print(f"[precopy]   rank {r} → {seg}: {len(rkeys)} keys")

    if args.protocol == "ascend":
        import torch
        import torch_npu  # noqa: F401

        torch.npu.set_device(args.device)
        print(f"[precopy] torch.npu.set_device({args.device})")

    from common import (
        assert_targets_have_replicas,
        create_copy_and_wait,
        memory_replica_endpoints,
        setup_store,
    )

    store = setup_store(
        local_hostname=coord,
        master_server_address=master,
        global_segment_size=0,
        local_buffer_size=32 * 1024 * 1024,
        protocol=args.protocol,
        device_name=args.device_name,
    )

    try:
        if computed and not args.skip_exist_check:
            # Same batch_is_exist semantics as collect --check-master: computed
            # keys must ALL be in the pool (warm covered the full prefix, hash
            # algo matches engine, key format not drifted) or copying is moot.
            ex = store.batch_is_exist(keys)
            n_ok = sum(1 for e in ex if e == 1 or e is True or (isinstance(e, int) and e > 0))
            print(f"[precopy] exist check: {n_ok}/{len(keys)}")
            if n_ok != len(keys):
                print(
                    "[precopy] ERROR: keys missing in pool — warm 未覆盖该前缀，"
                    "或 hash-algo/key 格式与引擎漂移",
                    file=sys.stderr,
                )
                return 2

        if args.dry_show_before:
            for key in keys:
                print(
                    f"[precopy] BEFORE {key}: "
                    f"{sorted(memory_replica_endpoints(store, key))}"
                )

        # Still serial key-by-key (parallelism TBD); mapping is per-rank.
        for key, target in plan:
            print(f"[precopy] create_copy_task({key!r}, [{target!r}])")
            create_copy_and_wait(store, key, [target], timeout_s=args.timeout_s)

        # Verify each key only on its mapped target (not every seg).
        for key, target in plan:
            assert_targets_have_replicas(store, [key], [target])
            print(
                f"[precopy] AFTER  {key}: "
                f"{sorted(memory_replica_endpoints(store, key))}"
            )

        print(
            "[precopy] READY - each rank's keys on its B local_seg; "
            "send traffic to target worker",
            flush=True,
        )
    finally:
        # Orderly teardown while the interpreter is healthy. Without close(),
        # cleanup is deferred to exit-time GC/atexit, which intermittently
        # aborts (RC=134) or hangs on mooncake <0.3.12 (teardown races with
        # in-flight RPC/transfer threads; cf. upstream #3909/#3943).
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
