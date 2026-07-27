//! Promote / demote 流水线（抄 Dynamo `Pipeline` 骨架，补 lake promote）。
//!
//! 参考：`kvbm-engine` `offload/pipeline.rs::Pipeline`（transfer 后 settlement /
//! presence）；lake 同步版：`tick` 返回完整 [`LocationEvent`] 列表（含 collateral）。
//! 关键差异：无 async PendingTracker；失败 requeue 一次窗口尾，不 silent-drop。

use crate::bandwidth::BandwidthPool;
use crate::engine::{LocalTier, LocalTierEngine, TierSideEffects};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PipelineAction {
    Promote { hash: Vec<u8> },
    DemoteL0 { hash: Vec<u8> },
    DemoteL2 { hash: Vec<u8> },
    FillMiss { hash: Vec<u8> },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LocationEvent {
    Present { hash: Vec<u8>, tier: LocalTier },
    Absent { hash: Vec<u8>, tier: LocalTier },
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

pub struct TierPipeline {
    pub engine: LocalTierEngine,
    pub bandwidth: BandwidthPool,
    queue: std::collections::VecDeque<PipelineAction>,
}

impl TierPipeline {
    pub fn new(engine: LocalTierEngine, bandwidth: BandwidthPool) -> Self {
        Self {
            engine,
            bandwidth,
            queue: std::collections::VecDeque::new(),
        }
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
            let result = match &action {
                PipelineAction::Promote { hash } => self.engine.promote_to_l0(hash),
                PipelineAction::FillMiss { hash } => self.engine.fill_read_miss(hash),
                PipelineAction::DemoteL0 { hash } => self
                    .engine
                    .demote_l0(hash)
                    .map(|n| (n, TierSideEffects::default())),
                PipelineAction::DemoteL2 { hash } => self
                    .engine
                    .demote_l2_to_l3(hash)
                    .map(|n| (n, TierSideEffects::default())),
            };
            match (&action, result) {
                (PipelineAction::Promote { hash }, Ok((_n, fx)))
                | (PipelineAction::FillMiss { hash }, Ok((_n, fx))) => {
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
                (PipelineAction::DemoteL0 { hash }, Ok(_)) => {
                    events.push(LocationEvent::Absent {
                        hash: hash.clone(),
                        tier: LocalTier::L0,
                    });
                    done += 1;
                    consecutive_fail = 0;
                }
                (PipelineAction::DemoteL2 { hash }, Ok(_)) => {
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
                (_, Err(_)) => {
                    if cost > 0 {
                        self.bandwidth.refund(cost);
                    }
                    // Requeue at back; stop if we rotate the whole queue without progress.
                    self.queue.push_back(action);
                    consecutive_fail += 1;
                    if consecutive_fail >= q0 {
                        break;
                    }
                }
            }
        }
        (done, events)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::TierCaps;

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
}
