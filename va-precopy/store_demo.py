#!/usr/bin/env python3
"""va-precopy store-only demo (no vLLM / no NPU when protocol=tcp).

Topology:

  mooncake_master
       |
  +----+----+
  seg-A   seg-B
  (source)(target)
       |
  coordinator   create_copy_task (global_segment_size=0)

Flow:
  1. Put key on seg-A (replica_num=1, preferred_segment=A)
  2. Verify A has a memory replica
  3. create_copy_task(key, [B])
  4. Poll query_task -> SUCCESS
  5. batch_get_replica_desc: A and B both present
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import queue
import sys
import time
import uuid
from multiprocessing.queues import Queue as MpQueue
from multiprocessing.synchronize import Event as MpEvent

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from mooncake.store import ReplicateConfig

from common import (
    assert_targets_have_replicas,
    create_copy_and_wait,
    memory_replica_endpoints,
    setup_store,
)


def _client_main(
    hostname: str,
    master: str,
    protocol: str,
    device_name: str,
    segment_size: int,
    ready_q: MpQueue,
    stop_ev: MpEvent,
) -> None:
    store = setup_store(
        local_hostname=hostname,
        master_server_address=master,
        global_segment_size=segment_size,
        local_buffer_size=segment_size * 2,
        protocol=protocol,
        device_name=device_name,
    )
    ready_q.put({"local_hostname": hostname})
    while not stop_ev.is_set():
        time.sleep(0.5)
    _ = store


def main() -> int:
    p = argparse.ArgumentParser(description="va-precopy store-only demo")
    p.add_argument(
        "--master",
        default=os.environ.get("MC_MASTER_ADDRESS", "127.0.0.1:50088"),
        help="mooncake_master host:port",
    )
    p.add_argument(
        "--protocol",
        default=os.environ.get("MOONCAKE_PROTOCOL", "tcp"),
        help="tcp (default for this demo) | rdma | ascend",
    )
    p.add_argument("--device-name", default=os.environ.get("MOONCAKE_DEVICE", ""))
    p.add_argument("--segment-size", type=int, default=16 * 1024 * 1024)
    p.add_argument("--source-segment", default="localhost:10001")
    p.add_argument("--target-segment", default="localhost:10002")
    p.add_argument("--payload-bytes", type=int, default=2 * 1024 * 1024)
    p.add_argument("--key", default="", help="object key; default random")
    args = p.parse_args()

    master = args.master
    if master.count(":") == 0:
        master = f"{master}:50088"

    key = args.key or f"va-precopy-{uuid.uuid4().hex[:12]}"
    source = args.source_segment
    target = args.target_segment
    if source == target:
        print("ERROR: source and target segments must differ", file=sys.stderr)
        return 2

    ctx = mp.get_context("spawn")
    ready_q: MpQueue = ctx.Queue()
    stop_ev: MpEvent = ctx.Event()
    procs = []

    print(f"[demo] master={master} protocol={args.protocol}")
    print(f"[demo] source={source} target={target} key={key}")

    for hostname in (source, target):
        proc = ctx.Process(
            target=_client_main,
            args=(
                hostname,
                master,
                args.protocol,
                args.device_name,
                args.segment_size,
                ready_q,
                stop_ev,
            ),
            daemon=True,
        )
        proc.start()
        procs.append(proc)

    ready = []
    try:
        for _ in (source, target):
            info = ready_q.get(timeout=60)
            ready.append(info["local_hostname"])
            print(f"[demo] segment client ready: {info['local_hostname']}")
    except queue.Empty:
        print("ERROR: segment clients failed to become ready", file=sys.stderr)
        stop_ev.set()
        for proc in procs:
            proc.join(timeout=5)
        return 1

    if set(ready) != {source, target}:
        print(f"ERROR: unexpected clients {ready}", file=sys.stderr)
        stop_ev.set()
        for proc in procs:
            proc.join(timeout=5)
        return 1

    coord = setup_store(
        local_hostname="localhost:12001",
        master_server_address=master,
        global_segment_size=0,
        local_buffer_size=args.segment_size * 2,
        protocol=args.protocol,
        device_name=args.device_name,
    )

    payload = os.urandom(args.payload_bytes)
    cfg = ReplicateConfig()
    cfg.replica_num = 1
    cfg.preferred_segment = source
    rc = coord.put(key, payload, cfg)
    if rc != 0:
        print(f"ERROR: put failed rc={rc}", file=sys.stderr)
        stop_ev.set()
        return 1
    print(f"[demo] put {args.payload_bytes} bytes -> preferred_segment={source}")

    before = memory_replica_endpoints(coord, key)
    print(f"[demo] replicas BEFORE copy: {sorted(before)}")
    if source not in before:
        print(
            f"ERROR: source {source} missing after put; have={before}",
            file=sys.stderr,
        )
        stop_ev.set()
        return 1
    if target in before:
        print(
            f"WARN: target {target} already had a replica before copy; "
            "demo still runs create_copy_task"
        )

    print(f"[demo] create_copy_task({key!r}, [{target!r}]) ...")
    resp = create_copy_and_wait(coord, key, [target])
    print(f"[demo] copy task SUCCESS: {resp}")

    placement = assert_targets_have_replicas(coord, [key], [source, target])
    print(f"[demo] replicas AFTER copy: {placement[key]}")
    print("[demo] PASS - create_copy_task -> local DRAM replica on target")

    stop_ev.set()
    for proc in procs:
        proc.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
