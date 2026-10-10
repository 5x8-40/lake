#!/usr/bin/env python3
"""batch_is_exist check for PoolKeys listed in a file (run inside vllm-ascend container).

Usage:
  python3 check_exists.py prefix_keys.txt --master 127.0.0.1:50088 [--device 0]

Prints one line per key: rank / exists / hash tail / replica endpoints.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRECOPY = os.path.join(os.path.dirname(_HERE), "precopy")
if _PRECOPY not in sys.path:
    sys.path.insert(0, _PRECOPY)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("keys_file")
    p.add_argument("--master", default=os.environ.get("MC_MASTER", "127.0.0.1:50088"))
    p.add_argument("--device", type=int, default=0)
    p.add_argument("--protocol", default=os.environ.get("MOONCAKE_PROTOCOL", "ascend"))
    a = p.parse_args()

    keys = [ln.strip() for ln in open(a.keys_file, encoding="utf-8") if ln.strip()]
    if not keys:
        print("no keys in file", file=sys.stderr)
        return 1

    if a.protocol == "ascend":
        import torch
        import torch_npu  # noqa: F401

        torch.npu.set_device(a.device)

    from common import memory_replica_endpoints, setup_store

    local_ip = os.environ.get("LOCAL_IP") or os.environ.get("HCCL_IF_IP") or "127.0.0.1"
    store = setup_store(
        local_hostname=f"{local_ip}:13021",
        master_server_address=a.master,
        global_segment_size=0,
        local_buffer_size=32 * 1024 * 1024,
        protocol=a.protocol,
        device_name="",
    )
    rank_re = re.compile(r"@head_or_tp_rank:(\d+)@")
    try:
        ex = store.batch_is_exist(keys)
        n_ok = 0
        for k, e in zip(keys, ex):
            ok = e == 1 or e is True or (isinstance(e, int) and e > 0)
            n_ok += bool(ok)
            m = rank_re.search(k)
            rank = m.group(1) if m else "?"
            reps = memory_replica_endpoints(store, k) if ok else []
            print(f"rank={rank} exists={1 if ok else 0} hash=..{k[-12:]} replicas={reps}")
        print(f"present={n_ok}/{len(keys)}")
        return 0 if n_ok == len(keys) else 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())