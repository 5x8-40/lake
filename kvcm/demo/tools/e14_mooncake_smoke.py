#!/usr/bin/env python3
"""E14 + 真 Mooncake：本机合成空 seg 作目标，强制新鲜 create_copy_task。

绕开「warm 后副本落在 225」的现状：目标改成本机卡 6/7 新建 seg，
拷前确认目标无副本，拷后确认有副本，并收 ZMQ BlockStored。
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import sys
import time
import uuid
import urllib.request

import msgspec
import zmq

os.environ.setdefault("PYTHONHASHSEED", "0")
sys.path.insert(0, "/data/w84156052-104-45/lake/kvcm")
sys.path.insert(0, "/data/w84156052-104-45/lake/kvcm/demo/precopy")

from transformers import AutoTokenizer

from common import setup_store
from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.capability import mooncake_v0311
from kvcm.backends.mooncake.client import StoreClient
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.events.publisher import BlockStoredPublisher
from kvcm.ledger import Ledger
from kvcm.loops import KeepaliveLoop
from kvcm.ops.types import InstanceId, Prefix
from kvcm.task_engine import TaskEngine

HOST = "7.242.104.45"
MASTER = f"{HOST}:50088"
ADMIN = "http://127.0.0.1:9003"
MODEL_PATH = "/data/models/Qwen3.8-27B"
MODEL_NAME = "Qwen3.8-27B"
SERVED = "qwen38-27b"
BLOCK_SIZE = 1536
TP = 2
TARGET = InstanceId("inst-synth")
EVENT_PORT = 19574
EVENT_EP = f"tcp://*:{EVENT_PORT}"
COORD_DEVICE = 2
COORD_PORT = 13035
LEDGER = "/tmp/kvcm_e14_smoke_ledger5.sqlite"
SEG_BYTES = 2 * 1024 * 1024 * 1024  # 2GiB / rank


def http_json(url: str, payload: dict, timeout: float = 300) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def admin_get(path: str) -> str:
    return urllib.request.urlopen(f"{ADMIN}{path}", timeout=10).read().decode()


def helper_main(
    hostname: str,
    master: str,
    seg_bytes: int,
    phys: str,
    base_port: str,
    hccl: str,
    ready_q,
    stop_ev,
) -> None:
    try:
        os.environ["ASCEND_RT_VISIBLE_DEVICES"] = str(phys)
        os.environ["ASCEND_BASE_PORT"] = str(base_port)
        os.environ["HCCL_NPU_SOCKET_PORT_RANGE"] = hccl
        import torch
        import torch_npu  # noqa: F401

        torch.npu.set_device(0)
        # 与 vllm-ascend 同款：TE rpc_port 作 seg 名，避免 verify 对不上
        from mooncake.engine import TransferEngine
        from mooncake.store import MooncakeDistributedStore

        te = TransferEngine()
        te.initialize(HOST, "P2PHANDSHAKE", "ascend", "")
        seg = f"{HOST}:{te.get_rpc_port()}"
        store = MooncakeDistributedStore()
        # 0.3.11：优先试 engine=；失败再退回无 engine 的 setup(hostname)
        try:
            rc = store.setup(
                seg,
                "P2PHANDSHAKE",
                seg_bytes,
                min(seg_bytes, 64 * 1024 * 1024),
                "ascend",
                "",
                master,
                te.get_engine(),
            )
            used = seg
        except TypeError:
            store = MooncakeDistributedStore()
            used = hostname
            rc = store.setup(
                used,
                "P2PHANDSHAKE",
                seg_bytes,
                min(seg_bytes, 64 * 1024 * 1024),
                "ascend",
                "",
                master,
            )
        if rc != 0:
            ready_q.put({"ok": False, "error": f"setup rc={rc}"})
            return
        ready_q.put({"ok": True, "seg": used})
        while not stop_ev.is_set():
            time.sleep(0.3)
        store.close()
    except Exception as e:
        ready_q.put({"ok": False, "error": repr(e)})


def start_helper(phys: str, base_port: str, hccl: str, port_hint: int):
    hostname = f"{HOST}:{port_hint}"
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    stop = ctx.Event()
    p = ctx.Process(
        target=helper_main,
        args=(hostname, MASTER, SEG_BYTES, phys, base_port, hccl, q, stop),
    )
    p.start()
    msg = q.get(timeout=180)
    if not msg.get("ok"):
        stop.set()
        p.join(10)
        raise RuntimeError(f"helper phys={phys} failed: {msg}")
    seg = msg["seg"]
    for _ in range(50):
        segs = admin_get("/get_all_segments").strip().splitlines()
        if seg in segs:
            break
        time.sleep(0.2)
    else:
        print(f"[warn] {seg} not in admin yet: {segs}", flush=True)
    return p, stop, seg


def hit_on(placement: dict[str, list[str]], targets: set[str]) -> tuple[int, int]:
    hit = sum(1 for eps in placement.values() if targets.intersection(eps))
    return hit, len(placement)


def main() -> int:
    uid = uuid.uuid4().hex[:12]
    tok = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True)
    base = f"e14fresh-{uid}- "
    prompt = base
    while True:
        ids = tok.encode(prompt, add_special_tokens=False)
        if len(ids) >= 2000:
            break
        prompt += base
    token_ids = tuple(ids)
    n_full = len(token_ids) // BLOCK_SIZE
    print(
        f"[0] uid={uid} tokens={len(token_ids)} full_blocks={n_full}",
        flush=True,
    )
    if n_full < 1:
        return 2

    print("[1] start synth helpers on NPU 6/7…", flush=True)
    suffix = int(time.time()) % 10000
    p0, s0, seg0 = start_helper("6", "23000", "28300-28334", 19000 + suffix)
    p1, s1, seg1 = start_helper("7", "23100", "28400-28434", 19100 + suffix)
    synth = [seg0, seg1]
    synth_set = set(synth)
    print(f"[1] synth segs={synth}", flush=True)

    os.environ["ASCEND_RT_VISIBLE_DEVICES"] = str(COORD_DEVICE)
    os.environ.setdefault("ASCEND_BASE_PORT", "23200")
    os.environ.setdefault("HCCL_NPU_SOCKET_PORT_RANGE", "28500-28534")

    client = StoreClient()
    client.connect(
        local_hostname=f"{HOST}:{COORD_PORT}",
        master_server_address=MASTER,
        protocol="ascend",
        ascend_device=0,
        global_segment_size=0,
    )
    keys_comp = KeyComputer(
        model_name=MODEL_NAME,
        block_size=BLOCK_SIZE,
        tp_size=TP,
        peer_tp_size=TP,
        put_step=1,
        hash_algo="sha256",
    )
    plan_keys = keys_comp.keys_for_tokens(token_ids)
    print(f"[2] plan_keys={len(plan_keys)}", flush=True)

    pre = client.is_exist(plan_keys)
    print(f"[2] before warm in_pool={sum(pre)}/{len(pre)}", flush=True)
    if any(pre):
        print("FAIL: keys already in pool", flush=True)
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 10

    print("[3] warm A…", flush=True)
    t_warm = time.time()
    http_json(
        "http://127.0.0.1:8001/v1/completions",
        {"model": SERVED, "prompt": prompt, "max_tokens": 1, "temperature": 0},
    )
    print(f"[3] warm {time.time()-t_warm:.1f}s", flush=True)

    exist = client.is_exist(plan_keys)
    print(f"[3] after warm in_pool={sum(exist)}/{len(exist)}", flush=True)
    if not all(exist):
        print("FAIL: tokenizer/engine key mismatch", flush=True)
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 9

    place0 = client.batch_memory_replica_endpoints(plan_keys)
    hit0, n0 = hit_on(place0, synth_set)
    print(f"[3] before copy on_synth={hit0}/{n0} place_sample={list(place0.values())[:2]}", flush=True)
    if hit0 > 0:
        print("FAIL: synth already has replicas", flush=True)
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 6

    copy_calls = {"n": 0}
    _orig = client.create_copy_task

    def _counted(key, targets):
        copy_calls["n"] += 1
        if copy_calls["n"] <= 3:
            print(f"  create_copy_task #{copy_calls['n']} → {targets}", flush=True)
        return _orig(key, targets)

    client.create_copy_task = _counted  # type: ignore[method-assign]

    if os.path.exists(LEDGER):
        os.remove(LEDGER)
    ledger = Ledger(LEDGER)
    backend = MooncakeBackend(
        client=client,
        key_computer=keys_comp,
        locate=lambda _i: list(synth),
        capability=mooncake_v0311(),
        ledger=ledger,
        submit_concurrency=4,
    )
    events = BlockStoredPublisher(
        {str(TARGET): EVENT_EP},
        block_size=BLOCK_SIZE,
        hash_algo="sha256",
    )
    events._publisher_for(str(TARGET), EVENT_EP)
    time.sleep(0.3)
    sub = zmq.Context.instance().socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.RCVTIMEO = 180_000
    sub.connect(f"tcp://127.0.0.1:{EVENT_PORT}")
    time.sleep(0.5)

    keepalive = KeepaliveLoop(
        backend, ledger, interval_s=5.0, window_s=600.0, executor=backend.executor
    )
    engine = TaskEngine(
        backend, keepalive=keepalive, max_concurrent=4, events=events
    )
    prefix = Prefix(model_id=MODEL_NAME, token_ids=token_ids)

    print("[4] run_once → synth…", flush=True)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        keepalive.start()
        t0 = time.time()
        await engine.run_once(prefix, TARGET, poll_s=0.5, timeout_s=180.0)
        return time.time() - t0

    try:
        dt = loop.run_until_complete(_run())
    except Exception as e:
        print(f"FAIL prefetch: {type(e).__name__}: {e}", flush=True)
        events.shutdown()
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 3

    print(f"[4] READY {dt:.1f}s create_copy_task={copy_calls['n']}", flush=True)
    if copy_calls["n"] < 1:
        print("FAIL: no create_copy_task", flush=True)
        events.shutdown()
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 7

    place1 = client.batch_memory_replica_endpoints(plan_keys)
    hit1, n1 = hit_on(place1, synth_set)
    print(f"[4] after copy on_synth={hit1}/{n1}", flush=True)
    for k, eps in place1.items():
        print(f"    …{k[-20:]} eps={eps}", flush=True)
    if hit1 < n1:
        print(f"FAIL: only {hit1}/{n1} keys on synth", flush=True)
        events.shutdown()
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 8

    print("[5] ZMQ…", flush=True)
    try:
        _topic, seq, payload = sub.recv_multipart()
    except zmq.Again:
        print("FAIL: no ZMQ", flush=True)
        events.shutdown()
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 4

    obj = msgspec.msgpack.decode(payload)
    ev = obj[1][0] if isinstance(obj, list) else obj
    print(
        f"[5] BlockStored medium={ev.get('medium')} kind={ev.get('kv_cache_spec_kind')} "
        f"hashes={len(ev.get('block_hashes') or [])}",
        flush=True,
    )
    ok = (
        isinstance(ev, dict)
        and ev.get("type") == "BlockStored"
        and ev.get("medium") == "CPU"
        and ev.get("kv_cache_spec_kind") == "full_attention"
        and len(ev.get("block_hashes") or []) == n_full
    )
    events.shutdown()
    sub.close(linger=0)
    s0.set(); s1.set(); p0.join(10); p1.join(10)
    try:
        client.close()
    except Exception:
        pass
    if not ok:
        print("FAIL: bad event", flush=True)
        return 5
    print(
        f"PASS: create_copy_task={copy_calls['n']} READY={dt:.1f}s "
        f"on_synth={hit1}/{n1} + ZMQ",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    # spawn helpers need
    mp.set_start_method("spawn", force=True)
    raise SystemExit(main())
