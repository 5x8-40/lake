//! 位置视图权威：每 model_id 一个 `BlockRegistry` + 强句柄 + InactiveIndex。
//!
//! 参考:Dynamo `BlockRegistry` / `InactiveIndex`；驱逐主路径 =
//! `LineageBackend::with_frequency`（只驱叶子 ≈ 前缀亲和 + TinyLFU 冷叶优先 ≈ LFU-Aging）。
//! 不用 `BlockManager`/`BlockStore`——因此必须自己守 inactive 上界：
//! `report_ref` 满容只 skip insert（对齐 Dynamo `inactive.insert`）；
//! 压力 `allocate` 只在显式 `evict_n`（对齐 `allocate_atomic`）。
//! EventsManager 不接线。

use std::collections::HashMap;
use std::sync::Arc;

use kvbm_logical::registry::BlockRegistrationHandle;
use kvbm_logical::{
    BlockId, BlockRegistry, FrequencyTrackingCapacity, InactiveIndex, LineageBackend, SequenceHash,
};

use crate::hash_chain::lineage_from_prefix;
use crate::tier::{TierL0, TierL1, TierL2};
use lake_proto::lake::*;

const INACTIVE_CAP: usize = 4096;
/// Same threshold shape as `MultiLruBackend` / Frequency LeafPolicy.
const FREQ_THRESHOLDS: [u8; 3] = [3, 8, 15];

struct Entry {
    seq_hash: SequenceHash,
    meta: BlockMeta,
    block_id: BlockId,
}

struct Namespace {
    registry: BlockRegistry,
    /// Keep strong refs so Weak entries in the radix tree stay alive.
    handles: HashMap<SequenceHash, BlockRegistrationHandle>,
    /// Flat (content) hash → entry. Same `model_id` + same flat in different
    /// lineages would overwrite; agent chained SHA256 is assumed globally unique
    /// within a model namespace.
    by_flat: HashMap<Vec<u8>, Entry>,
    seq_to_flat: HashMap<SequenceHash, Vec<u8>>,
    inactive: Box<dyn InactiveIndex>,
    /// Hard cap on inactive Real nodes (slab/`LruCache` capacity hint **and**
    /// insert gate: at cap, `report_ref` skips insert rather than allocate).
    inactive_cap: usize,
    /// Aggregate global refs (P4.2 skeleton: all `RefKind` summed into one
    /// counter; `kind` on the wire is ignored). Agent local L1 + per-kind → later.
    global_refs: HashMap<SequenceHash, i64>,
    next_block_id: BlockId,
}

impl Namespace {
    fn new(inactive_cap: usize) -> Self {
        let cap = inactive_cap.max(1);
        let tracker = FrequencyTrackingCapacity::Small.create_tracker();
        let registry = BlockRegistry::builder()
            .frequency_tracker(Arc::clone(&tracker) as _)
            .build();
        let inactive = Box::new(
            LineageBackend::with_frequency(cap, FREQ_THRESHOLDS, tracker)
                .expect("Lineage+Frequency thresholds"),
        );
        Self {
            registry,
            handles: HashMap::new(),
            by_flat: HashMap::new(),
            seq_to_flat: HashMap::new(),
            inactive,
            inactive_cap: cap,
            global_refs: HashMap::new(),
            next_block_id: 1,
        }
    }

    fn alloc_block_id(&mut self) -> BlockId {
        let id = self.next_block_id;
        self.next_block_id = self.next_block_id.saturating_add(1);
        id
    }

    /// Drop up to `n` inactive victims from the location view.
    /// Pressure path only — mirrors Dynamo `BlockStore::allocate_atomic`
    /// (evict when a new slot is needed), never called from `report_ref`.
    fn drop_inactive_victims(&mut self, n: usize) -> usize {
        let victims = self.inactive.allocate(n);
        let mut removed = 0;
        for (seq, _bid) in victims {
            if self.global_refs.get(&seq).copied().unwrap_or(0) > 0 {
                continue;
            }
            self.handles.remove(&seq);
            self.global_refs.remove(&seq);
            if let Some(flat) = self.seq_to_flat.remove(&seq) {
                self.by_flat.remove(&flat);
                removed += 1;
            }
        }
        removed
    }
}

