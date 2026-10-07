#!/usr/bin/env python3
"""va-precopy control plane against a live Mooncake + vllm-ascend cluster.

Workers already contribute segments via AscendStoreConnector. This process
does NOT put KV; it only:

  create_copy_task(key, [target_segment]) -> poll -> verify placement

Finish copy BEFORE the target worker opens a get session for the real
request. After READY, send traffic to that worker.

Examples:
  python3 precopy.py --master 127.0.0.1:50088 --protocol ascend \\
      --target "$(hostname):50088" --keys k1,k2

  python3 precopy.py --master 127.0.0.1:50088 --protocol ascend \\
      --target worker-b:50088 --keys-file prefix_keys.txt
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from common import (
    assert_targets_have_replicas,
    create_copy_and_wait,
    memory_replica_endpoints,
    setup_store,
)


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
        required=True,
        help="target segment name (worker local_seg)",
    )
    p.add_argument("--keys", default="", help="comma-separated object keys")
    p.add_argument("--keys-file", default="", help="file with one key per line")
    p.add_argument(
        "--coord-hostname",
        default="va-precopy:0",
        help="local_hostname for this coordinator client (no segment)",
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

    keys = _load_keys(args)
    if not keys:
        print("ERROR: provide --keys and/or --keys-file", file=sys.stderr)
        return 2

    print(f"[precopy] master={master} protocol={args.protocol}")
    print(f"[precopy] target_segment={args.target}")
    print(f"[precopy] keys ({len(keys)}): {keys[:5]}{'...' if len(keys) > 5 else ''}")

    store = setup_store(
        local_hostname=args.coord_hostname,
        master_server_address=master,
        global_segment_size=0,
        local_buffer_size=32 * 1024 * 1024,
        protocol=args.protocol,
        device_name=args.device_name,
    )

    if args.dry_show_before:
        for key in keys:
            print(f"[precopy] BEFORE {key}: {sorted(memory_replica_endpoints(store, key))}")

    for key in keys:
        print(f"[precopy] create_copy_task({key!r}, [{args.target!r}])")
        create_copy_and_wait(store, key, [args.target], timeout_s=args.timeout_s)

    placement = assert_targets_have_replicas(store, keys, [args.target])
    for key in keys:
        print(f"[precopy] AFTER  {key}: {sorted(placement[key])}")

    print("[precopy] READY - target has local DRAM replicas; send traffic to target worker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
