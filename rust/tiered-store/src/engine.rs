//! 本地分层字节引擎：L0(HBM 站位) / L1(DRAM) / L2(NVMe) / L3(对象存储站位)。
//!
//! 参考：SGLang `hiradix_cache.py::_evict_write_back`（`lock_ref>0` 跳过）+
//! `write_backup` 后再丢 device；Dynamo offload 副作用需回写 presence。
//! 关键差异：无 BlockStore；位置权威在 CP；本 crate 通过 [`TierSideEffects`] 上报副作用。

use std::collections::{HashMap, VecDeque};

use crate::segment::{Placement, Relocate, SegmentArena, DEFAULT_SLOT_BYTES};
use crate::stats::{AccessKind, HitStats};

/// Soft capacity knobs (block counts). `0` = unlimited.
#[derive(Debug, Clone, Copy)]
pub struct TierCaps {
    pub l0: usize,
    pub l1: usize,
    pub l2: usize,
}

impl Default for TierCaps {
    fn default() -> Self {
        Self {
            l0: 64,
            l1: 256,
            l2: 1024,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LocalTier {
    L0,
    L1,
    L2,
    L3,
}

/// Collateral tier mutations that **must** be published to the controlplane view.
///
/// Mirrors Dynamo transfer→presence apply: engine mutates bytes, caller syncs markers.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct TierSideEffects {
    pub l0_demoted: Vec<Vec<u8>>,
    pub l2_demoted_to_l3: Vec<Vec<u8>>,
}

impl TierSideEffects {
    pub fn merge(&mut self, other: TierSideEffects) {
        self.l0_demoted.extend(other.l0_demoted);
        self.l2_demoted_to_l3.extend(other.l2_demoted_to_l3);
    }

    pub fn is_empty(&self) -> bool {
        self.l0_demoted.is_empty() && self.l2_demoted_to_l3.is_empty()
    }
}

/// One L2→L3 demote — enough to undo if the enclosing op fails.
struct L2Demote {
    hash: Vec<u8>,
    /// Bytes removed from L2 (restored on undo even when L3 already had SSOT).
    l2_bytes: Vec<u8>,
    /// Whether this demote inserted into L3 (`entry` was vacant).
    inserted_l3: bool,
}

struct L2DemoteBatch(Vec<L2Demote>);

impl L2DemoteBatch {
    fn into_hashes(self) -> Vec<Vec<u8>> {
        self.0.into_iter().map(|d| d.hash).collect()
    }
}

/// In-process L0–L3 byte maps + LRU + local pin (≈ SGLang `lock_ref`).
pub struct LocalTierEngine {
    caps: TierCaps,
    l0: HashMap<Vec<u8>, Vec<u8>>,
    l1: HashMap<Vec<u8>, Vec<u8>>,
    l2: HashMap<Vec<u8>, Vec<u8>>,
    l3: HashMap<Vec<u8>, Vec<u8>>,
    l0_order: VecDeque<Vec<u8>>,
    l1_order: VecDeque<Vec<u8>>,
    l2_order: VecDeque<Vec<u8>>,
    /// Local freeze (request / writeback / in-flight). Demote skips when >0.
    pins: HashMap<Vec<u8>, u32>,
    /// L2 segment layout (P4.8); bytes stay in `l2`, arena tracks segment/offset.
    pub l2_arena: SegmentArena,
    pub stats: HitStats,
}

impl Default for LocalTierEngine {
    fn default() -> Self {
        Self::with_caps(TierCaps::default())
    }
}

impl LocalTierEngine {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_caps(caps: TierCaps) -> Self {
        Self::with_caps_arena(caps, SegmentArena::new(DEFAULT_SLOT_BYTES, 64))
    }

    pub fn with_caps_arena(caps: TierCaps, l2_arena: SegmentArena) -> Self {
        Self {
            caps,
            l0: HashMap::new(),
            l1: HashMap::new(),
            l2: HashMap::new(),
            l3: HashMap::new(),
            l0_order: VecDeque::new(),
            l1_order: VecDeque::new(),
            l2_order: VecDeque::new(),
            pins: HashMap::new(),
            l2_arena,
            stats: HitStats::default(),
        }
    }

    pub fn l2_placement(&self, h: &[u8]) -> Option<Placement> {
        self.l2_arena.placement(h)
    }

    fn note_l2_present(&mut self, h: &[u8]) {
        if self.l2_arena.placement(h).is_none() {
            let _ = self.l2_arena.alloc(h);
        }
    }

    fn note_l2_absent(&mut self, h: &[u8]) {
        let _ = self.l2_arena.free(h);
    }

    /// Compact one L2 segment. Fails if any resident block is pinned.
    pub fn compact_l2_segment(&mut self, segment_id: u64) -> Result<Vec<Relocate>, String> {
        for hash in self.l2_arena.hashes_in_segment(segment_id) {
            if self.pin_count(&hash) > 0 {
                return Err("compact: pinned block in segment".into());
            }
        }
        self.l2_arena.compact(segment_id)
    }

    /// Co-locate move on L2 arena. Fails if pinned or hash not on L2.
    pub fn colocate_l2_move(
        &mut self,
        hash: &[u8],
        dest_segment: u64,
        dest_offset: u64,
    ) -> Result<Relocate, String> {
        if self.pin_count(hash) > 0 {
            return Err("colocate: pinned".into());
        }
        if !self.l2.contains_key(hash) {
            return Err("colocate: not on L2".into());
        }
        self.l2_arena.relocate(hash, dest_segment, dest_offset)
    }

    pub fn caps(&self) -> TierCaps {
        self.caps
    }

    pub fn l0_len(&self) -> usize {
        self.l0.len()
    }
    pub fn l1_len(&self) -> usize {
        self.l1.len()
    }
    pub fn l2_len(&self) -> usize {
        self.l2.len()
    }
    pub fn l3_len(&self) -> usize {
        self.l3.len()
    }

    pub fn is_l2_durable(&self, h: &[u8]) -> bool {
        self.l2.contains_key(h)
    }

    pub fn l3_present(&self, h: &[u8]) -> bool {
        self.l3.contains_key(h)
    }

    pub fn is_settled(&self, h: &[u8]) -> bool {
        self.l2.contains_key(h) || self.l3.contains_key(h)
    }

    pub fn has_durable_backing(&self, h: &[u8]) -> bool {
        self.is_settled(h)
    }

    pub fn pin_count(&self, h: &[u8]) -> u32 {
        self.pins.get(h).copied().unwrap_or(0)
    }

    /// ≈ SGLang `inc_lock_ref` — freeze demote while request/writeback holds the block.
    pub fn pin(&mut self, h: &[u8]) {
        *self.pins.entry(h.to_vec()).or_insert(0) += 1;
    }

    pub fn unpin(&mut self, h: &[u8]) -> Result<(), String> {
        let Some(c) = self.pins.get_mut(h) else {
            return Err("unpin: not pinned".into());
        };
        *c = c.saturating_sub(1);
        if *c == 0 {
            self.pins.remove(h);
        }
        Ok(())
    }

    /// Drop a block from all local tiers (PutEnd register/quota failure rollback).
    ///
    /// Does **not** reverse collateral L2→L3 demotes of *other* hashes from the
    /// same `put_durable` window — those stay. Call only for the session's own
    /// hashes after CP rejected registration (see PutEnd preflight).
    pub fn discard_settled(&mut self, h: &[u8]) {
        self.l0.remove(h);
        self.l1.remove(h);
        self.l2.remove(h);
        self.l3.remove(h);
        self.l0_order.retain(|x| x.as_slice() != h);
        self.l1_order.retain(|x| x.as_slice() != h);
        self.l2_order.retain(|x| x.as_slice() != h);
        self.pins.remove(h);
        self.note_l2_absent(h);
    }

    pub(crate) fn l0_nbytes(&self, h: &[u8]) -> u64 {
        self.l0.get(h).map(|b| b.len() as u64).unwrap_or(0)
    }

    pub(crate) fn l2_nbytes(&self, h: &[u8]) -> u64 {
        self.l2.get(h).map(|b| b.len() as u64).unwrap_or(0)
    }

    /// Multi-hop promote cost estimate for `BandwidthPool` / `HitStats`.
    ///
    /// Hop 语义（与 [`promote_to_l0`] 实际 insert 次数对齐，供 P7 校准）：
    /// - 已在 L0 → `0`
    /// - 仅缺 L0（已在 L1）→ `1 * n`（L1→L0）
    /// - 在 L2、无 L1 → `2 * n`（L2→L1 + L1→L0）
    /// - 仅在 L3 → `3 * n`（L3→L2 fill + L2→L1 + L1→L0）
    ///
    /// 这是字节×跳数的骨架模型，不是实测带宽；真 workload 曲线见 `00-plan` P7 /
    /// #20（PR #31 review §4.5）。
    pub fn estimate_promote_cost(&self, h: &[u8]) -> u64 {
        let Some(n) = self.get(h).map(|b| b.len() as u64) else {
            return 0;
        };
        if self.l0.contains_key(h) {
            return 0;
        }
        let mut hops = 1u64; // into L0
        if !self.l1.contains_key(h) {
            hops += 1;
        }
        if !self.l2.contains_key(h) && self.l3.contains_key(h) {
            hops += 1; // L3→L2 fill
        }
        n * hops
    }

    fn touch(order: &mut VecDeque<Vec<u8>>, h: &[u8]) {
        order.retain(|x| x.as_slice() != h);
        order.push_back(h.to_vec());
    }

    /// PutEnd / 满块写回：只落 L2（F4 恢复点）。
    ///
    /// 对齐 `storage-layer.md`：写回不写 L1、不写 L3。L3 仅经 [`demote_l2_to_l3`]
    /// 或 L2 容量压力（迁移窗后稳态 XOR）。参考：SGLang 满块不写 host；
    /// Mooncake PutEnd 完成当前副本；Dynamo offload 链式 demote 而非 Put 双写。
    ///
    /// `ensure_l2_cap` 失败时**回滚本窗全部 L2→L3 demote**（含本次 hash 被挤到 L3
    /// 的情况），避免 Err 仍 `is_settled` / 污染 L3。
    ///
    /// 覆盖写（同 hash 已有 L2）失败时**恢复原先 L2 字节**，不无条件 `remove`
    ///（幂等重放 / 重试安全）。新插入失败则撤掉本次 key。
    pub fn put_durable(
        &mut self,
        h: &[u8],
        bytes: &[u8],
    ) -> Result<(LocalTier, TierSideEffects), String> {
        let prior_l2 = self.l2.get(h).cloned();
        let prior_l2_order = self.l2_order.clone();
        let had_placement = self.l2_arena.placement(h).is_some();
        self.l2.insert(h.to_vec(), bytes.to_vec());
        Self::touch(&mut self.l2_order, h);
        if !had_placement {
            self.note_l2_present(h);
        }
        let l2_demoted_to_l3 = match self.ensure_l2_cap() {
            Ok(d) => d.into_hashes(),
            Err((demoted, e)) => {
                // Undo every demote in this window, then restore pre-call L2.
                self.undo_l2_demotes(&demoted);
                match prior_l2 {
                    Some(old) => {
                        self.l2.insert(h.to_vec(), old);
                        Self::touch(&mut self.l2_order, h);
                    }
                    None => {
                        self.l2.remove(h);
                        self.l2_order.retain(|x| x.as_slice() != h);
                        if !had_placement {
                            self.note_l2_absent(h);
                        }
                    }
                }
                self.l2_order = prior_l2_order;
                return Err(e);
            }
        };
        for victim in &l2_demoted_to_l3 {
            self.note_l2_absent(victim);
        }
        let effects = TierSideEffects {
            l0_demoted: Vec::new(),
            l2_demoted_to_l3,
        };
        if self.l2.contains_key(h) {
            Ok((LocalTier::L2, effects))
        } else if self.l3.contains_key(h) {
            // This hash itself was immediately demoted under L2 cap → L3 only.
            self.note_l2_absent(h);
            Ok((LocalTier::L3, effects))
        } else {
            Err("put_durable: block lost after L2 cap demote".into())
        }
    }

    /// Coldest-first LRU demote L2→L3. On stuck-pinned, returns demotes so far
    /// for caller rollback (put/promote must not leak L3 on Err).
    fn ensure_l2_cap(&mut self) -> Result<L2DemoteBatch, (L2DemoteBatch, String)> {
        let mut demoted = Vec::new();
        let cap = self.caps.l2;
        if cap == 0 {
            return Ok(L2DemoteBatch(demoted));
        }
        let mut skipped = 0usize;
        while self.l2.len() > cap {
            if skipped >= self.l2.len() {
                return Err((
                    L2DemoteBatch(demoted),
                    format!(
                        "l2 over cap ({}>{cap}): all victims pinned — unpin before put",
                        self.l2.len()
                    ),
                ));
            }
            let Some(victim) = self.l2_order.pop_front() else {
                break;
            };
            if !self.l2.contains_key(&victim) {
                continue;
            }
            if self.pin_count(&victim) > 0 {
                self.l2_order.push_back(victim);
                skipped += 1;
                continue;
            }
            skipped = 0;
            if let Some(bytes) = self.l2.remove(&victim) {
                let inserted_l3 = !self.l3.contains_key(&victim);
                if inserted_l3 {
                    self.l3.insert(victim.clone(), bytes.clone());
                }
                // else: L3 already holds SSOT; drop L2 copy (same as `or_insert`).
                demoted.push(L2Demote {
                    hash: victim,
                    l2_bytes: bytes,
                    inserted_l3,
                });
            }
        }
        Ok(L2DemoteBatch(demoted))
    }

    fn undo_l2_demotes(&mut self, demoted: &L2DemoteBatch) {
        for d in demoted.0.iter().rev() {
            if d.inserted_l3 {
                self.l3.remove(&d.hash);
            }
            self.l2.insert(d.hash.clone(), d.l2_bytes.clone());
            Self::touch(&mut self.l2_order, &d.hash);
            self.note_l2_present(&d.hash);
        }
    }

    pub fn probe(&mut self, h: &[u8]) -> AccessKind {
        let kind = if self.l0.contains_key(h) {
            AccessKind::L0Hit
        } else if self.l1.contains_key(h) {
            AccessKind::L1Hit
        } else if self.l2.contains_key(h) {
            AccessKind::L2Hit
        } else if self.l3.contains_key(h) {
            AccessKind::L3Hit
        } else {
            AccessKind::Miss
        };
        self.stats.record(kind);
        kind
    }

    pub fn get(&self, h: &[u8]) -> Option<&[u8]> {
        self.l0
            .get(h)
            .or_else(|| self.l1.get(h))
            .or_else(|| self.l2.get(h))
            .or_else(|| self.l3.get(h))
            .map(|v| v.as_slice())
    }

    pub fn local_tier(&self, h: &[u8]) -> Option<LocalTier> {
        if self.l0.contains_key(h) {
            Some(LocalTier::L0)
        } else if self.l1.contains_key(h) {
            Some(LocalTier::L1)
        } else if self.l2.contains_key(h) {
            Some(LocalTier::L2)
        } else if self.l3.contains_key(h) {
            Some(LocalTier::L3)
        } else {
            None
        }
    }

    /// Promote toward L0. Returns `(bytes_moved_estimate, collateral effects)`.
    pub fn promote_to_l0(&mut self, h: &[u8]) -> Result<(u64, TierSideEffects), String> {
        let mut effects = TierSideEffects::default();
        if self.l0.contains_key(h) {
            Self::touch(&mut self.l0_order, h);
            return Ok((0, effects));
        }
        let bytes = self
            .get(h)
            .ok_or_else(|| "promote: block missing all tiers".to_string())?
            .to_vec();
        let n = self.estimate_promote_cost(h).max(bytes.len() as u64);
        if !self.l2.contains_key(h) && self.l3.contains_key(h) {
            self.l2.insert(h.to_vec(), bytes.clone());
            Self::touch(&mut self.l2_order, h);
            self.note_l2_present(h);
            match self.ensure_l2_cap() {
                Ok(d) => {
                    for victim in &d.0 {
                        self.note_l2_absent(&victim.hash);
                    }
                    effects.l2_demoted_to_l3 = d.into_hashes();
                }
                Err((demoted, e)) => {
                    self.undo_l2_demotes(&demoted);
                    self.l2.remove(h);
                    self.l2_order.retain(|x| x.as_slice() != h);
                    self.note_l2_absent(h);
                    return Err(e);
                }
            }
            if !self.is_settled(h) {
                return Err("promote: lost durable backing under L2 cap".into());
            }
        }
        if !self.l1.contains_key(h) {
            self.ensure_l1_room();
            self.l1.insert(h.to_vec(), bytes.clone());
            Self::touch(&mut self.l1_order, h);
        }
        effects.merge(self.ensure_l0_room()?);
        self.l0.insert(h.to_vec(), bytes);
        Self::touch(&mut self.l0_order, h);
        Ok((n, effects))
    }

    /// Demote L0. Requires durable backing; refuses if locally pinned (`lock_ref`).
    pub fn demote_l0(&mut self, h: &[u8]) -> Result<u64, String> {
        if self.pin_count(h) > 0 {
            return Err("demote_l0: pinned (lock_ref)".into());
        }
        if !self.has_durable_backing(h) {
            return Err("demote_l0: need L2 or L3 durable backing".into());
        }
        let n = self.l0.remove(h).map(|b| b.len() as u64).unwrap_or(0);
        self.l0_order.retain(|x| x.as_slice() != h);
        Ok(n)
    }

    pub fn fill_read_miss(&mut self, h: &[u8]) -> Result<(u64, TierSideEffects), String> {
        if self.l0.contains_key(h) {
            Self::touch(&mut self.l0_order, h);
            return Ok((0, TierSideEffects::default()));
        }
        self.promote_to_l0(h)
    }

    pub fn evict_l0_pressure(&mut self) -> Result<Option<Vec<u8>>, String> {
        if self.caps.l0 == 0 || self.l0.len() < self.caps.l0 {
            return Ok(None);
        }
        let victim = self
            .l0_order
            .iter()
            .find(|h| self.pin_count(h) == 0 && self.has_durable_backing(h.as_slice()))
            .cloned()
            .ok_or_else(|| {
                "evict_l0: no durable unpinned victim (pin/writeback first)".to_string()
            })?;
        self.demote_l0(&victim)?;
        Ok(Some(victim))
    }

    fn ensure_l0_room(&mut self) -> Result<TierSideEffects, String> {
        let mut effects = TierSideEffects::default();
        let cap = self.caps.l0;
        if cap == 0 {
            return Ok(effects);
        }
        while self.l0.len() >= cap {
            match self.evict_l0_pressure()? {
                Some(v) => effects.l0_demoted.push(v),
                None => return Err("l0 full: cannot make room".into()),
            }
        }
        Ok(effects)
    }

    /// Drop coldest L1 until under cap.
    ///
    /// **静默、不进 [`TierSideEffects`]**：P4.3 从不把 L1 presence 发布到 CP
    ///（PutEnd/`register_request` 只报 L2；promote 只发 L0；L1=demotion-only）。
    /// 因此 L1 drop 无需 revoke。`LocationEvent::Present{L1}` / `apply_location_events`
    /// 的 L1 arm 为未来「满块顺便写 L1 / P7」预留，当前无生产者——若开始发 L1
    /// 事件，此处必须同步上报 Absent，否则视图漂移（#20；PR #31 review §4.2）。
    fn ensure_l1_room(&mut self) {
        let cap = self.caps.l1;
        if cap == 0 {
            return;
        }
        while self.l1.len() >= cap {
            let Some(v) = self.l1_order.pop_front() else {
                break;
            };
            self.l1.remove(&v);
        }
    }

    /// Steady L2/L3 XOR: move L2 → L3, drop L2.
    pub fn demote_l2_to_l3(&mut self, h: &[u8]) -> Result<u64, String> {
        if self.pin_count(h) > 0 {
            return Err("demote_l2: pinned".into());
        }
        let bytes = self
            .l2
            .remove(h)
            .ok_or_else(|| "demote_l2: not on L2".to_string())?;
        let n = bytes.len() as u64;
        self.l2_order.retain(|x| x.as_slice() != h);
        self.l3.insert(h.to_vec(), bytes);
        self.note_l2_absent(h);
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn durable_promote_demote() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 2,
            l1: 4,
            l2: 8,
        });
        e.put_durable(b"a", b"A").unwrap();
        assert!(!e.l3_present(b"a"), "PutEnd must not dual-write L3");
        e.promote_to_l0(b"a").unwrap();
        assert_eq!(e.local_tier(b"a"), Some(LocalTier::L0));
        e.demote_l0(b"a").unwrap();
        assert_eq!(e.local_tier(b"a"), Some(LocalTier::L1));
    }