/// Process-local authority state.
pub struct Authority {
    namespaces: HashMap<String, Namespace>,
    inactive_cap: usize,
    /// Completed request barriers: `(request_id, node_id)`.
    /// P4.3: agent flushes L2 + `ReportRef(WRITEBACK,-1)` **before** this RPC;
    /// CP records completion for observability / future directory updates.
    completed_barriers: HashMap<String, String>,
}

impl Default for Authority {
    fn default() -> Self {
        Self::with_inactive_cap(INACTIVE_CAP)
    }
}

impl Authority {
    /// Construct with a custom inactive hard cap (tests use a small value).
    pub fn with_inactive_cap(inactive_cap: usize) -> Self {
        Self {
            namespaces: HashMap::new(),
            inactive_cap: inactive_cap.max(1),
            completed_barriers: HashMap::new(),
        }
    }

    fn ns_mut(&mut self, model_id: &str) -> &mut Namespace {
        let cap = self.inactive_cap;
        self.namespaces
            .entry(model_id.to_string())
            .or_insert_with(|| Namespace::new(cap))
    }

    fn ns(&self, model_id: &str) -> Option<&Namespace> {
        self.namespaces.get(model_id)
    }

    /// Register durable blocks. `prefix_hashes` must be the full ordered chain;
    /// `metas` must be a **contiguous segment** of that chain (miss suffix or
    /// initial prefix — not an arbitrary subset).
    pub fn register(
        &mut self,
        node_id: &str,
        prefix_hashes: &[Vec<u8>],
        metas: Vec<BlockMeta>,
    ) -> Result<(), String> {
        if metas.is_empty() {
            return Ok(());
        }
        let model_id = metas
            .iter()
            .find_map(|m| m.id.as_ref().map(|i| i.model_id.clone()))
            .ok_or_else(|| "RegisterBlocks: no KVBlockID".to_string())?;

        if prefix_hashes.is_empty() {
            return Err("RegisterBlocks: prefix_hashes required (P4.2 lineage)".into());
        }

        let index_of: HashMap<&[u8], usize> = prefix_hashes
            .iter()
            .enumerate()
            .map(|(i, h)| (h.as_slice(), i))
            .collect();

        let mut positions: Vec<usize> = Vec::with_capacity(metas.len());
        for meta in &metas {
            let Some(id) = meta.id.as_ref() else {
                continue;
            };
            if id.model_id != model_id {
                return Err(format!(
                    "RegisterBlocks: mixed model_id {} vs {}",
                    model_id, id.model_id
                ));
            }
            let Some(&pos) = index_of.get(id.block_hash.as_slice()) else {
                return Err(format!(
                    "RegisterBlocks: block hash not in prefix_hashes (len={})",
                    id.block_hash.len()
                ));
            };
            positions.push(pos);
        }
        if positions.is_empty() {
            return Err("RegisterBlocks: no KVBlockID".into());
        }
        positions.sort_unstable();
        positions.dedup();
        for w in positions.windows(2) {
            if w[1] != w[0] + 1 {
                return Err(
                    "RegisterBlocks: blocks must be a contiguous segment of prefix_hashes".into(),
                );
            }
        }

        let lineage = lineage_from_prefix(prefix_hashes);
        let ns = self.ns_mut(&model_id);

        for mut meta in metas {
            let Some(id) = meta.id.clone() else { continue };
            let flat = id.block_hash.clone();
            let pos = *index_of.get(flat.as_slice()).expect("checked");
            let seq = lineage[pos];

            // Refuse invented COMPLETE (Mooncake Get-only-COMPLETE): no empty
            // locations unless l3_present. Callers must pass settle-accurate metas.
            if meta.locations.is_empty() && !meta.l3_present {
                return Err(
                    "RegisterBlocks: need L2 location or l3_present (durable first)".into(),
                );
            }
            for loc in &mut meta.locations {
                if loc.node_id.is_empty() {
                    loc.node_id = node_id.to_string();
                }
            }

            let handle = ns.registry.register_sequence_hash(seq);
            for loc in &meta.locations {
                if loc.tier == Tier::L0 as i32 {
                    handle.mark_present::<TierL0>();
                } else if loc.tier == Tier::L1 as i32 {
                    handle.mark_present::<TierL1>();
                } else if loc.tier == Tier::L2 as i32 {
                    handle.mark_present::<TierL2>();
                }
            }
            ns.handles.insert(seq, handle);

            let block_id = if let Some(prev) = ns.by_flat.get(&flat) {
                prev.block_id
            } else {
                ns.alloc_block_id()
            };
            // Fresh register: not inactive candidate while ref unknown; start refs at 0
            // until ReportRef. Agent historically set ref_count=1 on meta — keep field
            // for Locate display but global_refs drives eviction.
            ns.seq_to_flat.insert(seq, flat.clone());
            ns.by_flat.insert(
                flat,
                Entry {
                    seq_hash: seq,
                    meta,
                    block_id,
                },
            );
        }
        Ok(())
    }

