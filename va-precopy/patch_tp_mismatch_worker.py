#!/usr/bin/env python3
"""Patch vllm-ascend v0.26.0rc1 pool_worker.py: full backport of upstream #15835.

Upstream bug (validated on quay.io/ascend/vllm-ascend:v0.26.0rc1):
#11582 introduced TP-mismatch (effective_tp = max(prefill, decode)) strided
KV load/store; #11444's refactor then dropped the production wiring:

1. ``KVPoolWorker._start_kv_transfer_threads`` constructs
   ``KVCacheStoreSendingThread`` / ``KVCacheStoreRecvingThread`` WITHOUT
   ``worker=``; both tp_mismatch branches in ``kv_transfer.py`` gate on
   ``self.worker is not None`` (put: _handle_request; get: async recv), so
   strided sub-key put/get are dead code.
2. ``start_load_kv`` lost the synchronous-load dispatch to
   ``_load_kv_tp_mismatch``: a sync-loading small-TP consumer fetches ONE key
   under its local rank name with the full-local head-slice size, mismatches
   the effective-rank object, and falls back to recompute (hit rate still
   looks high -- fake hit).

Fixed upstream by https://github.com/vllm-project/vllm-ascend/pull/15835
(merge commit 9f8773ea, 2026-09-09, main only; NOT in v0.26.0rc1/rc2).

This script applies the same changes, rc1-anchored:

1. pass ``worker=self if self.tp_mismatch else None`` to both transfer
   threads (None for same-TP keeps the regular path untouched);
2. route synchronous TP-mismatch loads through ``_load_kv_tp_mismatch()``
   (inserted after the ``load_async`` branch, so async loads still enter the
   receiving-thread queue first);
3. upgrades files already carrying the older subset patch (plain
   ``worker=self,``) to the conditional form.

Effect: ``load_async=1`` is no longer REQUIRED for small-TP consumers
(sync load dispatches correctly); async remains the recommended overlap path.

Usage (inside the vllm-ascend container):
  python3 patch_tp_mismatch_worker.py [--pool-worker /path/to/pool_worker.py]
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time

DEFAULT = "/vllm-workspace/vllm-ascend/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py"

COND_WORKER = "worker=self if self.tp_mismatch else None,"
OLD_WORKER = "worker=self,"

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
                    worker=self if self.tp_mismatch else None,
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
                    worker=self if self.tp_mismatch else None,
                )"""

LOAD_OLD = """            if self.load_async:
                self.kv_recv_thread.add_request(  # type: ignore[union-attr]
                    request,
                )
                continue

            addr_list = []"""
LOAD_NEW = """            if self.load_async:
                self.kv_recv_thread.add_request(  # type: ignore[union-attr]
                    request,
                )
                continue

            if self.tp_mismatch:
                # TP mismatch is restricted to non-hybrid, single-group KV.
                group_block_size = self.grouped_block_size[0]
                mask_num = load_spec.vllm_cached_tokens // group_block_size * group_block_size
                self._load_kv_tp_mismatch(
                    request.block_hashes,
                    request.block_ids_by_group[0],
                    token_len,
                    mask_num,
                )
                continue

            addr_list = []"""

SYNC_MARKER = "if self.tp_mismatch:\n                # TP mismatch is restricted to non-hybrid, single-group KV."


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pool-worker", default=DEFAULT)
    a = p.parse_args()
    path = a.pool_worker

    src = open(path, encoding="utf-8").read()
    if COND_WORKER in src and SYNC_MARKER in src:
        print("[patch] already patched, nothing to do")
        return 0

    changed: list[str] = []

    # 1) transfer-thread construction: restore worker= wiring
    if COND_WORKER not in src:
        if OLD_WORKER in src:
            n = src.count(OLD_WORKER)
            src = src.replace(OLD_WORKER, COND_WORKER)
            changed.append(f"upgraded subset patch -> conditional worker= ({n} site(s))")
        else:
            missing = [name for name, old in (("send", SEND_OLD), ("recv", RECV_OLD)) if old not in src]
            if missing:
                print(f"[patch] anchor(s) not found: {missing}; upstream source may differ", file=sys.stderr)
                return 1
            src = src.replace(SEND_OLD, SEND_NEW, 1).replace(RECV_OLD, RECV_NEW, 1)
            changed.append("worker=self if self.tp_mismatch else None (send/recv threads)")

    # 2) synchronous load dispatch: route tp_mismatch through _load_kv_tp_mismatch
    if SYNC_MARKER not in src:
        if LOAD_OLD not in src:
            print("[patch] anchor not found: start_load_kv load_async block; upstream source may differ",
                  file=sys.stderr)
            return 1
        src = src.replace(LOAD_OLD, LOAD_NEW, 1)
        changed.append("sync load dispatch -> _load_kv_tp_mismatch")

    if not changed:
        print("[patch] already patched, nothing to do")
        return 0

    bak = f"{path}.bak.{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(path, bak)
    print(f"[patch] backup -> {bak}")

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("[patch] applied: " + "; ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
