//! P4.8:碎片整理计划（逻辑共置 + 物理压实）。
//!
//! 参考:Mooncake 无 compaction 线程（靠 `OffsetBufferAllocator` 减碎片）；
//! SGLang group semantics ≈ 共置提示；Dynamo Pipeline 已承载带宽节流。
//! 关键差异:CP 出计划 + 更新 Location 权威；字节/段布局在 tiered-store SegmentArena。
//! P4.8:co-locate **仅同 node**（跨节点需 transfer + source/target，defer P5）。

use std::collections::{HashMap, HashSet};

use lake_proto::lake::*;

use crate::authority::{resolve_pool_kind, Authority};
use crate::tier::TierL2;

/// Default slot bytes when TriggerDefrag.slot_bytes == 0 (align SegmentArena).
pub const DEFAULT_DEFRAG_SLOT_BYTES: u64 = 4096;

impl Authority {
    /// Build defrag moves from the location view (no byte mutation).
    pub fn plan_defrag(
        &self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        mode: DefragMode,
        slot_bytes: u64,
    ) -> Result<Vec<DefragMove>, String> {
        let slot = if slot_bytes == 0 {
            DEFAULT_DEFRAG_SLOT_BYTES
        } else {
            slot_bytes
        };
        let pk = resolve_pool_kind(pool_kind)?;
        let Some(ns) = self.ns(model_id, revision) else {
            return Err(format!(
                "plan_defrag: unknown namespace ({model_id}, rev={revision:?})"
            ));
        };
        let Some(pool) = ns.pools.get(&pk) else {
            return Ok(Vec::new());
        };

        let mode = if mode == DefragMode::Unspecified {
            DefragMode::Both
        } else {
            mode
        };

        let mut moves = Vec::new();
        if mode == DefragMode::Compact || mode == DefragMode::Both {
            moves.extend(plan_compact(pool, slot));
        }
        if mode == DefragMode::Colocate || mode == DefragMode::Both {
            moves.extend(plan_colocate(pool, slot));
        }
        Ok(moves)
    }

    /// Update L0/L1/L2 placement coordinates (P4.8 Moved).
    #[allow(clippy::too_many_arguments)] // mirrors publish_location_at wire shape
    pub fn relocate_in_view(
        &mut self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
        tier: Tier,
        node_id: &str,
        segment_id: u64,
        offset: u64,
    ) -> Result<(), String> {
        let pk = resolve_pool_kind(pool_kind)?;
        let ns = self
            .ns_mut(model_id, revision)
            .ok_or_else(|| format!("unknown namespace ({model_id}, rev={revision:?})"))?;
        let pool = ns
            .pools
            .get_mut(&pk)
            .ok_or_else(|| format!("relocate: unknown pool_kind {pk}"))?;
        let entry = pool
            .by_flat
            .get_mut(flat)
            .ok_or_else(|| "relocate: unknown block".to_string())?;
        let tier_i = tier as i32;
        let mut found = false;
        for loc in &mut entry.meta.locations {
            if loc.tier == tier_i && loc.node_id == node_id {
                loc.segment_id = segment_id;
                loc.offset = offset;
                found = true;
                break;
            }
        }
        if !found {
            // Upsert: treat as present at new coords.
            entry.meta.locations.push(Location {
                tier: tier_i,
                node_id: node_id.to_string(),
                segment_id,
                offset,
            });
            if let Some(handle) = pool.handles.get(&entry.seq_hash) {
                if tier == Tier::L2 {
                    handle.mark_present::<TierL2>();
                }
            }
        }
        // P6.2: defrag Moved → MOVED 事件(变更后全量位置)。
        let view_ev = crate::view::upsert_event(
            view_event::Kind::Moved,
            entry.meta.id.clone().unwrap_or_else(|| KvBlockId {
                model_id: model_id.into(),
                revision: revision.into(),
                pool_kind: pk,
                block_hash: flat.to_vec(),
                scope: "public".into(),
            }),
            entry.meta.locations.clone(),
            entry.meta.l3_present,
            0,
        );
        self.pending_view_events.push(view_ev);
        Ok(())
    }
}