    #[test]
    fn l0_pressure_reports_collateral() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        e.put_durable(b"c0", b"0").unwrap();
        e.put_durable(b"c1", b"1").unwrap();
        e.promote_to_l0(b"c0").unwrap();
        let (_, fx) = e.promote_to_l0(b"c1").unwrap();
        assert_eq!(fx.l0_demoted, vec![b"c0".to_vec()]);
        assert_eq!(e.l0_len(), 1);
    }

    #[test]
    fn read_miss_from_l3() {
        let mut e = LocalTierEngine::new();
        e.l3.insert(b"z".to_vec(), b"ZZ".to_vec());
        e.fill_read_miss(b"z").unwrap();
        assert_eq!(e.local_tier(b"z"), Some(LocalTier::L0));
        assert!(e.is_l2_durable(b"z"));
    }

    #[test]
    fn l0_demote_allows_l3_only_backing() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        e.put_durable(b"p", b"P").unwrap();
        e.promote_to_l0(b"p").unwrap();
        e.demote_l2_to_l3(b"p").unwrap();
        assert!(!e.is_l2_durable(b"p") && e.l3_present(b"p"));
        e.put_durable(b"q", b"Q").unwrap();
        let (_, fx) = e.promote_to_l0(b"q").unwrap();
        assert_eq!(fx.l0_demoted, vec![b"p".to_vec()]);
        assert_eq!(e.l0_len(), 1);
    }

    #[test]
    fn pin_blocks_demote_like_lock_ref() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        e.put_durable(b"p", b"P").unwrap();
        e.promote_to_l0(b"p").unwrap();
        e.pin(b"p");
        e.put_durable(b"q", b"Q").unwrap();
        assert!(e.promote_to_l0(b"q").is_err());
        e.unpin(b"p").unwrap();
        let (_, fx) = e.promote_to_l0(b"q").unwrap();
        assert_eq!(fx.l0_demoted, vec![b"p".to_vec()]);
    }

    #[test]
    fn put_durable_l2_only_l3_via_cap_xor() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        let (tx, d0) = e.put_durable(b"x", b"X").unwrap();
        assert_eq!(tx, LocalTier::L2);
        assert!(!e.l3_present(b"x"));
        assert!(d0.is_empty());
        // Cap pressure moves cold x L2→L3 (XOR), not PutEnd dual-write.
        let (ty, demoted) = e.put_durable(b"y", b"Y").unwrap();
        assert_eq!(ty, LocalTier::L2);
        assert_eq!(demoted.l2_demoted_to_l3, vec![b"x".to_vec()]);
        assert!(!e.is_l2_durable(b"x") && e.l3_present(b"x"));
        assert!(e.is_l2_durable(b"y") && !e.l3_present(b"y"));
    }

    #[test]
    fn pinned_cold_skipped_new_write_may_land_l3() {
        // x pinned → cap demote skips x; unpinned y can be demoted to L3 (still settled).
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        e.put_durable(b"x", b"X").unwrap();
        e.pin(b"x");
        let (ty, fx) = e.put_durable(b"y", b"Y").unwrap();
        assert_eq!(ty, LocalTier::L3);
        assert_eq!(fx.l2_demoted_to_l3, vec![b"y".to_vec()]);
        assert!(e.is_l2_durable(b"x") && e.l3_present(b"y"));
    }

    #[test]
    fn all_pinned_l2_over_cap_errors_and_rolls_back() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        e.put_durable(b"x", b"X").unwrap();
        e.pin(b"x");
        // Force a second pinned L2 resident (bypass put_durable).
        e.l2.insert(b"z".to_vec(), b"Z".to_vec());
        e.l2_order.push_back(b"z".to_vec());
        e.pin(b"z");
        assert_eq!(e.l2_len(), 2);
        let err = e.put_durable(b"y", b"Y").unwrap_err();
        assert!(err.contains("pinned"), "{err}");
        assert!(!e.is_l2_durable(b"y"));
        assert!(
            !e.l3_present(b"y"),
            "Err must not leave y on L3 (cap demote then stuck-pinned)"
        );
        assert!(!e.is_settled(b"y"));
        assert_eq!(e.l2_len(), 2, "rollback y only");
        // Pre-existing pinned L2 residents untouched.
        assert!(e.is_l2_durable(b"x") && e.is_l2_durable(b"z"));
    }

    #[test]
    fn put_durable_err_restores_prior_l2_on_overwrite() {
        // Cap=2 with x,y pinned at capacity; force z pinned → over cap.
        // Overwrite y fails cap; must keep OLD bytes (not wipe the key).
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 2,
        });
        e.put_durable(b"y", b"OLD").unwrap();
        e.put_durable(b"x", b"X").unwrap();
        e.pin(b"x");
        e.pin(b"y");
        e.l2.insert(b"z".to_vec(), b"Z".to_vec());
        e.l2_order.push_back(b"z".to_vec());
        e.pin(b"z");
        assert_eq!(e.l2_len(), 3);
        let prior_order = e.l2_order.clone();
        let err = e.put_durable(b"y", b"NEW").unwrap_err();
        assert!(err.contains("pinned"), "{err}");
        assert_eq!(e.get(b"y"), Some(b"OLD".as_slice()));
        assert!(e.is_l2_durable(b"y"));
        assert!(!e.l3_present(b"y"));
        assert_eq!(e.l2_order, prior_order);
    }

    #[test]
    fn put_durable_err_also_undoes_collateral_l2_demotes() {
        // Cap=1; x pinned. Insert unpinned w on L2 (over cap). put y: demotes w to L3
        // then stuck (x pinned, y may demote…) — any Err must restore pre-put L2/L3.
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        e.put_durable(b"x", b"X").unwrap();
        e.pin(b"x");
        e.l2.insert(b"w".to_vec(), b"W".to_vec());
        e.l2_order.push_front(b"w".to_vec()); // colder than x
        assert_eq!(e.l2_len(), 2);
        // Second pinned resident so after demoting w (+ maybe y) we still stick.
        e.l2.insert(b"z".to_vec(), b"Z".to_vec());
        e.l2_order.push_back(b"z".to_vec());
        e.pin(b"z");
        assert_eq!(e.l2_len(), 3);
        let prior_order = e.l2_order.clone();
        let err = e.put_durable(b"y", b"Y").unwrap_err();
        assert!(err.contains("pinned"), "{err}");
        assert!(!e.l3_present(b"y") && !e.is_settled(b"y"));
        // Collateral demote of w must be undone.
        assert!(e.is_l2_durable(b"w"), "w restored to L2");
        assert!(!e.l3_present(b"w"), "w not left on L3");
        assert_eq!(e.get(b"w"), Some(b"W".as_slice()));
        assert_eq!(e.l2_order, prior_order);
    }
}