    /// Prefix lookup + soft touch. `&mut self` because of **lazy handle repair**
    /// (`handles.insert` when a flat is in `by_flat` but missing from the registry
    /// index) and TinyLFU `match_sequence_hash(..., touch=true)`.
    ///
    /// P4.3: fine under a single `Mutex<Authority>`. P6 HA / 读写分锁时：懒修复
    /// 应挪到 register 路径，lookup 热路径只读 + 可选无锁 touch，避免永远写锁
    ///（#20；PR #31 review §4.6）。
    pub fn lookup_prefix(
        &mut self,
        model_id: &str,
        prefix_hashes: &[Vec<u8>],
        requester: &str,
    ) -> (Vec<ReusableBlock>, u32, bool) {
        if prefix_hashes.is_empty() {
            return (Vec::new(), 0, false);
        }
        // Ensure namespace exists so we can lazy-index flats registered earlier.
        let _ = self.ns_mut(model_id);
        let lineage = lineage_from_prefix(prefix_hashes);
        let ns = self.ns_mut(model_id);

        let mut out = Vec::new();
        let mut hit = 0u32;
        let mut all_local = true;

        for (i, flat) in prefix_hashes.iter().enumerate() {
            let seq = lineage[i];
            let Some(entry) = ns.by_flat.get(flat) else {
                all_local = false;
                break;
            };
            // Wrong-chain registration must not silently hit (same flat, different lineage).
            if entry.seq_hash != seq {
                all_local = false;
                break;
            }
            // Lazy repair: presence markers **only** from meta.locations —
            // never invent TierL2 for L3-only blocks.
            let meta = entry.meta.clone();
            if !ns.handles.contains_key(&seq) {
                let handle = ns.registry.register_sequence_hash(seq);
                for loc in &meta.locations {
                    if loc.tier == Tier::L0 as i32 {
                        handle.mark_present::<TierL0>();
                    } else if loc.tier == Tier::L1 as i32 {
                        handle.mark_present::<TierL1>();
                    } else if loc.tier == Tier::L2 as i32 {
                        handle.mark_present::<TierL2>();
                    }
                }
                ns.handles.insert(seq, handle);
            } else {
                let _ = ns.registry.match_sequence_hash(seq, true);
            }

            let local = meta
                .locations
                .iter()
                .any(|l| l.tier == Tier::L0 as i32 && l.node_id == requester);
            if !local {
                all_local = false;
            }
            out.push(ReusableBlock {
                id: meta.id.clone(),
                meta: Some(meta),
                local_hit: local,
            });
            hit += 1;
        }
        if hit == 0 {
            all_local = false;
        }
        (out, hit, all_local && hit > 0)
    }

