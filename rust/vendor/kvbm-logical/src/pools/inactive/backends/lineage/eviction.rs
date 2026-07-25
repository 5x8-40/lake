// SPDX-FileCopyrightText: Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Pluggable leaf-eviction ordering for [`LineageBackend`](super::LineageBackend).
//!
//! The lineage graph only ever evicts *leaves*; *which* leaf goes first is
//! the one policy decision that varies. [`LeafPolicy`] isolates it behind a
//! small set of hooks the backend calls as nodes are inserted, change
//! leaf/interior status, and are removed:
//!
//! - [`on_node_inserted`](LeafPolicy::on_node_inserted) — a slot became a
//!   `Real` node (fresh insert or ghost promotion). Passes `SequenceHash`
//!   so frequency-aware policies can bucket by TinyLFU.
//! - [`on_leaf_added`](LeafPolicy::on_leaf_added) — a `Real` node is now a
//!   leaf and enters the eviction order.
//! - [`on_leaf_demoted`](LeafPolicy::on_leaf_demoted) — a `Real` leaf
//!   gained a child; still in the graph, no longer evictable. Per-node
//!   ordering state is *retained* (a future re-leafing may need it).
//! - [`on_node_removed`](LeafPolicy::on_node_removed) — a `Real` node left
//!   the graph (evicted or resurrected). Per-node ordering state is cleared.
//! - [`next_victim`](LeafPolicy::next_victim) — the eviction-order head.
//!
//! ## Variants
//!
//! - [`Fifo`](LeafPolicy::Fifo) — an intrusive FIFO over leaves: O(1) per
//!   hook, **zero heap allocation in steady state** (a pre-sized link
//!   array). A node that *re-becomes* a leaf appends at the tail.
//! - [`Tick`](LeafPolicy::Tick) — a `BTreeMap` ordered by a monotonic
//!   per-node insertion tick: a re-leafed node retains its *original*
//!   position. This is the historical lineage-backend behavior and the
//!   default. Costs O(log n) per hook and B-tree node churn — it is the
//!   only structure here that is not pre-sized.
//! - [`Frequency`](LeafPolicy::Frequency) — bucket leaves by TinyLFU count
//!   (same thresholds shape as [`super::super::MultiLruBackend`]), evict
//!   cold tiers first; within a tier, LRU among leaves. lake P4.2 default
//!   for Authority (`LineageBackend::with_frequency`).

use std::collections::BTreeMap;
use std::num::NonZeroUsize;
use std::sync::Arc;

use anyhow::{Result, bail};
use lru::LruCache;

use crate::blocks::SequenceHash;
use crate::tinylfu::FrequencyTracker;

/// Leaf-eviction ordering strategy for `LineageBackend`. See the module docs.
pub(crate) enum LeafPolicy {
    Fifo(FifoPolicy),
    Tick(TickPolicy),
    /// Boxed: FrequencyPolicy holds 4× LruCache and dwarfs Fifo/Tick.
    Frequency(Box<FrequencyPolicy>),
}

impl LeafPolicy {
    /// Intrusive-FIFO policy, pre-sized for `capacity` slots.
    pub(crate) fn fifo(capacity: usize) -> Self {
        Self::Fifo(FifoPolicy::with_capacity(capacity))
    }

    /// Insertion-tick `BTreeMap` policy, pre-sized for `capacity` slots.
    pub(crate) fn tick(capacity: usize) -> Self {
        Self::Tick(TickPolicy::with_capacity(capacity))
    }

    /// TinyLFU-tiered leaf order (cold tiers first). Thresholds must match
    /// MultiLru rules: ascending, `t0 >= 1`, `t2 <= 15`.
    pub(crate) fn frequency(
        capacity: usize,
        thresholds: [u8; 3],
        frequency_tracker: Arc<dyn FrequencyTracker<u128>>,
    ) -> Result<Self> {
        Ok(Self::Frequency(Box::new(FrequencyPolicy::new(
            capacity,
            thresholds,
            frequency_tracker,
        )?)))
    }

