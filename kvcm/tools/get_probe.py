#!/usr/bin/env python3
"""Byte-level Get probe: read one replica of the 10-12 forward keys after A's death.

Verifies the surviving B-side replicas are readable (not just metadata-present).
Runs on 245 with a bare store client (ascend protocol, NPU device 0 freed by A's
kill), reading cross-machine from 217's segments.

Usage: docker exec vllm-ascend-lake-test python3 /data/wm/lake/kvcm/tools/get_probe.py <key> [n_keys]
"""
import json
import os
import sys

sys.path.insert(0, "/data/wm/lake/kvcm/precopy")
from common import setup_store  # noqa: E402

def main() -> int:
    keys = json.load(open("/data/wm/lake/kvcm/logs/baseline_recheck.json"))
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    sel = [k for k in keys if k.startswith(sys.argv[1])] if len(sys.argv) > 1 else keys
    sel = sel[:want]
    if not sel:
        print("NO KEY SELECTED")
        return 2

    import torch
    import torch_npu  # noqa: F401
    torch.npu.set_device(0)  # device 0 freed by killing A

    store = setup_store(
        local_hostname="7.242.105.245:13031",
        master_server_address="7.242.105.245:50088",
        global_segment_size=0,
        local_buffer_size=64 * 1024 * 1024,
        protocol="ascend",
        device_name="",
    )
    try:
        ok = 0
        for k in sel:
            sz = store.get_size(k)
            if sz is None or sz <= 0:
                print(f"key=..{k[-12:]} get_size={sz} FAIL")
                continue
            data = store.get(k)
            n = len(data) if data is not None else -1
            print(f"key=..{k[-12:]} get_size={sz} bytes={n} {'OK' if n == sz else 'MISMATCH'}")
            ok += n == sz
        print(f"read_ok={ok}/{len(sel)}")
        return 0 if ok == len(sel) else 1
    finally:
        store.close()

if __name__ == "__main__":
    raise SystemExit(main())