    pub fn locate(&self, ids: &[KvBlockId]) -> Vec<BlockMeta> {
        let mut blocks = Vec::new();
        for id in ids {
            if let Some(ns) = self.ns(&id.model_id) {
                if let Some(entry) = ns.by_flat.get(&id.block_hash) {
                    blocks.push(entry.meta.clone());
                }
            }
        }
        blocks
    }

    /// Validate a ref delta can be applied (block known). Does not mutate.
    pub fn check_report_ref(&self, delta: &RefDelta) -> Result<(), String> {
        let id = delta
            .id
            .as_ref()
            .ok_or_else(|| "RefDelta missing id".to_string())?;
        let ns = self
            .ns(&id.model_id)
            .ok_or_else(|| format!("unknown model_id {}", id.model_id))?;
        if !ns.by_flat.contains_key(&id.block_hash) {
            return Err("RefDelta: unknown block_hash".to_string());
        }
        Ok(())
    }

    /// Apply one ref delta (sum into `global_refs`). Returns error if block unknown.
    ///
    /// P4.2: `delta.kind` is intentionally ignored (合账骨架). Per-kind buckets
    /// and agent `ReportRef` producers land in a later slice.
    pub fn report_ref(&mut self, delta: &RefDelta) -> Result<(), String> {
        self.check_report_ref(delta)?;
        let id = delta.id.as_ref().expect("checked");
        let ns = self.namespaces.get_mut(&id.model_id).expect("checked");
        let entry = ns.by_flat.get(&id.block_hash).expect("checked");
        let seq = entry.seq_hash;
        let block_id = entry.block_id;
        let _kind = delta.kind; // reserved; not booked separately in P4.2

        let cur = ns.global_refs.entry(seq).or_insert(0);
        let before = *cur;
        *cur = cur.saturating_add(i64::from(delta.delta));
        if *cur < 0 {
            *cur = 0;
        }
        let after = *cur;

        if before > 0 && after == 0 {
            // Candidate for eviction — do not delete the view (Dynamo
            // `release_primary` → `inactive.insert` only; allocate is separate).
            // At cap: skip insert so Frequency tiers never silently drop leaves.
            // Skipped block stays at ref=0 out of inactive until a later
            // 0→正→0 cycle retries insert — `evict_n` freeing a slot does
            // **not** auto-requeue it.
            if !ns.inactive.has(seq) && ns.inactive.len() < ns.inactive_cap {
                ns.inactive.insert(seq, block_id);
            }
        } else if before == 0 && after > 0 {
            // Frozen again — leave inactive index (take if present).
            let _ = ns.inactive.take(seq, block_id);
        }
        Ok(())
    }

    /// Apply a batch atomically: validate all, then apply all.
    /// On validation failure, state is unchanged (`ok: false` ⇒ safe to retry whole batch).
    ///
    /// Must not pressure-evict mid-batch: `report_ref` only inserts/skips, so a
    /// later delta cannot see a peer deleted by an earlier one (would panic on
    /// the post-check `expect` and break all-or-nothing).
    pub fn report_refs(&mut self, deltas: &[RefDelta]) -> Result<(), String> {
        for (i, d) in deltas.iter().enumerate() {
            self.check_report_ref(d)
                .map_err(|e| format!("ReportRef batch[{i}]: {e}"))?;
        }
        for d in deltas {
            self.report_ref(d)
                .expect("report_ref after successful check_report_ref");
        }
        Ok(())
    }

    /// Test / pressure hook: evict up to `n` inactive (ref==0) blocks.
    /// Returns number of blocks removed from the location view.
    ///
    /// Production pressure-driven `allocate` (and agent `ReportRef` feeding) → later slice.
    pub fn evict_n(&mut self, model_id: &str, n: usize) -> usize {
        let Some(ns) = self.namespaces.get_mut(model_id) else {
            return 0;
        };
        ns.drop_inactive_victims(n)
    }

