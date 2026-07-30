//! Promote / demote / defrag 流水线（抄 Dynamo `Pipeline` 骨架，补 lake promote）。
//!
//! 参考：`kvbm-engine` `offload/pipeline.rs::Pipeline`（transfer 后 settlement /
//! presence）；lake 同步版：`tick` 返回完整 [`LocationEvent`] 列表（含 collateral）。
//! P4.8:Compact / CoLocate 经同一 [`BandwidthPool`]。
//! 关键差异：无 async PendingTracker；失败 requeue 一次窗口尾，不 silent-drop。

use crate::bandwidth::BandwidthPool;
use crate::engine::{LocalTier, LocalTierEngine, TierSideEffects};
use crate::segment::Relocate;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PipelineAction {
    Promote {
        hash: Vec<u8>,
    },
    DemoteL0 {
        hash: Vec<u8>,
    },
    DemoteL2 {
        hash: Vec<u8>,
    },
    FillMiss {
        hash: Vec<u8>,
    },
    /// Physical compaction of one L2 segment.
    CompactSegment {
        segment_id: u64,
    },
    /// Move one L2 block to dest placement (logical co-location).
    CoLocateMove {
        hash: Vec<u8>,
        dest_segment: u64,
        dest_offset: u64,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LocationEvent {
    Present {
        hash: Vec<u8>,
        tier: LocalTier,
    },
    Absent {
        hash: Vec<u8>,
        tier: LocalTier,
    },
    /// Placement changed within a tier (P4.8 defrag).
    Moved {
        hash: Vec<u8>,
        tier: LocalTier,
        segment_id: u64,
        offset: u64,
        node_id: String,
    },
}

fn events_from_effects(fx: &TierSideEffects) -> Vec<LocationEvent> {
    let mut ev = Vec::new();
    for h in &fx.l0_demoted {
        ev.push(LocationEvent::Absent {
            hash: h.clone(),
            tier: LocalTier::L0,
        });
    }
    for h in &fx.l2_demoted_to_l3 {
        ev.push(LocationEvent::Absent {
            hash: h.clone(),
            tier: LocalTier::L2,
        });
        ev.push(LocationEvent::Present {
            hash: h.clone(),
            tier: LocalTier::L3,
        });
    }
    ev
}

fn events_from_relocs(relocs: &[Relocate], node_id: &str) -> Vec<LocationEvent> {
    relocs
        .iter()
        .filter(|r| r.from != r.to)
        .map(|r| LocationEvent::Moved {
            hash: r.hash.clone(),
            tier: LocalTier::L2,
            segment_id: r.to.segment_id,
            offset: r.to.offset,
            node_id: node_id.to_string(),
        })
        .collect()
}

pub struct TierPipeline {
    pub engine: LocalTierEngine,
    pub bandwidth: BandwidthPool,
    /// Node id stamped on Moved events (single-process mock).
    pub node_id: String,
    queue: std::collections::VecDeque<PipelineAction>,
}

impl TierPipeline {
    pub fn new(engine: LocalTierEngine, bandwidth: BandwidthPool) -> Self {
        Self {
            engine,
            bandwidth,
            node_id: "local".into(),
            queue: std::collections::VecDeque::new(),
        }
    }

    pub fn with_node_id(mut self, node_id: impl Into<String>) -> Self {
        self.node_id = node_id.into();
        self
    }

    pub fn enqueue(&mut self, action: PipelineAction) {
        self.queue.push_back(action);
    }

    pub fn pending(&self) -> usize {
        self.queue.len()
    }

    fn estimate_cost(&self, action: &PipelineAction) -> u64 {
        match action {
            PipelineAction::Promote { hash } | PipelineAction::FillMiss { hash } => {
                self.engine.estimate_promote_cost(hash)
            }
            PipelineAction::DemoteL0 { hash } => self.engine.l0_nbytes(hash),
            PipelineAction::DemoteL2 { hash } => self.engine.l2_nbytes(hash),
            PipelineAction::CompactSegment { segment_id } => {
                // Charge live slots * slot_bytes (layout rewrite cost).
                let n = self.engine.l2_arena.hashes_in_segment(*segment_id).len() as u64;
                n * self.engine.l2_arena.slot_bytes()
            }
            PipelineAction::CoLocateMove { hash, .. } => self
                .engine
                .l2_nbytes(hash)
                .max(self.engine.l2_arena.slot_bytes()),
        }
    }

    /// Run up to `max_steps` attempts. Returns `(successes, location_events)`.
    /// Failed actions are **requeued at the back** (Dynamo PendingTracker spirit);
    /// bandwidth exhaustion stops the window without dropping the head.
    pub fn tick(&mut self, max_steps: usize) -> (usize, Vec<LocationEvent>) {
        let mut done = 0;
        let mut events = Vec::new();
        let mut attempts = 0;
        let mut consecutive_fail = 0usize;
        let q0 = self.queue.len().max(1);
        while attempts < max_steps {
            let Some(action) = self.queue.pop_front() else {
                break;
            };
            attempts += 1;
            let cost = self.estimate_cost(&action);
            if cost > 0 && !self.bandwidth.try_consume(cost) {
                self.queue.push_front(action);
                break;
            }
            let node = self.node_id.clone();
            match &action {
                PipelineAction::Promote { hash } => match self.engine.promote_to_l0(hash) {
                    Ok((_n, fx)) => {
                        events.extend(events_from_effects(&fx));
                        if self.engine.local_tier(hash) == Some(LocalTier::L0) {
                            events.push(LocationEvent::Present {
                                hash: hash.clone(),
                                tier: LocalTier::L0,
                            });
                        }
                        done += 1;
                        consecutive_fail = 0;
                    }
                    Err(_) => {
                        if cost > 0 {
                            self.bandwidth.refund(cost);
                        }
                        self.queue.push_back(action);
                        consecutive_fail += 1;
                        if consecutive_fail >= q0 {
                            break;
                        }
                    }
                },
                PipelineAction::FillMiss { hash } => match self.engine.fill_read_miss(hash) {
                    Ok((_n, fx)) => {
                        events.extend(events_from_effects(&fx));
                        if self.engine.local_tier(hash) == Some(LocalTier::L0) {
                            events.push(LocationEvent::Present {
                                hash: hash.clone(),
                                tier: LocalTier::L0,
                            });
                        }
                        done += 1;
                        consecutive_fail = 0;
                    }
                    Err(_) => {
                        if cost > 0 {
                            self.bandwidth.refund(cost);
                        }
                        self.queue.push_back(action);
                        consecutive_fail += 1;
                        if consecutive_fail >= q0 {
                            break;
                        }
                    }
                },
                PipelineAction::DemoteL0 { hash } => match self.engine.demote_l0(hash) {
                    Ok(_) => {
                        events.push(LocationEvent::Absent {
                            hash: hash.clone(),
                            tier: LocalTier::L0,
                        });
                        done += 1;
                        consecutive_fail = 0;
                    }
                    Err(_) => {
                        if cost > 0 {
                            self.bandwidth.refund(cost);
                        }
                        self.queue.push_back(action);
                        consecutive_fail += 1;
                        if consecutive_fail >= q0 {
                            break;
                        }
                    }
                },
                PipelineAction::DemoteL2 { hash } => match self.engine.demote_l2_to_l3(hash) {
                    Ok(_) => {
                        events.push(LocationEvent::Absent {
                            hash: hash.clone(),
                            tier: LocalTier::L2,
                        });
                        events.push(LocationEvent::Present {
                            hash: hash.clone(),
                            tier: LocalTier::L3,
                        });
                        done += 1;
                        consecutive_fail = 0;
                    }
                    Err(_) => {
                        if cost > 0 {
                            self.bandwidth.refund(cost);
                        }
                        self.queue.push_back(action);
                        consecutive_fail += 1;
                        if consecutive_fail >= q0 {
                            break;
                        }
                    }
                },
                PipelineAction::CompactSegment { segment_id } => {
                    match self.engine.compact_l2_segment(*segment_id) {
                        Ok(relocs) => {
                            events.extend(events_from_relocs(&relocs, &node));
                            done += 1;
                            consecutive_fail = 0;
                        }
                        Err(_) => {
                            if cost > 0 {
                                self.bandwidth.refund(cost);
                            }
                            self.queue.push_back(action);
                            consecutive_fail += 1;
                            if consecutive_fail >= q0 {
                                break;
                            }
                        }
                    }
                }
                PipelineAction::CoLocateMove {
                    hash,
                    dest_segment,
                    dest_offset,
                } => match self
                    .engine
                    .colocate_l2_move(hash, *dest_segment, *dest_offset)
                {
                    Ok(r) => {
                        events.extend(events_from_relocs(&[r], &node));
                        done += 1;
                        consecutive_fail = 0;
                    }
                    Err(_) => {
                        if cost > 0 {
                            self.bandwidth.refund(cost);
                        }
                        self.queue.push_back(action);
                        consecutive_fail += 1;
                        if consecutive_fail >= q0 {
                            break;
                        }
                    }
                },
            }
        }
        (done, events)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::TierCaps;
    use crate::segment::SegmentArena;

    #[test]
    fn pipeline_promote_emits_collateral_l0_absent() {
        let eng = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        let mut p = TierPipeline::new(eng, BandwidthPool::new(1 << 20));
        p.engine.put_durable(b"a", b"A").unwrap();
        p.engine.put_durable(b"b", b"B").unwrap();
        p.enqueue(PipelineAction::Promote {
            hash: b"a".to_vec(),
        });
        let (n, ev) = p.tick(1);
        assert_eq!(n, 1);
        p.enqueue(PipelineAction::Promote {
            hash: b"b".to_vec(),
        });
        let (n2, ev2) = p.tick(1);
        assert_eq!(n2, 1);
        assert!(
            ev2.iter().any(|e| matches!(
                e,
                LocationEvent::Absent {
                    hash,
                    tier: LocalTier::L0
                } if hash == b"a"
            )),
            "collateral demote must emit Absent L0; got {ev2:?} (first tick {ev:?})"
        );
    }

    #[test]
    fn pipeline_stops_when_budget_exhausted() {
        let eng = LocalTierEngine::with_caps(TierCaps::default());
        let mut p = TierPipeline::new(eng, BandwidthPool::new(3));
        p.engine.put_durable(b"a", b"abcd").unwrap();
        p.enqueue(PipelineAction::Promote {
            hash: b"a".to_vec(),
        });
        let (n, _) = p.tick(1);
        assert_eq!(n, 0);
        assert_eq!(p.pending(), 1);
        assert_ne!(p.engine.local_tier(b"a"), Some(LocalTier::L0));
    }

    #[test]
    fn pipeline_err_requeues() {
        let eng = LocalTierEngine::with_caps(TierCaps::default());
        let mut p = TierPipeline::new(eng, BandwidthPool::new(1024));
        p.enqueue(PipelineAction::DemoteL0 {
            hash: b"missing".to_vec(),
        });
        let (n, _) = p.tick(1);
        assert_eq!(n, 0);
        assert_eq!(p.pending(), 1, "failed action requeued");
    }

    #[test]
    fn pipeline_compact_emits_moved_and_pause_blocks() {
        let arena = SegmentArena::new(100, 8);
        let eng = LocalTierEngine::with_caps_arena(TierCaps::default(), arena);
        let mut p = TierPipeline::new(eng, BandwidthPool::new(1 << 20)).with_node_id("n0");
        // Manually scatter placements with holes.
        p.engine.put_durable(b"a", b"A").unwrap();
        p.engine.put_durable(b"b", b"B").unwrap();
        p.engine.put_durable(b"c", b"C").unwrap();
        // Force hole: free mid-slot by relocating after place_at dance.
        let _ = p.engine.l2_arena.free(b"a");
        let _ = p.engine.l2_arena.free(b"b");
        let _ = p.engine.l2_arena.free(b"c");
        p.engine.l2_arena.place_at(b"a", 1, 0).unwrap();
        p.engine.l2_arena.place_at(b"b", 1, 200).unwrap();
        p.engine.l2_arena.place_at(b"c", 1, 300).unwrap();
        assert!(p.engine.l2_arena.has_holes(1));

        p.bandwidth.pause();
        p.enqueue(PipelineAction::CompactSegment { segment_id: 1 });
        let (n, _) = p.tick(1);
        assert_eq!(n, 0);
        assert!(p.engine.l2_arena.has_holes(1));

        p.bandwidth.resume();
        p.bandwidth.reset_window();
        let (n2, ev) = p.tick(1);
        assert_eq!(n2, 1);
        assert!(!p.engine.l2_arena.has_holes(1));
        assert!(ev.iter().any(|e| matches!(e, LocationEvent::Moved { .. })));
    }

    #[test]
    fn pipeline_colocate_adjacent() {
        let arena = SegmentArena::new(64, 8);
        let eng = LocalTierEngine::with_caps_arena(TierCaps::default(), arena);
        let mut p = TierPipeline::new(eng, BandwidthPool::new(1 << 20)).with_node_id("n0");
        p.engine.put_durable(b"h0", b"0").unwrap();
        p.engine.put_durable(b"h1", b"1").unwrap();
        let _ = p.engine.l2_arena.free(b"h0");
        let _ = p.engine.l2_arena.free(b"h1");
        p.engine.l2_arena.place_at(b"h0", 1, 0).unwrap();
        p.engine.l2_arena.place_at(b"h1", 2, 0).unwrap();
        p.enqueue(PipelineAction::CoLocateMove {
            hash: b"h1".to_vec(),
            dest_segment: 1,
            dest_offset: 64,
        });
        let (n, ev) = p.tick(1);
        assert_eq!(n, 1);
        assert_eq!(p.engine.l2_placement(b"h1").unwrap().segment_id, 1);
        assert_eq!(p.engine.l2_placement(b"h1").unwrap().offset, 64);
        assert!(ev.iter().any(|e| matches!(
            e,
            LocationEvent::Moved {
                hash,
                segment_id: 1,
                offset: 64,
                ..
            } if hash == b"h1"
        )));
    }
}
