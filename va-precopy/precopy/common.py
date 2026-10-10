"""Mooncake create_copy_task helpers for va-precopy.

The vLLM engine does not participate: copy is a store-client background
transfer coordinated by mooncake_master.
"""

from __future__ import annotations

import time
from typing import Iterable, Sequence

from mooncake.store import (
    MooncakeDistributedStore,
    QueryTaskResponse,
    TaskStatus,
)


def setup_store(
    *,
    local_hostname: str,
    master_server_address: str,
    metadata_server: str = "P2PHANDSHAKE",
    global_segment_size: int = 16 * 1024 * 1024,
    local_buffer_size: int = 32 * 1024 * 1024,
    protocol: str = "tcp",
    device_name: str = "",
) -> MooncakeDistributedStore:
    store = MooncakeDistributedStore()
    rc = store.setup(
        local_hostname,
        metadata_server,
        global_segment_size,
        local_buffer_size,
        protocol,
        device_name,
        master_server_address,
    )
    if rc != 0:
        raise RuntimeError(
            f"MooncakeDistributedStore.setup failed rc={rc} "
            f"host={local_hostname} master={master_server_address}"
        )
    return store


def query_task_until_complete(
    store: MooncakeDistributedStore,
    task_id,
    *,
    poll_s: float = 0.5,
    timeout_s: float = 120.0,
) -> QueryTaskResponse:
    deadline = time.monotonic() + timeout_s
    while True:
        resp, err = store.query_task(task_id)
        if err != 0:
            raise RuntimeError(f"query_task failed err={err} task_id={task_id}")
        if resp.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
            return resp
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"copy/move task {task_id} not terminal after {timeout_s}s; last={resp}"
            )
        time.sleep(poll_s)


def entry_exists(e) -> bool:
    """batch_is_exist entry truthiness — the single definition used by both
    keys.py --check-master and precopy.py's prompt-mode check."""
    return e == 1 or e is True or (isinstance(e, int) and e > 0)


def memory_replica_endpoints(store: MooncakeDistributedStore, key: str) -> list[str]:
    """Return transport endpoints of memory replicas for one key."""
    endpoints: list[str] = []
    for desc in store.get_replica_desc(key):
        if not desc.is_memory_replica:
            continue
        mem = desc.get_memory_descriptor()
        endpoints.append(mem.buffer_descriptor.transport_endpoint)
    return endpoints


def batch_memory_replica_endpoints(
    store: MooncakeDistributedStore, keys: Sequence[str]
) -> dict[str, list[str]]:
    """Map key -> memory-replica endpoints via batch_get_replica_desc."""
    out: dict[str, list[str]] = {k: [] for k in keys}
    descs = store.batch_get_replica_desc(list(keys))
    if isinstance(descs, dict):
        items = descs.items()
    else:
        items = zip(keys, descs)
    for key, replicas in items:
        eps: list[str] = []
        for desc in replicas or []:
            if getattr(desc, "is_memory_replica", False):
                mem = desc.get_memory_descriptor()
                eps.append(mem.buffer_descriptor.transport_endpoint)
        out[str(key)] = eps
    return out


def create_copy_and_wait(
    store: MooncakeDistributedStore,
    key: str,
    targets: Sequence[str],
    *,
    timeout_s: float = 120.0,
) -> QueryTaskResponse:
    task_id, err = store.create_copy_task(key, list(targets))
    if err != 0:
        raise RuntimeError(
            f"create_copy_task failed err={err} key={key!r} targets={list(targets)}"
        )
    resp = query_task_until_complete(store, task_id, timeout_s=timeout_s)
    if resp.status != TaskStatus.SUCCESS:
        raise RuntimeError(
            f"create_copy_task finished non-success: status={resp.status} "
            f"key={key!r} targets={list(targets)} resp={resp}"
        )
    return resp


def assert_targets_have_replicas(
    store: MooncakeDistributedStore,
    keys: Iterable[str],
    targets: Sequence[str],
) -> dict[str, list[str]]:
    key_list = list(keys)
    placement = batch_memory_replica_endpoints(store, key_list)
    missing: list[str] = []
    for key in key_list:
        have = set(placement.get(key, []))
        for t in targets:
            if t not in have:
                missing.append(f"{key} missing on {t} (have={sorted(have)})")
    if missing:
        raise AssertionError("va-precopy verify failed:\n  " + "\n  ".join(missing))
    return placement