    /// A slot just became a `Real` node (fresh insert or ghost promotion).
    pub(crate) fn on_node_inserted(&mut self, idx: u32, seq_hash: SequenceHash) {
        match self {
            Self::Fifo(_) => {}
            Self::Tick(p) => p.on_node_inserted(idx),
            Self::Frequency(p) => p.on_node_inserted(idx, seq_hash),
        }
    }

    /// A `Real` node is now a leaf — add it to the eviction order.
    pub(crate) fn on_leaf_added(&mut self, idx: u32) {
        match self {
            Self::Fifo(p) => p.on_leaf_added(idx),
            Self::Tick(p) => p.on_leaf_added(idx),
            Self::Frequency(p) => p.on_leaf_added(idx),
        }
    }

    /// A `Real` leaf gained a child — remove it from the eviction order but
    /// keep its per-node ordering state for a possible later re-leafing.
    pub(crate) fn on_leaf_demoted(&mut self, idx: u32) {
        match self {
            Self::Fifo(p) => p.unlink(idx),
            Self::Tick(p) => p.on_leaf_demoted(idx),
            Self::Frequency(p) => p.on_leaf_demoted(idx),
        }
    }

    /// A `Real` node left the graph — drop it from the eviction order and
    /// clear its per-node state so a recycled slot starts fresh.
    pub(crate) fn on_node_removed(&mut self, idx: u32) {
        match self {
            Self::Fifo(p) => p.unlink(idx),
            Self::Tick(p) => p.on_node_removed(idx),
            Self::Frequency(p) => p.on_node_removed(idx),
        }
    }

    /// Slot index of the next block to evict, or `None` if no leaves.
    pub(crate) fn next_victim(&self) -> Option<u32> {
        match self {
            Self::Fifo(p) => p.next_victim(),
            Self::Tick(p) => p.next_victim(),
            Self::Frequency(p) => p.next_victim(),
        }
    }

    /// Number of currently-evictable leaves. Test-only.
    #[cfg(test)]
    pub(crate) fn len(&self) -> usize {
        match self {
            Self::Fifo(p) => p.len(),
            Self::Tick(p) => p.queue.len(),
            Self::Frequency(p) => p.len(),
        }
    }
}

// ---------------------------------------------------------------------------
// FIFO
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
struct FifoLink {
    prev: Option<u32>,
    next: Option<u32>,
}

/// Intrusive doubly-linked FIFO over leaf slots. `links[idx]` is `Some`
/// exactly while slot `idx` is a leaf in the list; the array is pre-sized
/// from the capacity hint, so steady-state hooks do not allocate.
pub(crate) struct FifoPolicy {
    links: Vec<Option<FifoLink>>,
    head: Option<u32>,
    tail: Option<u32>,
}

impl FifoPolicy {
    fn with_capacity(capacity: usize) -> Self {
        Self {
            links: Vec::with_capacity(capacity),
            head: None,
            tail: None,
        }
    }

    /// Grow the link array to cover `idx` (only past the capacity hint).
    fn ensure(&mut self, idx: u32) {
        if idx as usize >= self.links.len() {
            self.links.resize(idx as usize + 1, None);
        }
    }

    fn on_leaf_added(&mut self, idx: u32) {
        self.ensure(idx);
        debug_assert!(
            self.links[idx as usize].is_none(),
            "FifoPolicy: leaf {idx} added while already linked"
        );
        self.links[idx as usize] = Some(FifoLink {
            prev: self.tail,
            next: None,
        });
        match self.tail {
            Some(t) => self.links[t as usize].as_mut().unwrap().next = Some(idx),
            None => self.head = Some(idx),
        }
        self.tail = Some(idx);
    }