    /// Inactive index size (tests).
    pub fn inactive_len(&self, model_id: &str) -> usize {
        self.ns(model_id).map(|n| n.inactive.len()).unwrap_or(0)
    }

    pub fn global_ref(&self, model_id: &str, flat: &[u8]) -> i64 {
        let Some(ns) = self.ns(model_id) else {
            return 0;
        };
        let Some(entry) = ns.by_flat.get(flat) else {
            return 0;
        };
        ns.global_refs.get(&entry.seq_hash).copied().unwrap_or(0)
    }

    /// Record a completed request-end barrier (`consistency.md` §3).
    ///
    /// Agent contract: durable flush + `ReportRef(WRITEBACK,-1)` already applied
    /// so radix blocks are no longer writeback-frozen. Idempotent per request_id.
    pub fn complete_barrier(&mut self, request_id: &str, node_id: &str) -> Result<(), String> {
        if request_id.is_empty() {
            return Err("RequestBarrier: request_id required".into());
        }
        if node_id.is_empty() {
            return Err("RequestBarrier: node_id required".into());
        }
        self.completed_barriers
            .insert(request_id.to_string(), node_id.to_string());
        Ok(())
    }

    pub fn barrier_completed(&self, request_id: &str) -> bool {
        self.completed_barriers.contains_key(request_id)
    }

    /// Publish / revoke a tier location on the view + presence markers.
    ///
    /// P4.3: agent promote/demote calls this after local byte moves.
    pub fn publish_location(
        &mut self,
        model_id: &str,
        flat: &[u8],
        tier: Tier,
        node_id: &str,
        present: bool,
    ) -> Result<(), String> {
        let ns = self
            .namespaces
            .get_mut(model_id)
            .ok_or_else(|| format!("unknown model_id {model_id}"))?;
        let entry = ns
            .by_flat
            .get_mut(flat)
            .ok_or_else(|| "publish_location: unknown block".to_string())?;
        let handle = ns
            .handles
            .get(&entry.seq_hash)
            .ok_or_else(|| "publish_location: missing handle".to_string())?;

        let tier_i = tier as i32;
        let had = entry
            .meta
            .locations
            .iter()
            .any(|l| l.tier == tier_i && l.node_id == node_id);

        if present && !had {
            entry.meta.locations.push(Location {
                tier: tier_i,
                node_id: node_id.to_string(),
                segment_id: 1,
                offset: 0,
            });
            match tier {
                Tier::L0 => handle.mark_present::<TierL0>(),
                Tier::L1 => handle.mark_present::<TierL1>(),
                Tier::L2 => handle.mark_present::<TierL2>(),
                _ => {}
            }
        } else if !present && had {
            entry
                .meta
                .locations
                .retain(|l| !(l.tier == tier_i && l.node_id == node_id));
            match tier {
                Tier::L0 => handle.mark_absent::<TierL0>(),
                Tier::L1 => handle.mark_absent::<TierL1>(),
                Tier::L2 => handle.mark_absent::<TierL2>(),
                _ => {}
            }
        }
        Ok(())
    }

    pub fn set_l3_present(
        &mut self,
        model_id: &str,
        flat: &[u8],
        present: bool,
    ) -> Result<(), String> {
        let ns = self
            .namespaces
            .get_mut(model_id)
            .ok_or_else(|| format!("unknown model_id {model_id}"))?;
        let entry = ns
            .by_flat
            .get_mut(flat)
            .ok_or_else(|| "set_l3_present: unknown block".to_string())?;
        entry.meta.l3_present = present;
        Ok(())
    }

    pub fn has_l0_on(&self, model_id: &str, flat: &[u8], node_id: &str) -> bool {
        self.ns(model_id)
            .and_then(|n| n.by_flat.get(flat))
            .map(|e| {
                e.meta
                    .locations
                    .iter()
                    .any(|l| l.tier == Tier::L0 as i32 && l.node_id == node_id)
            })
            .unwrap_or(false)
    }
}
