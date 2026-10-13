#!/usr/bin/env python3
"""va-precopy store-only demo (no vLLM).

On stock Mooncake, ``protocol=tcp`` can run without NPU.
On vllm-ascend's Ascend-built Mooncake, AscendDirectTransport is always
installed: use ``protocol=ascend``, a real IP (not localhost), and bind an
NPU in each process (``torch.npu.set_device``).

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
import socket
import sys
import time
import uuid
from multiprocessing.queues import Queue as MpQueue
from multiprocessing.synchronize import Event as MpEvent

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRECOPY = os.path.join(os.path.dirname(_HERE), "precopy")
if _PRECOPY not in sys.path:
    sys.path.insert(0, _PRECOPY)

from mooncake.store import ReplicateConfig

from common import (
    assert_targets_have_replicas,
    create_copy_and_wait,
    memory_replica_endpoints,
    setup_store,
)


def _host_ip() -> str:
    env = os.environ.get("HOST_IP", "").strip()
    if env:
        return env
    # Prefer a non-loopback address; Ascend AdxlEngine rejects "localhost".
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if not ip.startswith("127."):
            return ip
    return "127.0.0.1"


def _bind_npu(
    visible_device: str | None,
    *,
    hccl_port_range: str | None = None,
) -> None:
    """Bind one physical NPU for Ascend Mooncake (logical device 0)."""
    if not visible_device:
        return
    os.environ["ASCEND_RT_VISIBLE_DEVICES"] = str(visible_device)
    # Multi-process on one host: each client needs a distinct HCCL socket range
    # or AdxlEngine fails with EI0020 (port already bound, often 16666).
    if hccl_port_range:
        os.environ["HCCL_NPU_SOCKET_PORT_RANGE"] = hccl_port_range
    import torch
    import torch_npu  # noqa: F401

    torch.npu.set_device(0)


def _client_main(
    hostname: str,
    master: str,
    protocol: str,
    device_name: str,
    segment_size: int,
    visible_device: str | None,
    hccl_port_range: str | None,
    ready_q: MpQueue,
    stop_ev: MpEvent,
) -> None:
    try:
        _bind_npu(visible_device, hccl_port_range=hccl_port_range)
        store = setup_store(
            local_hostname=hostname,
            master_server_address=master,
            global_segment_size=segment_size,
            local_buffer_size=segment_size * 2,
            protocol=protocol,
            device_name=device_name,
        )
        ready_q.put({"local_hostname": hostname, "ok": True})
        while not stop_ev.is_set():
            time.sleep(0.5)
        store.close()
    except Exception as e:
        ready_q.put({"local_hostname": hostname, "ok": False, "error": repr(e)})


def main() -> int:
    ip = _host_ip()
    p = argparse.ArgumentParser(description="va-precopy store-only demo")
    p.add_argument(
        "--master",
        default=os.environ.get("MC_MASTER_ADDRESS", "127.0.0.1:50088"),
        help="mooncake_master host:port",
    )
    p.add_argument(
        "--protocol",
        default=os.environ.get("MOONCAKE_PROTOCOL", "tcp"),
        help="tcp | rdma | ascend (vllm-ascend image: use ascend)",
    )
    p.add_argument("--device-name", default=os.environ.get("MOONCAKE_DEVICE", ""))
    p.add_argument("--segment-size", type=int, default=16 * 1024 * 1024)
    p.add_argument(
        "--source-segment",
        default=os.environ.get("SOURCE_SEGMENT", f"{ip}:10001"),
    )
    p.add_argument(
        "--target-segment",
        default=os.environ.get("TARGET_SEGMENT", f"{ip}:10002"),
    )
    p.add_argument(
        "--coord-hostname",
        default=os.environ.get("COORD_HOSTNAME", f"{ip}:12001"),
    )
    p.add_argument(
        "--source-device",
        default=os.environ.get("SOURCE_DEVICE", ""),
        help="physical NPU id for source segment (ascend)",
    )
    p.add_argument(
        "--target-device",
        default=os.environ.get("TARGET_DEVICE", ""),
        help="physical NPU id for target segment (ascend)",
    )
    p.add_argument(
        "--coord-device",
        default=os.environ.get("COORD_DEVICE", ""),
        help="physical NPU id for coordinator client (ascend)",
    )
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

    if args.protocol == "ascend":
        # Defaults: free cards 4/5/6 when 0-3 are occupied by other workloads.
        src_dev = args.source_device or os.environ.get("SOURCE_DEVICE", "4")
        tgt_dev = args.target_device or os.environ.get("TARGET_DEVICE", "5")
        coord_dev = args.coord_device or os.environ.get("COORD_DEVICE", "6")
        # Avoid default 16666 (often taken by other Ascend workloads on this host).
        src_ports = os.environ.get("SOURCE_HCCL_PORTS", "26000-26034")
        tgt_ports = os.environ.get("TARGET_HCCL_PORTS", "26035-26069")
        coord_ports = os.environ.get("COORD_HCCL_PORTS", "26070-26104")
    else:
        src_dev = args.source_device or None
        tgt_dev = args.target_device or None
        coord_dev = args.coord_device or None
        src_ports = tgt_ports = coord_ports = None

    ctx = mp.get_context("spawn")
    ready_q: MpQueue = ctx.Queue()
    stop_ev: MpEvent = ctx.Event()
    procs = []

    print(f"[demo] master={master} protocol={args.protocol} host_ip={ip}")
    print(f"[demo] source={source} (npu={src_dev}) target={target} (npu={tgt_dev})")
    print(f"[demo] coord={args.coord_hostname} (npu={coord_dev}) key={key}")

    for hostname, vis, ports in (
        (source, src_dev, src_ports),
        (target, tgt_dev, tgt_ports),
    ):
        proc = ctx.Process(
            target=_client_main,
            args=(
                hostname,
                master,
                args.protocol,
                args.device_name,
                args.segment_size,
                vis,
                ports,
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
            info = ready_q.get(timeout=120)
            if not info.get("ok", False):
                print(
                    f"ERROR: segment {info.get('local_hostname')} setup failed: "
                    f"{info.get('error')}",
                    file=sys.stderr,
                )
                stop_ev.set()
                for proc in procs:
                    proc.join(timeout=5)
                return 1
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

    _bind_npu(coord_dev, hccl_port_range=coord_ports)
    coord = setup_store(
        local_hostname=args.coord_hostname,
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
    try:
        coord.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