    /// Unlink `idx` from the list. No-op if it is not currently a leaf —
    /// `on_node_removed` is called for interior nodes too.
    fn unlink(&mut self, idx: u32) {
        let Some(link) = self.links.get_mut(idx as usize).and_then(Option::take) else {
            return;
        };
        match link.prev {
            Some(p) => self.links[p as usize].as_mut().unwrap().next = link.next,
            None => self.head = link.next,
        }
        match link.next {
            Some(n) => self.links[n as usize].as_mut().unwrap().prev = link.prev,
            None => self.tail = link.prev,
        }
    }

    fn next_victim(&self) -> Option<u32> {
        self.head
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        let mut n = 0;
        let mut cur = self.head;
        while let Some(i) = cur {
            n += 1;
            cur = self.links[i as usize].unwrap().next;
        }
        n
    }
}

// ---------------------------------------------------------------------------
// Tick
// ---------------------------------------------------------------------------

/// `BTreeMap` of `(insertion_tick, slot)` ordered ascending. A node's tick
/// is assigned once, at Real-ification, and survives leaf→interior→leaf
/// transitions — so a re-leafed node returns to its original eviction
/// position. Reproduces the historical lineage-backend ordering exactly.
pub(crate) struct TickPolicy {
    /// `ticks[idx]` is the node's insertion tick while it is `Real`, `None`
    /// once removed (so a recycled slot is re-ticked on its next insert).
    ticks: Vec<Option<u64>>,
    /// Currently-evictable leaves, ordered by `(tick, slot)`. Ticks are
    /// unique per node, so `slot` only keeps the key `Ord` total — it never
    /// actually breaks a tie.
    queue: BTreeMap<(u64, u32), ()>,
    next_tick: u64,
}

impl TickPolicy {
    fn with_capacity(capacity: usize) -> Self {
        Self {
            ticks: Vec::with_capacity(capacity),
            queue: BTreeMap::new(),
            next_tick: 0,
        }
    }

    fn ensure(&mut self, idx: u32) {
        if idx as usize >= self.ticks.len() {
            self.ticks.resize(idx as usize + 1, None);
        }
    }

    fn on_node_inserted(&mut self, idx: u32) {
        self.ensure(idx);
        let tick = self.next_tick;
        self.next_tick += 1;
        self.ticks[idx as usize] = Some(tick);
    }

    fn on_leaf_added(&mut self, idx: u32) {
        let tick =
            self.ticks[idx as usize].expect("TickPolicy: on_leaf_added before on_node_inserted");
        self.queue.insert((tick, idx), ());
    }

    fn on_leaf_demoted(&mut self, idx: u32) {
        // Leave `ticks[idx]` set — a later re-leafing restores this exact
        // `(tick, idx)` key, putting the node back in its original spot.
        if let Some(tick) = self.ticks[idx as usize] {
            self.queue.remove(&(tick, idx));
        }
    }

    fn on_node_removed(&mut self, idx: u32) {
        // `take()` clears the tick; the `queue.remove` is a no-op for an
        // interior node that was demoted before being removed.
        if let Some(tick) = self.ticks[idx as usize].take() {
            self.queue.remove(&(tick, idx));
        }
    }

    fn next_victim(&self) -> Option<u32> {
        self.queue.first_key_value().map(|(&(_, idx), _)| idx)
    }
}

// ---------------------------------------------------------------------------
// Frequency (TinyLFU tiers among leaves — lake / planned Dynamo third arm)
// ---------------------------------------------------------------------------

/// 4-tier frequency-aware leaf order. Mirrors [`super::super::MultiLruBackend`]
/// bucketing, but keys are slab slot indices (leaves only).
pub(crate) struct FrequencyPolicy {
    /// `hashes[idx] = Some(seq.as_u128())` while the slot is `Real`.
    hashes: Vec<Option<u128>>,
    /// Which tier currently holds `idx` as an evictable leaf (`None` if not
    /// in the leaf order — interior, or not yet added).
    tier_of: Vec<Option<u8>>,
    tiers: [LruCache<u32, ()>; 4],
    frequency_tracker: Arc<dyn FrequencyTracker<u128>>,
    frequency_thresholds: [u8; 3],
}

