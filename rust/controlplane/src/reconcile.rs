//! P4.7:冷块 GC / 孤儿 TTL / 节点级 reconcile。
//!
//! 参考:
//! - Mooncake `put_start_discard_timeout` / `ClearInvalidHandles`(zombie + 死 client)
//! - lake `consistency.md` §6:元数据先于字节删;冷块留 L2/L3
//!
//! 关键差异:lake 不做会话级 PutStart TTL;writeback 泄漏靠 **节点级** reconcile
//! (心跳过期 → 清该节点 ref + 摘 L0)。

use std::time::{SystemTime, UNIX_EPOCH};

use lake_proto::lake::*;

use crate::authority::{resolve_pool_kind, Authority, NamespaceKey, RegisterStatus};
use crate::tier::{TierL0, TierL1};

/// Default orphan TTL = Mooncake `put_start_discard_timeout` (30s).
pub const DEFAULT_ORPHAN_TTL_MS: u64 = 30_000;

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
pub(crate) struct BlockKey {
    pub model_id: String,
    pub revision: String,
    pub pool_kind: i32,
    pub flat: Vec<u8>,
}

impl BlockKey {
    pub fn from_id(id: &KvBlockId) -> Self {
        Self {
            model_id: id.model_id.clone(),
            revision: id.revision.clone(),
            pool_kind: if id.pool_kind == 0 {
                PoolKind::Target as i32
            } else {
                id.pool_kind
            },
            flat: id.block_hash.clone(),
        }
    }