fn plan_compact(pool: &crate::authority::PoolView, slot: u64) -> Vec<DefragMove> {
    // (node, segment) → sorted offsets
    let mut groups: HashMap<(String, u64), Vec<u64>> = HashMap::new();
    let mut frozen_segments: HashSet<(String, u64)> = HashSet::new();
    for entry in pool.by_flat.values() {
        let frozen = pool.global_refs.get(&entry.seq_hash).copied().unwrap_or(0) > 0;
        for loc in &entry.meta.locations {
            if loc.tier != Tier::L2 as i32 {
                continue;
            }
            if frozen {
                frozen_segments.insert((loc.node_id.clone(), loc.segment_id));
                continue;
            }
            groups
                .entry((loc.node_id.clone(), loc.segment_id))
                .or_default()
                .push(loc.offset);
        }
    }
    let mut out = Vec::new();
    let mut keys: Vec<_> = groups.keys().cloned().collect();
    keys.sort();
    for (node_id, segment_id) in keys {
        if frozen_segments.contains(&(node_id.clone(), segment_id)) {
            continue;
        }
        let mut offs = groups.remove(&(node_id.clone(), segment_id)).unwrap();
        offs.sort_unstable();
        offs.dedup();
        if offs.len() < 2 {
            continue;
        }
        let dense = (0..offs.len()).all(|i| offs[i] == (i as u64).saturating_mul(slot));
        if dense {
            continue;
        }
        out.push(DefragMove {
            id: None,
            node_id,
            from_segment: segment_id,
            from_offset: 0,
            to_segment: segment_id,
            to_offset: 0,
            compact_segment: true,
            segment_id,
        });
    }
    out
}

/// (depth, flat, segment_id, offset) for one L2 member under a prefix root.
type ColocateMember = (usize, Vec<u8>, u64, u64);

fn plan_colocate(pool: &crate::authority::PoolView, slot: u64) -> Vec<DefragMove> {
    // Group by (prefix root, node_id). P4.8: same-node only — cross-node co-locate
    // needs Transfer + source/target fields (defer P5).
    let mut by_root_node: HashMap<(Vec<u8>, String), Vec<ColocateMember>> = HashMap::new();
    // (node, seg, offset) → flat occupying that L2 slot (CP view = planner input).
    let mut occupancy: HashMap<(String, u64, u64), Vec<u8>> = HashMap::new();
    for entry in pool.by_flat.values() {
        let flat = entry
            .meta
            .id
            .as_ref()
            .map(|i| i.block_hash.clone())
            .unwrap_or_default();
        for loc in &entry.meta.locations {
            if loc.tier != Tier::L2 as i32 {
                continue;
            }
            occupancy.insert(
                (loc.node_id.clone(), loc.segment_id, loc.offset),
                flat.clone(),
            );
        }
        if entry.prefix_chain.is_empty() {
            continue;
        }
        if pool.global_refs.get(&entry.seq_hash).copied().unwrap_or(0) > 0 {
            continue;
        }
        let Some(l2) = entry
            .meta
            .locations
            .iter()
            .find(|l| l.tier == Tier::L2 as i32)
        else {
            continue;
        };
        let root = entry.prefix_chain[0].clone();
        let pos = entry
            .prefix_chain
            .iter()
            .position(|h| h == &flat)
            .unwrap_or(entry.prefix_chain.len().saturating_sub(1));
        by_root_node
            .entry((root, l2.node_id.clone()))
            .or_default()
            .push((pos, flat, l2.segment_id, l2.offset));
    }

    let mut out = Vec::new();
    let mut keys: Vec<_> = by_root_node.keys().cloned().collect();
    keys.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)));
    for key in keys {
        let mut members = by_root_node.remove(&key).unwrap();
        let node_id = key.1;
        if members.len() < 2 {
            continue;
        }
        members.sort_by_key(|(pos, _, _, _)| *pos);
        let needs = !is_adjacent_on_segment(&members, slot);
        if !needs {
            continue;
        }
        let Some((dest_seg, base_off)) = pick_colocate_target(&node_id, &members, slot, &occupancy)
        else {
            continue;
        };
        for (i, (_pos, flat, seg, off)) in members.iter().enumerate() {
            let to_off = base_off.saturating_add((i as u64).saturating_mul(slot));
            if *seg == dest_seg && *off == to_off {
                continue;
            }
            let id = pool.by_flat.get(flat).and_then(|e| e.meta.id.clone());
            out.push(DefragMove {
                id,
                node_id: node_id.clone(),
                from_segment: *seg,
                from_offset: *off,
                to_segment: dest_seg,
                to_offset: to_off,
                compact_segment: false,
                segment_id: dest_seg,
            });
        }
    }
    out
}