impl FrequencyPolicy {
    fn new(
        capacity: usize,
        thresholds: [u8; 3],
        frequency_tracker: Arc<dyn FrequencyTracker<u128>>,
    ) -> Result<Self> {
        if !(thresholds[0] < thresholds[1] && thresholds[1] < thresholds[2]) {
            bail!("Thresholds must be in ascending order: {:?}", thresholds);
        }
        if thresholds[2] > 15 {
            bail!(
                "Maximum threshold cannot exceed 15 (4-bit counter limit), got: {:?}",
                thresholds
            );
        }
        if thresholds[0] < 1 {
            bail!(
                "Cold threshold must be >= 1 to distinguish from never-accessed blocks, got: {:?}",
                thresholds
            );
        }
        let level_cap = NonZeroUsize::new(capacity.max(1)).expect("capacity+1 > 0");
        Ok(Self {
            hashes: Vec::with_capacity(capacity),
            tier_of: Vec::with_capacity(capacity),
            tiers: [
                LruCache::new(level_cap),
                LruCache::new(level_cap),
                LruCache::new(level_cap),
                LruCache::new(level_cap),
            ],
            frequency_tracker,
            frequency_thresholds: thresholds,
        })
    }

    fn ensure(&mut self, idx: u32) {
        let need = idx as usize + 1;
        if self.hashes.len() < need {
            self.hashes.resize(need, None);
            self.tier_of.resize(need, None);
        }
    }

    fn level_for_hash(&self, hash: u128) -> usize {
        let frequency = self.frequency_tracker.count(hash);
        let [t1, t2, t3] = self.frequency_thresholds;
        if frequency < t1 as u32 {
            0
        } else if frequency < t2 as u32 {
            1
        } else if frequency < t3 as u32 {
            2
        } else {
            3
        }
    }

    fn on_node_inserted(&mut self, idx: u32, seq_hash: SequenceHash) {
        self.ensure(idx);
        self.hashes[idx as usize] = Some(seq_hash.as_u128());
        // Not a leaf yet (or will be added via on_leaf_added).
        debug_assert!(
            self.tier_of[idx as usize].is_none(),
            "FrequencyPolicy: insert while still in a tier"
        );
    }

    fn on_leaf_added(&mut self, idx: u32) {
        let hash = self.hashes[idx as usize]
            .expect("FrequencyPolicy: on_leaf_added before on_node_inserted");
        let level = self.level_for_hash(hash);
        debug_assert!(
            self.tier_of[idx as usize].is_none(),
            "FrequencyPolicy: leaf {idx} added while already in a tier"
        );
        // Align with MultiLruBackend::insert: never silently drop a leaf.
        // lake Authority gates at report_ref (skip insert when inactive is full);
        // this assert is a misuse fuse if a caller bypasses that gate.
        debug_assert!(
            self.tiers[level].len() < self.tiers[level].cap().get(),
            "FrequencyPolicy tier {level} insert would cause eviction! len={}, cap={}. \
             Caller must skip insert (or free a slot) when inactive is at capacity.",
            self.tiers[level].len(),
            self.tiers[level].cap().get()
        );
        self.tiers[level].put(idx, ());
        self.tier_of[idx as usize] = Some(level as u8);
    }

    fn on_leaf_demoted(&mut self, idx: u32) {
        if let Some(level) = self.tier_of[idx as usize].take() {
            let _ = self.tiers[level as usize].pop(&idx);
        }
        // Keep `hashes[idx]` for a later re-leaf.
    }

    fn on_node_removed(&mut self, idx: u32) {
        if let Some(level) = self.tier_of[idx as usize].take() {
            let _ = self.tiers[level as usize].pop(&idx);
        }
        if (idx as usize) < self.hashes.len() {
            self.hashes[idx as usize] = None;
        }
    }

