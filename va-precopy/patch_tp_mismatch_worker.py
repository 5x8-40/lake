#!/usr/bin/env python3
"""Patch vllm-ascend v0.26.0rc1 pool_worker.py: pass ``worker=self`` to transfer threads.

Upstream bug (validated on quay.io/ascend/vllm-ascend:v0.26.0rc1):
``KVCacheStoreSendingThread`` / ``KVCacheStoreRecvingThread`` are constructed in
``KVPoolWorker._start_kv_transfer_threads`` WITHOUT the ``worker=`` argument.
Both tp_mismatch branches in ``kv_transfer.py`` gate on
``self.worker is not None`` (put: _handle_request; get: async recv), so with
prefill_tp_size != decode_tp_size the put path silently ignores tp_mismatch and
stores keys under LOCAL rank names (head_or_tp_rank:<local>) with full local
head slices, instead of effective-rank sub-keys with strided head slices.
Consumers with larger TP then miss eff-rank keys (partial external hits) and
any "hit" on a coincidentally-named key reads wrong bytes.

This patch only adds ``worker=self`` to the two construction sites; the
tp_mismatch branches themselves are left untouched.

Usage (inside the vllm-ascend container):
  python3 patch_tp_mismatch_worker.py [--pool-worker /path/to/pool_worker.py]
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time

DEFAULT = "/vllm-workspace/vllm-ascend/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py"

SEND_OLD = """                self.kv_send_thread = KVCacheStoreSendingThread(
                    self.m_store,
                    self.token_database,
                    self.grouped_block_size,
                    self.tp_rank,
                    self.tp_size,
                    self.dcp_size,
                    self.put_step,
                    self.kv_role,
                    ready_event_sending,
                    self.group_uses_align_state,
                    self.enable_kv_events,
                )"""
SEND_NEW = """                self.kv_send_thread = KVCacheStoreSendingThread(
                    self.m_store,
                    self.token_database,
                    self.grouped_block_size,
                    self.tp_rank,
                    self.tp_size,
                    self.dcp_size,
                    self.put_step,
                    self.kv_role,
                    ready_event_sending,
                    self.group_uses_align_state,
                    self.enable_kv_events,
                    worker=self,
                )"""
RECV_OLD = """                self.kv_recv_thread = KVCacheStoreRecvingThread(
                    self.m_store,
                    self.token_database,
                    self.grouped_block_size,
                    self.tp_rank,
                    self.tp_size,
                    self.dcp_size,
                    ready_event,
                    invalid_block_ids=self._invalid_block_ids,
                    invalid_block_ids_lock=self._invalid_block_ids_lock,
                )"""
RECV_NEW = """                self.kv_recv_thread = KVCacheStoreRecvingThread(
                    self.m_store,
                    self.token_database,
                    self.grouped_block_size,
                    self.tp_rank,
                    self.tp_size,
                    self.dcp_size,
                    ready_event,
                    invalid_block_ids=self._invalid_block_ids,
                    invalid_block_ids_lock=self._invalid_block_ids_lock,
                    worker=self,
                )"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pool-worker", default=DEFAULT)
    a = p.parse_args()
    path = a.pool_worker

    src = open(path, encoding="utf-8").read()
    if "worker=self," in src:
        print("[patch] already patched, nothing to do")
        return 0

    missing = [name for name, old in (("send", SEND_OLD), ("recv", RECV_OLD)) if old not in src]
    if missing:
        print(f"[patch] anchor(s) not found: {missing}; upstream source may differ", file=sys.stderr)
        return 1

    bak = f"{path}.bak.{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(path, bak)
    print(f"[patch] backup -> {bak}")

    src = src.replace(SEND_OLD, SEND_NEW, 1).replace(RECV_OLD, RECV_NEW, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("[patch] added worker=self to KVCacheStoreSendingThread / KVCacheStoreRecvingThread")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())