#!/usr/bin/env python3
"""PR72 #24 真测：段剩余 = master Capacity−Used；need 来自真副本大小。

真机手工冒烟，不进 GitHub Actions（需 NPU + master + 引擎 warm）。
Actions 只覆盖假 admin 单测（``kvcm/tests/test_backend.py`` 准入用例）。

做法：
  1) 合成空段（默认卡 6/7，各 16MiB，远小于一个 KV block）
  2) 新鲜 prompt warm 进池（真 key、真 size）
  3) TaskEngine 预热到这两个小段
期望：need > remaining → 闸外等到超时，create_copy_task=0

环境变量（缺省 = 45 机 #24 口径）::

    ADMIT_HOST ADMIT_MASTER ADMIT_ADMIN ADMIT_MODEL_PATH ADMIT_MODEL_NAME
    ADMIT_SERVED ADMIT_WARM_URL ADMIT_SYNTH_DEVICES ADMIT_COORD_DEVICE
    ADMIT_COORD_PORT ADMIT_LEDGER ADMIT_SEG_BYTES
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing as mp
import os
import sys
import time
import uuid
import urllib.request
from pathlib import Path

os.environ.setdefault("PYTHONHASHSEED", "0")
_DEMO = Path(__file__).resolve().parents[1]
_KVCM = _DEMO.parent
sys.path.insert(0, str(_KVCM))

from transformers import AutoTokenizer

from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.capability import mooncake_v0311
from kvcm.backends.mooncake.client import StoreClient
from kvcm.backends.mooncake.http_admin import AdminClient
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.flows.prefetch import PrefetchError
from kvcm.ledger import Ledger
from kvcm.ops.types import InstanceId, Prefix
from kvcm.task_engine import TaskEngine

HOST = os.environ.get("ADMIT_HOST", "7.242.104.45")
MASTER = os.environ.get("ADMIT_MASTER", f"{HOST}:50088")
ADMIN = os.environ.get("ADMIT_ADMIN", "http://127.0.0.1:9003")
MODEL_PATH = os.environ.get("ADMIT_MODEL_PATH", "/data/models/Qwen3.8-27B")
MODEL_NAME = os.environ.get("ADMIT_MODEL_NAME", "Qwen3.8-27B")
SERVED = os.environ.get("ADMIT_SERVED", "qwen38-27b")
WARM_URL = os.environ.get("ADMIT_WARM_URL", "http://127.0.0.1:8001/v1/completions")
_SYNTH_SPEC = (
    ("6", "23000", "28300-28334"),
    ("7", "23100", "28400-28434"),
)
_SYNTH_WANT = [
    d.strip()
    for d in os.environ.get("ADMIT_SYNTH_DEVICES", "6,7").split(",")
    if d.strip()
]
BLOCK_SIZE = int(os.environ.get("ADMIT_BLOCK_SIZE", "1536"))
TP = int(os.environ.get("ADMIT_TP", "2"))
TARGET = InstanceId(os.environ.get("ADMIT_TARGET", "inst-admit-real"))
COORD_DEVICE = int(os.environ.get("ADMIT_COORD_DEVICE", "2"))
COORD_PORT = int(os.environ.get("ADMIT_COORD_PORT", "13037"))
LEDGER = os.environ.get("ADMIT_LEDGER", "/tmp/kvcm_admit_real_ledger.sqlite")
# 真 block ~25–50MiB；master 段下限 16MiB，仍远小于一个 block → need > remaining
SEG_BYTES = int(os.environ.get("ADMIT_SEG_BYTES", str(16 * 1024 * 1024)))


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


def helper_main(hostname, master, seg_bytes, phys, base_port, hccl, ready_q, stop_ev):
    try:
        os.environ["ASCEND_RT_VISIBLE_DEVICES"] = str(phys)
        os.environ["ASCEND_BASE_PORT"] = str(base_port)
        os.environ["HCCL_NPU_SOCKET_PORT_RANGE"] = hccl
        import torch
        import torch_npu  # noqa: F401

        torch.npu.set_device(0)
        from mooncake.engine import TransferEngine
        from mooncake.store import MooncakeDistributedStore

        te = TransferEngine()
        te.initialize(HOST, "P2PHANDSHAKE", "ascend", "")
        seg = f"{HOST}:{te.get_rpc_port()}"
        store = MooncakeDistributedStore()
        try:
            rc = store.setup(
                seg,
                "P2PHANDSHAKE",
                seg_bytes,
                min(seg_bytes, 4 * 1024 * 1024),
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
                min(seg_bytes, 4 * 1024 * 1024),
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


def start_helper(phys, base_port, hccl, port_hint):
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
        if seg in admin_get("/get_all_segments").strip().splitlines():
            break
        time.sleep(0.2)
    return p, stop, seg


def _synth_specs() -> list[tuple[str, str, str]]:
    by_dev = {phys: (phys, base, hccl) for phys, base, hccl in _SYNTH_SPEC}
    out: list[tuple[str, str, str]] = []
    for phys in _SYNTH_WANT:
        if phys not in by_dev:
            raise SystemExit(
                f"ADMIT_SYNTH_DEVICES 含未知卡 {phys!r}；"
                f"本脚本只预置 {[p for p, _, _ in _SYNTH_SPEC]}"
            )
        out.append(by_dev[phys])
    if len(out) != 2:
        raise SystemExit("ADMIT 冒烟需要恰好 2 个合成 seg（TP=2）")
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(
        f"[cfg] host={HOST} master={MASTER} model={MODEL_PATH} "
        f"warm={WARM_URL} synth={_SYNTH_WANT} seg_bytes={SEG_BYTES}",
        flush=True,
    )
    uid = uuid.uuid4().hex[:12]
    tok = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True)
    base = f"admitreal-{uid}- "
    prompt = base
    while True:
        ids = tok.encode(prompt, add_special_tokens=False)
        if len(ids) >= 2000:
            break
        prompt += base
    token_ids = tuple(ids)
    n_full = len(token_ids) // BLOCK_SIZE
    print(f"[0] uid={uid} tokens={len(token_ids)} full_blocks={n_full} SEG_BYTES={SEG_BYTES}", flush=True)
    if n_full < 1:
        return 2

    specs = _synth_specs()
    print(
        f"[1] mount tiny synth segs on NPU {','.join(p for p, _, _ in specs)}…",
        flush=True,
    )
    suffix = int(time.time()) % 10000
    helpers = []
    for i, (phys, base_port, hccl) in enumerate(specs):
        helpers.append(start_helper(phys, base_port, hccl, 19200 + suffix + i * 100))
    (p0, s0, seg0), (p1, s1, seg1) = helpers
    synth = [seg0, seg1]
    print(f"[1] synth={synth}", flush=True)

    admin = AdminClient(ADMIN)
    for seg in synth:
        used, cap = admin.query_segment(seg)
        print(
            f"[1] query_segment {seg}: used={used} capacity={cap} remaining={cap - used}",
            flush=True,
        )

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

    if any(client.is_exist(plan_keys)):
        print("FAIL: keys already in pool", flush=True)
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 10

    print("[3] warm A…", flush=True)
    t_warm = time.time()
    http_json(
        WARM_URL,
        {"model": SERVED, "prompt": prompt, "max_tokens": 1, "temperature": 0},
    )
    print(f"[3] warm {time.time()-t_warm:.1f}s", flush=True)
    exist = client.is_exist(plan_keys)
    print(f"[3] in_pool={sum(exist)}/{len(exist)}", flush=True)
    if not all(exist):
        print("FAIL: key mismatch after warm", flush=True)
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 9

    # 真 need：admission 只看计划第一条目标段上、尚无副本的 key
    sizes = client.batch_memory_replica_sizes(plan_keys)
    first_seg = synth[0]
    # 与 backend._copy_plan 一致：keys 按 TP 展开，locate 返回 [seg0, seg1]
    # rank0 keys → seg0。这里直接复用 backend 算 need。
    if os.path.exists(LEDGER):
        os.remove(LEDGER)
    ledger = Ledger(LEDGER)
    backend = MooncakeBackend(
        client=client,
        key_computer=keys_comp,
        locate=lambda _i: list(synth),
        capability=mooncake_v0311(),
        ledger=ledger,
        admin=admin,
        submit_concurrency=4,
    )
    prefix = Prefix(model_id=MODEL_NAME, token_ids=token_ids)
    plan = backend._copy_plan(prefix, TARGET)  # noqa: SLF001
    need_keys = [k for k, seg in plan if seg == first_seg]
    need_bytes = 0
    for k in need_keys:
        rows = sizes.get(k, [])
        sz = next((sz for _ep, sz, _a in rows if sz), None)
        print(f"  size {k[-20:]} → {sz}", flush=True)
        if sz:
            need_bytes += sz
    used, cap = admin.query_segment(first_seg)
    remaining = cap - used
    print(
        f"[3] first_seg={first_seg} need_bytes={need_bytes} remaining={remaining} "
        f"(need>remaining={need_bytes > remaining})",
        flush=True,
    )
    if need_bytes <= remaining:
        print("FAIL: tiny seg unexpectedly fits; bump down SEG_BYTES", flush=True)
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 8

    copy_calls = {"n": 0}
    orig = client.create_copy_task

    def _counted(key, targets):
        copy_calls["n"] += 1
        print(f"  UNEXPECTED create_copy_task #{copy_calls['n']} → {targets}", flush=True)
        return orig(key, targets)

    client.create_copy_task = _counted  # type: ignore[method-assign]

    engine = TaskEngine(backend, max_concurrent=1)
    print("[4] run_once → tiny synth (expect stop before submit)…", flush=True)
    t0 = time.time()
    try:
        asyncio.run(engine.run_once(prefix, TARGET, poll_s=0.3, timeout_s=3.0))
        print("FAIL: run_once succeeded; admission did not block", flush=True)
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 4
    except PrefetchError as e:
        dt = time.time() - t0
        print(f"[4] PrefetchError after {dt:.2f}s: {e}", flush=True)
        if "before submit" not in str(e):
            print("FAIL: wrong PrefetchError shape", flush=True)
            s0.set(); s1.set(); p0.join(5); p1.join(5)
            client.close()
            return 5

    print(f"[4] create_copy_task={copy_calls['n']} (want 0)", flush=True)
    if copy_calls["n"] != 0:
        print("FAIL: copy was submitted despite full tiny seg", flush=True)
        s0.set(); s1.set(); p0.join(5); p1.join(5)
        client.close()
        return 7

    place = client.batch_memory_replica_endpoints(plan_keys)
    hit = sum(1 for eps in place.values() if set(synth).intersection(eps))
    print(f"[4] replicas on synth={hit}/{len(place)} (want 0)", flush=True)

    s0.set(); s1.set(); p0.join(10); p1.join(10)
    client.close()
    print("ALL_OK: real need > real remaining → no create_copy_task", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