    fn next_victim(&self) -> Option<u32> {
        for tier in &self.tiers {
            if let Some((&idx, _)) = tier.peek_lru() {
                return Some(idx);
            }
        }
        None
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.tiers.iter().map(|t| t.len()).sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tinylfu::TinyLFUTracker;
    use dynamo_tokens::PositionalLineageHash;

    /// FIFO: re-adding a node appends at the tail; unlink is order-preserving.
    #[test]
    fn fifo_order_and_unlink() {
        let mut p = FifoPolicy::with_capacity(8);
        for i in 0..4 {
            p.on_leaf_added(i);
        }
        assert_eq!(p.next_victim(), Some(0));
        assert_eq!(p.len(), 4);

        // Unlink the middle — order of the rest is preserved.
        p.unlink(2);
        assert_eq!(p.len(), 3);
        p.unlink(0);
        assert_eq!(p.next_victim(), Some(1));

        // Re-add 0 — it goes to the tail, not its old position.
        p.on_leaf_added(0);
        assert_eq!(p.next_victim(), Some(1)); // still 1 at head
        // Drain: 1, 3, 0.
        let mut order = Vec::new();
        while let Some(v) = p.next_victim() {
            order.push(v);
            p.unlink(v);
        }
        assert_eq!(order, vec![1, 3, 0]);
    }

    /// Tick: a demoted-then-re-added node returns to its ORIGINAL position.
    #[test]
    fn tick_re_leafed_node_keeps_original_position() {
        let mut p = TickPolicy::with_capacity(8);
        // Insert order (ticks): 0->t0, 1->t1, 2->t2.
        for i in 0..3 {
            p.on_node_inserted(i);
            p.on_leaf_added(i);
        }
        assert_eq!(p.next_victim(), Some(0));

        // Demote 0 (gained a child), then re-add it.
        p.on_leaf_demoted(0);
        assert_eq!(p.next_victim(), Some(1)); // 0 temporarily out
        p.on_leaf_added(0);
        // 0 keeps tick 0 → back at the head, ahead of 1 and 2.
        assert_eq!(p.next_victim(), Some(0));

        // Removing 0 entirely clears its tick; a recycled slot 0 re-ticks
        // to the END, not the front.
        p.on_node_removed(0);
        p.on_node_inserted(0);
        p.on_leaf_added(0);
        let mut order = Vec::new();
        while let Some(v) = p.next_victim() {
            order.push(v);
            p.on_node_removed(v);
        }
        assert_eq!(order, vec![1, 2, 0]);
    }

    #[test]
    fn frequency_evicts_cold_tier_before_hot() {
        let tracker: Arc<dyn FrequencyTracker<u128>> = Arc::new(TinyLFUTracker::new(1024));
        let cold = PositionalLineageHash::root(0x1111);
        let hot = PositionalLineageHash::root(0x2222);
        for _ in 0..64 {
            tracker.touch(hot.as_u128());
        }
        let mut p = FrequencyPolicy::new(8, [3, 8, 15], Arc::clone(&tracker)).unwrap();
        p.on_node_inserted(0, cold);
        p.on_leaf_added(0);
        p.on_node_inserted(1, hot);
        p.on_leaf_added(1);
        assert_eq!(p.next_victim(), Some(0), "cold leaf must precede hot");
    }

    /// Per-tier LruCache must not silently kick a leaf (debug builds).
    #[test]
    #[cfg(debug_assertions)]
    #[should_panic(expected = "would cause eviction")]
    fn frequency_over_tier_cap_panics_in_debug() {
        let tracker: Arc<dyn FrequencyTracker<u128>> = Arc::new(TinyLFUTracker::new(1024));
        let cap = 2;
        let mut p = FrequencyPolicy::new(cap, [3, 8, 15], Arc::clone(&tracker)).unwrap();
        for i in 0..=cap as u32 {
            let seq = PositionalLineageHash::root(0x1000 + u64::from(i));
            p.on_node_inserted(i, seq);
            p.on_leaf_added(i);
        }
    }
}