/// Slot is usable for `intended` iff empty or already held by that same flat.
/// Another chain member at the slot is a conflict (relocate has no temp staging).
fn slot_free_for(
    occupancy: &HashMap<(String, u64, u64), Vec<u8>>,
    node_id: &str,
    seg: u64,
    off: u64,
    intended: &[u8],
) -> bool {
    match occupancy.get(&(node_id.to_string(), seg, off)) {
        None => true,
        Some(occ) => occ.as_slice() == intended,
    }
}

/// Pick (segment, base_offset) with n conflict-free contiguous slots, or a fresh segment.
fn pick_colocate_target(
    node_id: &str,
    members: &[(usize, Vec<u8>, u64, u64)],
    slot: u64,
    occupancy: &HashMap<(String, u64, u64), Vec<u8>>,
) -> Option<(u64, u64)> {
    let n = members.len() as u64;
    if n == 0 || slot == 0 {
        return None;
    }
    let mut segs: Vec<u64> = occupancy
        .keys()
        .filter(|(nid, _, _)| nid == node_id)
        .map(|(_, s, _)| *s)
        .chain(members.iter().map(|(_, _, s, _)| *s))
        .collect();
    segs.sort_unstable();
    segs.dedup();

    // Prefer first member's segment, then others (stable, fewer long hops).
    let mut ordered = Vec::new();
    if let Some((_, _, s0, _)) = members.first() {
        ordered.push(*s0);
    }
    for s in &segs {
        if !ordered.contains(s) {
            ordered.push(*s);
        }
    }

    const MAX_SLOT_IDX: u64 = 64; // align SegmentArena default capacity
    for &seg in &ordered {
        for base_idx in 0..MAX_SLOT_IDX.saturating_sub(n).saturating_add(1) {
            let base = base_idx.saturating_mul(slot);
            let ok = (0..n).all(|i| {
                let off = base.saturating_add(i.saturating_mul(slot));
                let intended = members[i as usize].1.as_slice();
                slot_free_for(occupancy, node_id, seg, off, intended)
            });
            if ok {
                return Some((seg, base));
            }
        }
    }
    // Fresh segment: always empty in the view / arena ensure_seg.
    let fresh = segs.iter().copied().max().unwrap_or(0).saturating_add(1);
    Some((fresh, 0))
}

fn is_adjacent_on_segment(members: &[(usize, Vec<u8>, u64, u64)], slot: u64) -> bool {
    if members.is_empty() {
        return true;
    }
    let seg0 = members[0].2;
    let base = members[0].3;
    for (i, (_, _, seg, off)) in members.iter().enumerate() {
        if *seg != seg0 {
            return false;
        }
        if *off != base.saturating_add((i as u64).saturating_mul(slot)) {
            return false;
        }
    }
    true
}
