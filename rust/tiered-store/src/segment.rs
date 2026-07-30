//! L2 段式布局 mock（P4.8）。
//!
//! 参考:Mooncake `OffsetBufferAllocator` / `getLargestFreeRegion`（靠分配器减碎片、
//! **无** compaction 线程）。lake 主动压实：free 留洞 → `compact` 稠密重排。
//! 关键差异:真 NVMe / RDMA segment 仍 P5；本层只服务利用率与共置单测。

use std::collections::HashMap;

/// Default slot size when caller passes 0 (P4 mock; not production page size).
pub const DEFAULT_SLOT_BYTES: u64 = 4096;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Placement {
    pub segment_id: u64,
    pub offset: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Relocate {
    pub hash: Vec<u8>,
    pub from: Placement,
    pub to: Placement,
}

struct Segment {
    /// Fixed slot count; each occupied slot holds one block hash.
    slots: Vec<Option<Vec<u8>>>,
}

impl Segment {
    fn new(capacity_slots: usize) -> Self {
        Self {
            slots: vec![None; capacity_slots.max(1)],
        }
    }

    fn live_slots(&self) -> usize {
        self.slots.iter().filter(|s| s.is_some()).count()
    }

    fn capacity_slots(&self) -> usize {
        self.slots.len()
    }

    fn first_free(&self) -> Option<usize> {
        self.slots.iter().position(|s| s.is_none())
    }
}

/// In-process segment arena: layout metadata only (bytes stay in LocalTierEngine).
pub struct SegmentArena {
    slot_bytes: u64,
    capacity_slots: usize,
    segments: HashMap<u64, Segment>,
    next_seg: u64,
    by_hash: HashMap<Vec<u8>, Placement>,
}

impl SegmentArena {
    pub fn new(slot_bytes: u64, capacity_slots: usize) -> Self {
        Self {
            slot_bytes: if slot_bytes == 0 {
                DEFAULT_SLOT_BYTES
            } else {
                slot_bytes
            },
            capacity_slots: capacity_slots.max(1),
            segments: HashMap::new(),
            next_seg: 1,
            by_hash: HashMap::new(),
        }
    }

    pub fn slot_bytes(&self) -> u64 {
        self.slot_bytes
    }

    pub fn placement(&self, hash: &[u8]) -> Option<Placement> {
        self.by_hash.get(hash).copied()
    }

    /// Live bytes / segment capacity (0..1). Missing segment → 0.
    pub fn utilization(&self, segment_id: u64) -> f64 {
        let Some(seg) = self.segments.get(&segment_id) else {
            return 0.0;
        };
        let cap = seg.capacity_slots();
        if cap == 0 {
            return 0.0;
        }
        seg.live_slots() as f64 / cap as f64
    }

    /// Occupied slots / capacity for segment (same as utilization for fixed slots).
    pub fn occupied_ratio(&self, segment_id: u64) -> f64 {
        self.utilization(segment_id)
    }

    /// Dense prefix length in slots (how far from offset 0 without holes).
    pub fn dense_prefix_slots(&self, segment_id: u64) -> usize {
        let Some(seg) = self.segments.get(&segment_id) else {
            return 0;
        };
        seg.slots.iter().take_while(|s| s.is_some()).count()
    }

    fn ensure_seg(&mut self, segment_id: u64) {
        self.segments
            .entry(segment_id)
            .or_insert_with(|| Segment::new(self.capacity_slots));
        if segment_id >= self.next_seg {
            self.next_seg = segment_id + 1;
        }
    }

    /// Allocate into any segment with free slot; creates a new segment if needed.
    pub fn alloc(&mut self, hash: &[u8]) -> Result<Placement, String> {
        if self.by_hash.contains_key(hash) {
            return Err("segment alloc: hash already placed".into());
        }
        // Prefer existing segments with free slots (lowest id).
        let mut ids: Vec<u64> = self.segments.keys().copied().collect();
        ids.sort_unstable();
        for id in ids {
            if let Some(idx) = self.segments.get(&id).and_then(|s| s.first_free()) {
                return self.place_at(hash, id, (idx as u64) * self.slot_bytes);
            }
        }
        let id = self.next_seg;
        self.next_seg += 1;
        self.ensure_seg(id);
        self.place_at(hash, id, 0)
    }

    /// Force placement (tests / co-locate dest). Fails if slot occupied or hash exists.
    pub fn place_at(
        &mut self,
        hash: &[u8],
        segment_id: u64,
        offset: u64,
    ) -> Result<Placement, String> {
        if self.by_hash.contains_key(hash) {
            return Err("place_at: hash already placed".into());
        }
        if !offset.is_multiple_of(self.slot_bytes) {
            return Err("place_at: offset not slot-aligned".into());
        }
        let idx = (offset / self.slot_bytes) as usize;
        self.ensure_seg(segment_id);
        let seg = self.segments.get_mut(&segment_id).expect("ensured");
        if idx >= seg.slots.len() {
            return Err("place_at: offset beyond segment capacity".into());
        }
        if seg.slots[idx].is_some() {
            return Err("place_at: slot occupied".into());
        }
        seg.slots[idx] = Some(hash.to_vec());
        let p = Placement { segment_id, offset };
        self.by_hash.insert(hash.to_vec(), p);
        Ok(p)
    }

    pub fn free(&mut self, hash: &[u8]) -> bool {
        let Some(p) = self.by_hash.remove(hash) else {
            return false;
        };
        if let Some(seg) = self.segments.get_mut(&p.segment_id) {
            let idx = (p.offset / self.slot_bytes) as usize;
            if idx < seg.slots.len() {
                seg.slots[idx] = None;
            }
        }
        true
    }

    /// Compact one segment: pack live blocks to the front. Returns relocations.
    pub fn compact(&mut self, segment_id: u64) -> Result<Vec<Relocate>, String> {
        let Some(seg) = self.segments.get_mut(&segment_id) else {
            return Err(format!("compact: unknown segment {segment_id}"));
        };
        let live: Vec<Vec<u8>> = seg.slots.iter().filter_map(|s| s.clone()).collect();
        let mut relocs = Vec::new();
        // Clear all slots then rewrite densely.
        for slot in seg.slots.iter_mut() {
            *slot = None;
        }
        for (i, hash) in live.into_iter().enumerate() {
            let old = self.by_hash.get(&hash).copied().unwrap_or(Placement {
                segment_id,
                offset: 0,
            });
            let new_off = (i as u64) * self.slot_bytes;
            seg.slots[i] = Some(hash.clone());
            let to = Placement {
                segment_id,
                offset: new_off,
            };
            self.by_hash.insert(hash.clone(), to);
            if old != to {
                relocs.push(Relocate {
                    hash,
                    from: old,
                    to,
                });
            }
        }
        Ok(relocs)
    }

    /// Move a live block to dest (must be free). Used for co-location.
    pub fn relocate(
        &mut self,
        hash: &[u8],
        dest_segment: u64,
        dest_offset: u64,
    ) -> Result<Relocate, String> {
        let from = self
            .placement(hash)
            .ok_or_else(|| "relocate: unknown hash".to_string())?;
        if from.segment_id == dest_segment && from.offset == dest_offset {
            return Ok(Relocate {
                hash: hash.to_vec(),
                from,
                to: from,
            });
        }
        // Free source first so same-segment dense moves work.
        self.free(hash);
        match self.place_at(hash, dest_segment, dest_offset) {
            Ok(to) => Ok(Relocate {
                hash: hash.to_vec(),
                from,
                to,
            }),
            Err(e) => {
                // Best-effort restore.
                let _ = self.place_at(hash, from.segment_id, from.offset);
                Err(e)
            }
        }
    }

    /// True if segment has holes (live blocks not a dense prefix).
    pub fn has_holes(&self, segment_id: u64) -> bool {
        let Some(seg) = self.segments.get(&segment_id) else {
            return false;
        };
        let live = seg.live_slots();
        if live == 0 {
            return false;
        }
        self.dense_prefix_slots(segment_id) < live
    }

    pub fn segment_ids(&self) -> Vec<u64> {
        let mut ids: Vec<u64> = self.segments.keys().copied().collect();
        ids.sort_unstable();
        ids
    }

    /// Hashes currently placed in `segment_id` (order undefined).
    pub fn hashes_in_segment(&self, segment_id: u64) -> Vec<Vec<u8>> {
        self.by_hash
            .iter()
            .filter(|(_, p)| p.segment_id == segment_id)
            .map(|(h, _)| h.clone())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compact_raises_dense_prefix() {
        let mut a = SegmentArena::new(100, 8);
        a.place_at(b"a", 1, 0).unwrap();
        a.place_at(b"b", 1, 200).unwrap(); // hole at 100
        a.place_at(b"c", 1, 300).unwrap();
        assert!(a.has_holes(1));
        assert!(a.utilization(1) < 1.0);
        let before_dense = a.dense_prefix_slots(1);
        assert_eq!(before_dense, 1);
        let rel = a.compact(1).unwrap();
        assert!(!rel.is_empty());
        assert!(!a.has_holes(1));
        assert_eq!(a.dense_prefix_slots(1), 3);
        assert_eq!(a.placement(b"a").unwrap().offset, 0);
        assert_eq!(a.placement(b"b").unwrap().offset, 100);
        assert_eq!(a.placement(b"c").unwrap().offset, 200);
    }

    #[test]
    fn colocate_moves_to_adjacent_slots() {
        let mut a = SegmentArena::new(64, 8);
        a.place_at(b"h0", 1, 0).unwrap();
        a.place_at(b"h1", 2, 0).unwrap();
        let r = a.relocate(b"h1", 1, 64).unwrap();
        assert_eq!(r.to.segment_id, 1);
        assert_eq!(r.to.offset, 64);
        assert_eq!(a.placement(b"h0").unwrap().segment_id, 1);
        assert_eq!(a.placement(b"h1").unwrap().segment_id, 1);
    }
}