    pub fn to_id(&self) -> KvBlockId {
        KvBlockId {
            model_id: self.model_id.clone(),
            revision: self.revision.clone(),
            pool_kind: self.pool_kind,
            block_hash: self.flat.clone(),
            scope: "public".into(),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct OrphanEntry {
    #[allow(dead_code)] // retained for future node-scoped orphan scrub
    pub id: KvBlockId,
    #[allow(dead_code)]
    pub node_id: String,
    pub marked_at_ms: u64,
}

pub fn wall_now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

impl Authority {
    /// Inject clock for tests (orphan TTL).
    pub fn set_now_ms_fn(&mut self, f: fn() -> u64) {
        self.now_ms = f;
    }

    pub fn now_ms(&self) -> u64 {
        (self.now_ms)()
    }

    /// Record agent-reported incomplete writes.
    /// Skip ids already present in the location view (completed PutEnd).
    pub fn report_orphans(&mut self, reports: &[OrphanReport]) -> Result<(), String> {
        let now = self.now_ms();
        for r in reports {
            let stamped = if r.marked_at_ms == 0 {
                now
            } else {
                r.marked_at_ms
            };
            for id in &r.ids {
                let key = BlockKey::from_id(id);
                if self.block_in_view(&key) {
                    // Already registered — stale report; do not re-arm TTL kill.
                    self.orphans.remove(&key);
                    continue;
                }
                self.orphans.insert(
                    key.clone(),
                    OrphanEntry {
                        id: key.to_id(),
                        node_id: r.node_id.clone(),
                        marked_at_ms: stamped,
                    },
                );
            }
        }
        Ok(())
    }

    fn block_in_view(&self, key: &BlockKey) -> bool {
        let pk = resolve_pool_kind(key.pool_kind).unwrap_or(PoolKind::Target as i32);
        self.namespaces
            .get(&NamespaceKey::new(&key.model_id, &key.revision))
            .and_then(|ns| ns.pools.get(&pk))
            .map(|p| p.by_flat.contains_key(&key.flat))
            .unwrap_or(false)
    }

    /// Explicit metadata discard (bytes deleted by caller after).
    pub fn discard_blocks(&mut self, ids: &[KvBlockId]) -> Result<Vec<KvBlockId>, String> {
        let mut out = Vec::new();
        for id in ids {
            if self.discard_one(id)? {
                out.push(id.clone());
            }
        }
        Ok(out)
    }

    fn discard_one(&mut self, id: &KvBlockId) -> Result<bool, String> {
        let pk = resolve_pool_kind(id.pool_kind)?;
        let key = NamespaceKey::new(id.model_id.clone(), id.revision.clone());
        let Some(ns) = self.namespaces.get_mut(&key) else {
            return Ok(false);
        };
        let Some(pool) = ns.pools.get_mut(&pk) else {
            return Ok(false);
        };
        let Some(entry) = pool.by_flat.get(&id.block_hash) else {
            self.orphans.remove(&BlockKey::from_id(id));
            return Ok(false);
        };
        let seq = entry.seq_hash;
        if pool.global_refs.get(&seq).copied().unwrap_or(0) > 0 {
            return Err("DiscardBlocks: block has global_refs > 0".into());
        }
        let entry = pool
            .by_flat
            .remove(&id.block_hash)
            .expect("checked by_flat entry");
        let bid = entry.block_id;
        pool.handles.remove(&seq);
        pool.global_refs.remove(&seq);
        pool.seq_to_flat.remove(&seq);
        let _ = pool.inactive.take(seq, bid);
        let bpb = ns.bytes_per_block();
        ns.used_bytes = (ns.used_bytes - bpb).max(0);
        self.orphans.remove(&BlockKey::from_id(id));
        // Drop node_refs rows for this block.
        for held in self.node_refs.values_mut() {
            held.remove(&BlockKey::from_id(id));
        }
        Ok(true)
    }

    /// Cold GC: peel L0/L1 from inactive victims; keep radix + L2/L3.
    /// Victims with no durable backing are fully discarded.
    pub fn gc_cold(
        &mut self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        n: usize,
    ) -> Result<(Vec<KvBlockId>, Vec<KvBlockId>), String> {
        if n == 0 {
            return Ok((Vec::new(), Vec::new()));
        }
        let pk = resolve_pool_kind(pool_kind)?;
        let Some(ns) = self.ns_mut(model_id, revision) else {
            return Err(format!(
                "gc_cold: unknown namespace ({model_id}, rev={revision:?})"
            ));
        };
        let Some(pool) = ns.pools.get_mut(&pk) else {
            return Ok((Vec::new(), Vec::new()));
        };
        let victims = pool.inactive.allocate(n);
        let mut stripped = Vec::new();
        let mut discarded_keys = Vec::new();
        let mut full_remove = Vec::new();
        // Durable peel consumes inactive via allocate; must re-insert so later
        // GC / hard-quota reclaim still see ref=0 L2/L3 blocks.
        let mut reinsert = Vec::new();

        for (seq, bid) in victims {
            if pool.global_refs.get(&seq).copied().unwrap_or(0) > 0 {
                // Inactive victims should be ref=0 because report_ref 0→>0
                // removes them from inactive. If stale defensive state slips in,
                // dropping this allocation result is safe: the next >0→0 transition
                // will enqueue it again.
                continue;
            }
            let Some(flat) = pool.seq_to_flat.get(&seq).cloned() else {
                continue;
            };
            let Some(entry) = pool.by_flat.get_mut(&flat) else {
                continue;
            };
            let id = entry.meta.id.clone().unwrap_or_else(|| KvBlockId {
                model_id: model_id.into(),
                revision: revision.into(),
                pool_kind: pk,
                block_hash: flat.clone(),
                scope: "public".into(),
            });
            let has_l0 = entry
                .meta
                .locations
                .iter()
                .any(|l| l.tier == Tier::L0 as i32);
            let has_l1 = entry
                .meta
                .locations
                .iter()
                .any(|l| l.tier == Tier::L1 as i32);
            let has_l2 = entry
                .meta
                .locations
                .iter()
                .any(|l| l.tier == Tier::L2 as i32);
            let durable = has_l2 || entry.meta.l3_present;
            if let Some(handle) = pool.handles.get(&seq) {
                // mark_absent panics if marker was never present.
                if has_l0 {
                    handle.mark_absent::<TierL0>();
                }
                if has_l1 {
                    handle.mark_absent::<TierL1>();
                }
            }
            entry
                .meta
                .locations
                .retain(|l| l.tier != Tier::L0 as i32 && l.tier != Tier::L1 as i32);
            if durable {
                stripped.push(id);
                reinsert.push((seq, bid));
            } else {
                full_remove.push((seq, flat, id));
            }
        }

        let bpb = ns.bytes_per_block();
        for (seq, flat, id) in full_remove {
            if let Some(pool) = ns.pools.get_mut(&pk) {
                if let Some(entry) = pool.by_flat.remove(&flat) {
                    let bid = entry.block_id;
                    pool.handles.remove(&seq);
                    pool.global_refs.remove(&seq);
                    pool.seq_to_flat.remove(&seq);
                    let _ = pool.inactive.take(seq, bid);
                    ns.used_bytes = (ns.used_bytes - bpb).max(0);
                    discarded_keys.push(id);
                }
            }
        }
        if let Some(pool) = ns.pools.get_mut(&pk) {
            for (seq, bid) in reinsert {
                if pool.global_refs.get(&seq).copied().unwrap_or(0) > 0 {
                    continue;
                }
                if !pool.inactive.has(seq) && pool.inactive.len() < pool.inactive_cap {
                    pool.inactive.insert(seq, bid);
                }
            }
        }
        for id in &discarded_keys {
            self.orphans.remove(&BlockKey::from_id(id));
        }
        Ok((stripped, discarded_keys))
    }

    /// Node-level reconcile: clear that node's ref holdings + strip its L0.
    /// Covers writeback-ref leak when agent dies mid PutEnd (issue #20 P4.7).
    pub fn reconcile_dead_node(&mut self, node_id: &str) -> Result<(u32, Vec<KvBlockId>), String> {
        if node_id.is_empty() {
            return Err("reconcile_dead_node: node_id required".into());
        }
        let held = self.node_refs.remove(node_id).unwrap_or_default();
        let mut refs_cleared = 0u32;
        for (bk, count) in held {
            if count == 0 {
                continue;
            }
            refs_cleared += 1;
            let delta = RefDelta {
                id: Some(bk.to_id()),
                kind: RefKind::Unspecified as i32,
                delta: -(count as i32),
                // `track_node=false` below is the primary guard; keep node_id
                // empty too so dead-node cleanup cannot re-enter node_refs.
                node_id: String::new(),
            };
            // Best-effort: block may already be gone.
            let _ = self.report_ref_raw(&delta, /*track_node*/ false);
        }

        // Strip L0 locations for this node across all namespaces.
        let mut touched = Vec::new();
        let keys: Vec<NamespaceKey> = self.namespaces.keys().cloned().collect();
        for ns_key in keys {
            let Some(ns) = self.namespaces.get_mut(&ns_key) else {
                continue;
            };
            let pool_kinds: Vec<i32> = ns.pools.keys().copied().collect();
            for pk in pool_kinds {
                let Some(pool) = ns.pools.get_mut(&pk) else {
                    continue;
                };
                let flats: Vec<Vec<u8>> = pool.by_flat.keys().cloned().collect();
                for flat in flats {
                    let Some(entry) = pool.by_flat.get_mut(&flat) else {
                        continue;
                    };
                    let had = entry
                        .meta
                        .locations
                        .iter()
                        .any(|l| l.tier == Tier::L0 as i32 && l.node_id == node_id);
                    if !had {
                        continue;
                    }
                    entry
                        .meta
                        .locations
                        .retain(|l| !(l.tier == Tier::L0 as i32 && l.node_id == node_id));
                    if let Some(handle) = pool.handles.get(&entry.seq_hash) {
                        // mark_absent is presence-count based; if other nodes still
                        // have L0, count may stay >0 — acceptable for P4 mock.
                        if !entry
                            .meta
                            .locations
                            .iter()
                            .any(|l| l.tier == Tier::L0 as i32)
                        {
                            handle.mark_absent::<TierL0>();
                        }
                    }
                    touched.push(KvBlockId {
                        model_id: ns_key.model_id.clone(),
                        revision: ns_key.revision.clone(),
                        pool_kind: pk,
                        block_hash: flat,
                        scope: "public".into(),
                    });
                }
            }
        }
        Ok((refs_cleared, touched))
    }

    /// Sweep orphans past TTL → discard metadata.
    pub fn sweep_orphans(&mut self, ttl_ms: u64) -> Vec<KvBlockId> {
        let ttl = if ttl_ms == 0 {
            DEFAULT_ORPHAN_TTL_MS
        } else {
            ttl_ms
        };
        let now = self.now_ms();
        let expired: Vec<BlockKey> = self
            .orphans
            .iter()
            .filter(|(_, e)| now.saturating_sub(e.marked_at_ms) >= ttl)
            .map(|(k, _)| k.clone())
            .collect();
        let mut discarded = Vec::new();
        for k in expired {
            let id = k.to_id();
            // Defense: if PutEnd won the race and the block is in view, only
            // drop the stale mark — never discard a completed registration.
            if self.block_in_view(&k) {
                self.orphans.remove(&k);
                continue;
            }
            // Never registered (or already gone) — drop mark; discard if present.
            if self.discard_one(&id).unwrap_or(false) {
                discarded.push(id);
            } else {
                self.orphans.remove(&k);
                discarded.push(id);
            }
        }
        discarded
    }

    /// Combined reconcile entry (proto `ReconcileOrphans`).
    pub fn reconcile_orphans(
        &mut self,
        req: &ReconcileOrphansRequest,
    ) -> Result<ReconcileOrphansResponse, String> {
        self.report_orphans(&req.reports)?;
        let mut discarded = self.sweep_orphans(req.orphan_ttl_ms);
        let mut refs_cleared = 0u32;
        if !req.dead_node_id.is_empty() {
            let (n, _touched) = self.reconcile_dead_node(&req.dead_node_id)?;
            refs_cleared = n;
        }
        let mut cold_stripped = Vec::new();
        if req.gc_cold_limit > 0 && !req.gc_model_id.is_empty() {
            let (strip, full) = self.gc_cold(
                &req.gc_model_id,
                &req.gc_revision,
                req.gc_pool_kind,
                req.gc_cold_limit as usize,
            )?;
            cold_stripped = strip;
            discarded.extend(full);
        }
        Ok(ReconcileOrphansResponse {
            discarded,
            cold_stripped,
            refs_cleared,
            ok: true,
            err: String::new(),
        })
    }

    /// Export authority snapshot for checkpoint (meta + prefix lineage).
    pub fn export_snapshot(&self, seq: u64) -> CheckpointSnapshot {
        let mut models = Vec::new();
        let mut blocks = Vec::new();
        let mut keys: Vec<_> = self.namespaces.keys().cloned().collect();
        keys.sort_by(|a, b| {
            a.model_id
                .cmp(&b.model_id)
                .then(a.revision.cmp(&b.revision))
        });
        for k in keys {
            let Some(ns) = self.namespaces.get(&k) else {
                continue;
            };
            models.push(ns.descriptor.clone());
            let mut pks: Vec<_> = ns.pools.keys().copied().collect();
            pks.sort_unstable();
            for pk in pks {
                let Some(pool) = ns.pools.get(&pk) else {
                    continue;
                };
                for entry in pool.by_flat.values() {
                    let chain = if entry.prefix_chain.is_empty() {
                        // Legacy / defensive: single-hash root if chain missing.
                        entry
                            .meta
                            .id
                            .as_ref()
                            .map(|i| vec![i.block_hash.clone()])
                            .unwrap_or_default()
                    } else {
                        entry.prefix_chain.clone()
                    };
                    blocks.push(CheckpointBlock {
                        meta: Some(entry.meta.clone()),
                        prefix_chain: chain,
                    });
                }
            }
        }
        // Shorter chains first so restore can register ancestors before children.
        blocks.sort_by(|a, b| {
            a.prefix_chain
                .len()
                .cmp(&b.prefix_chain.len())
                .then_with(|| {
                    let ah = a
                        .meta
                        .as_ref()
                        .and_then(|m| m.id.as_ref())
                        .map(|i| i.block_hash.as_slice())
                        .unwrap_or(&[]);
                    let bh = b
                        .meta
                        .as_ref()
                        .and_then(|m| m.id.as_ref())
                        .map(|i| i.block_hash.as_slice())
                        .unwrap_or(&[]);
                    ah.cmp(bh)
                })
        });
        CheckpointSnapshot {
            seq,
            models,
            blocks,
        }
    }

    /// Replace authority from snapshot (metadata rebuild; bytes stay in pool).
    /// Restores radix lineage via each block's `prefix_chain` (not flat roots).
    pub fn import_snapshot(&mut self, snap: &CheckpointSnapshot) -> Result<(), String> {
        self.namespaces.clear();
        self.orphans.clear();
        self.node_refs.clear();
        self.completed_barriers.clear();
        self.checkpoint_seq = snap.seq;

        for m in &snap.models {
            self.register_model(m.clone())?;
        }

        let mut rows: Vec<&CheckpointBlock> = snap.blocks.iter().collect();
        rows.sort_by_key(|b| b.prefix_chain.len());

        for cb in rows {
            let Some(meta) = cb.meta.clone() else {
                continue;
            };
            let Some(id) = meta.id.as_ref() else {
                continue;
            };
            let pk = resolve_pool_kind(id.pool_kind).unwrap_or(PoolKind::Target as i32);
            let mut chain = cb.prefix_chain.clone();
            if chain.is_empty() {
                // Backward-compat: treat as lineage root (single-block only safe).
                chain = vec![id.block_hash.clone()];
            }
            if !chain.iter().any(|h| h == &id.block_hash) {
                return Err(format!(
                    "import_snapshot: block hash not in prefix_chain (len={})",
                    chain.len()
                ));
            }
            let node = meta
                .locations
                .iter()
                .find(|l| l.tier == Tier::L2 as i32)
                .map(|l| l.node_id.clone())
                .unwrap_or_else(|| "restore".into());
            let mut m = meta;
            if let Some(ref mut mid) = m.id {
                mid.pool_kind = pk;
            }
            match self.register(&node, &chain, vec![m]) {
                Ok(RegisterStatus::Accepted) => {}
                Ok(RegisterStatus::RejectedHardQuota(bp)) => {
                    return Err(format!(
                        "import_snapshot register rejected: reason={} deficit={}",
                        bp.reason, bp.deficit_bytes
                    ));
                }
                Err(e) => return Err(format!("import_snapshot register: {e}")),
            }
        }
        Ok(())
    }

    /// Apply ref without optional node tracking (used by dead-node clear).
    pub(crate) fn report_ref_raw(
        &mut self,
        delta: &RefDelta,
        track_node: bool,
    ) -> Result<(), String> {
        self.check_report_ref(delta)?;
        let id = delta.id.as_ref().expect("checked");
        let pk = resolve_pool_kind(id.pool_kind).expect("checked");
        let key = NamespaceKey::from_id(id);
        let ns = self.namespaces.get_mut(&key).expect("checked");
        let pool = ns.pools.get_mut(&pk).expect("checked");
        let entry = pool.by_flat.get(&id.block_hash).expect("checked");
        let seq = entry.seq_hash;
        let block_id = entry.block_id;

        let cur = pool.global_refs.entry(seq).or_insert(0);
        let before = *cur;
        *cur = cur.saturating_add(i64::from(delta.delta));
        if *cur < 0 {
            *cur = 0;
        }
        let after = *cur;

        if before > 0 && after == 0 {
            if !pool.inactive.has(seq) && pool.inactive.len() < pool.inactive_cap {
                pool.inactive.insert(seq, block_id);
            }
        } else if before == 0 && after > 0 {
            let _ = pool.inactive.take(seq, block_id);
        }

        if track_node && !delta.node_id.is_empty() && delta.delta != 0 {
            let bk = BlockKey::from_id(id);
            let held = self.node_refs.entry(delta.node_id.clone()).or_default();
            let e = held.entry(bk).or_insert(0);
            *e = e.saturating_add(i64::from(delta.delta));
            if *e <= 0 {
                held.remove(&BlockKey::from_id(id));
            }
        }
        Ok(())
    }
}
