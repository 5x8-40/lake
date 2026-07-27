//! PutEnd 两阶段（P4.3）。
//!
//! **COMPLETE 语义**（Mooncake `PutEnd` → `mark_complete`；#20；`storage-layer.md`）：
//! 1. PutStart 本地记账
//! 2. `put_durable` **只落 L2**（F4；不写 L1、不写 L3；**flush 在 pin 之前**以便 L2 cap XOR）
//! 3. 本地 `pin`（≈ SGLang `lock_ref`）
//! 4. `RegisterBlocks`（按真实 settle：L2 或 L3-only）= COMPLETE
//! 5. CP `ReportRef(WRITEBACK,+1)` → `RequestBarrier` → `WRITEBACK,-1`（解冻晚于 barrier）
//!
//! L3：仅 L2→L3 demote / L2 cap 压力（稳态 XOR），不在 PutEnd 双写。

use lake_proto::lake::*;
use lake_tiered_store::LocalTierEngine;

use crate::cp_port::ControlPlanePort;

#[derive(Clone, Debug)]
pub struct PendingBlock {
    pub id: KvBlockId,
    pub bytes: Vec<u8>,
    pub durable: bool,
}

#[derive(Debug)]
pub struct PutEndSession {
    pub request_id: String,
    pub node_id: String,
    pub model_id: String,
    pub prefix_hashes: Vec<Vec<u8>>,
    blocks: Vec<PendingBlock>,
    /// Observability / future assert: true while WRITEBACK+1 is held
    /// (between `report_refs(+1)` and successful `-1`). Not read by logic yet.
    writeback_open: bool,
}

impl PutEndSession {
    pub fn new(
        request_id: impl Into<String>,
        node_id: impl Into<String>,
        model_id: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            node_id: node_id.into(),
            model_id: model_id.into(),
            prefix_hashes: Vec::new(),
            blocks: Vec::new(),
            writeback_open: false,
        }
    }

    pub fn put_start(&mut self, block_hash: Vec<u8>, bytes: Vec<u8>) {
        self.prefix_hashes.push(block_hash.clone());
        self.blocks.push(PendingBlock {
            id: KvBlockId {
                model_id: self.model_id.clone(),
                block_hash,
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
            },
            bytes,
            durable: false,
        });
    }

    pub fn register_request(&self, store: &LocalTierEngine) -> RegisterBlocksRequest {
        let blocks = self
            .blocks
            .iter()
            .map(|b| {
                let h = b.id.block_hash.as_slice();
                let mut locations = Vec::new();
                if store.is_l2_durable(h) {
                    locations.push(Location {
                        tier: Tier::L2 as i32,
                        node_id: self.node_id.clone(),
                        // TODO(P5): real segment_id/offset from NVMe placement
                        // (#20 P4.3 review §4.3 / P4.4·P5；内存站位无 segment map).
                        segment_id: 1,
                        offset: 0,
                    });
                }
                BlockMeta {
                    id: Some(b.id.clone()),
                    block_kind: BlockKind::TType as i32,
                    locations,
                    l3_present: store.l3_present(h),
                    ref_count: 0,
                }
            })
            .collect();
        RegisterBlocksRequest {
            node_id: self.node_id.clone(),
            blocks,
            prefix_hashes: self.prefix_hashes.clone(),
        }
    }

    fn writeback_deltas(&self, delta: i32) -> Vec<RefDelta> {
        self.blocks
            .iter()
            .map(|b| RefDelta {
                id: Some(b.id.clone()),
                kind: RefKind::Writeback as i32,
                delta,
                node_id: self.node_id.clone(),
            })
            .collect()
    }

    /// Flush to L2 only. Returns hashes demoted L2→L3 under L2 cap (collateral).
    pub fn flush_durable(&mut self, store: &mut LocalTierEngine) -> Result<Vec<Vec<u8>>, String> {
        let mut demoted_all = Vec::new();
        for b in &mut self.blocks {
            let (_tier, fx) = store.put_durable(&b.id.block_hash, &b.bytes)?;
            demoted_all.extend(fx.l2_demoted_to_l3);
            b.durable = store.is_settled(&b.id.block_hash);
            if !b.durable {
                return Err(format!(
                    "flush: block {:?} not settled on L2|L3",
                    b.id.block_hash
                ));
            }
        }
        Ok(demoted_all)
    }

    pub fn all_durable(&self) -> bool {
        self.blocks.iter().all(|b| b.durable)
    }

    pub fn commit_through<P: ControlPlanePort>(
        &mut self,
        store: &mut LocalTierEngine,
        cp: &mut P,
    ) -> Result<(), String> {
        // Flush **before** pin: pinning blocks first would freeze them against
        // L2→L3 cap demote (`ensure_l2_cap` skips pinned), leaving L2 over cap.
        let demoted = self.flush_durable(store)?;
        if !self.all_durable() {
            return Err("commit: not settled on L2|L3".into());
        }

        // Pin after durable (≈ lock_ref for register→barrier window).
        for b in &self.blocks {
            store.pin(&b.id.block_hash);
        }
        let unpin_all = |store: &mut LocalTierEngine, blocks: &[PendingBlock]| {
            for b in blocks {
                let _ = store.unpin(&b.id.block_hash);
            }
        };

        // Cap demotions of prior blocks: L2→L3 XOR sync on CP.
        for h in &demoted {
            if !self.blocks.iter().any(|b| b.id.block_hash == *h) {
                if let Err(e) =
                    cp.publish_location(&self.model_id, h, Tier::L2, &self.node_id, false)
                {
                    unpin_all(store, &self.blocks);
                    return Err(e);
                }
                if let Err(e) = cp.set_l3_present(&self.model_id, h, true) {
                    unpin_all(store, &self.blocks);
                    return Err(e);
                }
            }
        }

        let reg = self.register_request(store);
        if let Err(e) = cp.register_blocks(reg) {
            unpin_all(store, &self.blocks);
            return Err(e);
        }
        for b in &self.blocks {
            let h = b.id.block_hash.as_slice();
            // Self may have been cap-demoted to L3-only during flush.
            if store.l3_present(h) {
                if let Err(e) = cp.set_l3_present(&self.model_id, h, true) {
                    unpin_all(store, &self.blocks);
                    return Err(e);
                }
            }
            if !store.is_l2_durable(h) {
                if let Err(e) =
                    cp.publish_location(&self.model_id, h, Tier::L2, &self.node_id, false)
                {
                    unpin_all(store, &self.blocks);
                    return Err(e);
                }
            }
        }

        // WRITEBACK +1 冻 radix 驱逐：register 后、barrier 前期间 block 不可被驱逐。
        // -1 延迟到 barrier 之后——barrier 完成才解冻，语义对齐 SGLang `lock_ref`
        // （flush→ack 整段持有）。骨架单进程无并发，此窗口为异步场景占位。
        if let Err(e) = cp.report_refs(&self.writeback_deltas(1)) {
            unpin_all(store, &self.blocks);
            return Err(e);
        }
        self.writeback_open = true;
        if let Err(e) = cp.request_barrier(RequestBarrierRequest {
            request_id: self.request_id.clone(),
            node_id: self.node_id.clone(),
        }) {
            // Roll back WRITEBACK+1. Side effect: global_refs may hit 0 → CP
            // `inactive.insert` (blocks become eviction candidates) even though
            // Register already succeeded. Correct for "no holder", but leaves a
            // register→retry window under real tonic + concurrent eviction.
            // Deferred: #20 P4.7（PR #31 review §4.1 / writeback 泄漏兜底）.
            let _ = cp.report_refs(&self.writeback_deltas(-1));
            self.writeback_open = false;
            unpin_all(store, &self.blocks);
            return Err(e);
        }
        if let Err(e) = cp.report_refs(&self.writeback_deltas(-1)) {
            self.writeback_open = false;
            unpin_all(store, &self.blocks);
            return Err(e);
        }
        self.writeback_open = false;

        for b in &self.blocks {
            store.unpin(&b.id.block_hash)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cp_port::{apply_location_events, AuthorityPort};
    use lake_controlplane::Authority;
    use lake_tiered_store::{
        BandwidthPool, LocalTier, LocalTierEngine, PipelineAction, TierCaps, TierPipeline,
    };

    #[test]
    fn commit_through_l2_only_no_l3() {
        let mut store = LocalTierEngine::with_caps(TierCaps::default());
        let mut auth = Authority::default();
        let mut sess = PutEndSession::new("r1", "n0", "m");
        sess.put_start(b"h0".to_vec(), b"KV:h0".to_vec());

        {
            let mut port = AuthorityPort { auth: &mut auth };
            sess.commit_through(&mut store, &mut port).unwrap();
        }
        assert!(store.is_l2_durable(b"h0"));
        assert!(!store.l3_present(b"h0"), "PutEnd must not write L3");
        assert!(auth.barrier_completed("r1"));
        let d = RefDelta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"h0".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
            }),
            kind: RefKind::Request as i32,
            delta: 1,
            node_id: "n0".into(),
        };
        auth.report_ref(&d).unwrap();
        let mut d0 = d;
        d0.delta = -1;
        auth.report_ref(&d0).unwrap();
        assert_eq!(auth.evict_n("m", 1), 1);
    }

    #[test]
    fn promote_publishes_l0() {
        let mut store = LocalTierEngine::new();
        let mut auth = Authority::default();
        let mut sess = PutEndSession::new("r2", "n0", "m");
        sess.put_start(b"p".to_vec(), b"P".to_vec());
        {
            let mut port = AuthorityPort { auth: &mut auth };
            sess.commit_through(&mut store, &mut port).unwrap();
            store.promote_to_l0(b"p").unwrap();
            port.publish_location("m", b"p", Tier::L0, "n0", true)
                .unwrap();
        }
        assert_eq!(store.local_tier(b"p"), Some(LocalTier::L0));
        assert!(auth.has_l0_on("m", b"p", "n0"));
    }

    #[test]
    fn l2_cap_demote_syncs_l3_xor_on_cp() {
        let mut store = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        let mut auth = Authority::default();
        let mut s0 = PutEndSession::new("r-a", "n0", "m");
        s0.put_start(b"x".to_vec(), b"X".to_vec());
        {
            let mut port = AuthorityPort { auth: &mut auth };
            s0.commit_through(&mut store, &mut port).unwrap();
        }
        assert!(store.is_l2_durable(b"x"));
        assert!(!store.l3_present(b"x"));

        let mut s1 = PutEndSession::new("r-b", "n0", "m");
        s1.put_start(b"y".to_vec(), b"Y".to_vec());
        {
            let mut port = AuthorityPort { auth: &mut auth };
            s1.commit_through(&mut store, &mut port).unwrap();
        }
        // Cap pressure: x moved L2→L3 (XOR), y on L2.
        assert!(!store.is_l2_durable(b"x"));
        assert!(store.l3_present(b"x"));
        assert!(store.is_l2_durable(b"y"));
        assert!(!store.l3_present(b"y"));

        let meta_x = auth
            .lookup_prefix("m", &[b"x".to_vec()], "n0")
            .0
            .into_iter()
            .next()
            .and_then(|r| r.meta)
            .expect("x");
        assert!(meta_x.l3_present);
        assert!(!meta_x
            .locations
            .iter()
            .any(|l| l.tier == Tier::L2 as i32 && l.node_id == "n0"));
    }

    /// durable-first 不变量：commit 成功 ⇒ radix 已发布 block 已 settle（L2|L3），无悬空。
    /// (Ref: consistency.md §2/§8.3, Mooncake PROCESSING→COMPLETE；cap 下可为 L3-only)
    #[test]
    fn commit_published_block_always_settled() {
        let mut store = LocalTierEngine::new();
        let mut auth = Authority::default();
        let mut sess = PutEndSession::new("r-df", "n0", "m");
        sess.put_start(b"h0".to_vec(), b"KV:h0".to_vec());
        {
            let mut port = AuthorityPort { auth: &mut auth };
            sess.commit_through(&mut store, &mut port).unwrap();
        }
        let (metas, _hit, _all) = auth.lookup_prefix("m", &[b"h0".to_vec()], "n0");
        let meta = metas
            .into_iter()
            .next()
            .and_then(|r| r.meta)
            .expect("block registered");
        let has_l2 = meta.locations.iter().any(|l| l.tier == Tier::L2 as i32);
        assert!(
            has_l2 || meta.l3_present,
            "durable-first: published block must be settled L2|L3, got {:?}",
            meta
        );
        assert!(store.is_settled(b"h0"));
    }

    /// Multi-block PutEnd under L2 cap: flush-before-pin so XOR demote can run.
    #[test]
    fn multi_block_putend_l2_cap_xor_without_pin_block() {
        let mut store = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        let mut auth = Authority::default();
        let mut sess = PutEndSession::new("r-mb", "n0", "m");
        sess.put_start(b"x".to_vec(), b"X".to_vec());
        sess.put_start(b"y".to_vec(), b"Y".to_vec());
        {
            let mut port = AuthorityPort { auth: &mut auth };
            sess.commit_through(&mut store, &mut port).unwrap();
        }
        assert!(!store.is_l2_durable(b"x") && store.l3_present(b"x"));
        assert!(store.is_l2_durable(b"y") && !store.l3_present(b"y"));
        assert_eq!(store.l2_len(), 1);
    }

    /// M1(B) 时序不变量：WRITEBACK −1 解冻必须发生在 RequestBarrier 之后。
    /// 单进程同步无并发窗口，故用 recording mock 锁调用序（防 −1 被改回 barrier 之前）。
    /// (Ref: consistency.md §3, SGLang `_evict_write_back` 持锁到 flush+ack)
    #[test]
    fn writeback_minus_one_after_barrier() {
        struct RecordingPort {
            auth: Authority,
            calls: Vec<&'static str>,
        }
        impl ControlPlanePort for RecordingPort {
            fn register_blocks(&mut self, req: RegisterBlocksRequest) -> Result<(), String> {
                self.calls.push("register");
                self.auth
                    .register(&req.node_id, &req.prefix_hashes, req.blocks)
            }
            fn report_refs(&mut self, deltas: &[RefDelta]) -> Result<(), String> {
                let tag = if deltas.first().map(|d| d.delta >= 0).unwrap_or(true) {
                    "wb+1"
                } else {
                    "wb-1"
                };
                self.calls.push(tag);
                self.auth.report_refs(deltas)
            }
            fn request_barrier(&mut self, req: RequestBarrierRequest) -> Result<(), String> {
                self.calls.push("barrier");
                self.auth.complete_barrier(&req.request_id, &req.node_id)
            }
            fn publish_location(
                &mut self,
                model_id: &str,
                flat: &[u8],
                tier: Tier,
                node_id: &str,
                present: bool,
            ) -> Result<(), String> {
                self.calls.push("publish");
                self.auth
                    .publish_location(model_id, flat, tier, node_id, present)
            }
            fn set_l3_present(
                &mut self,
                model_id: &str,
                flat: &[u8],
                present: bool,
            ) -> Result<(), String> {
                self.calls.push("l3");
                self.auth.set_l3_present(model_id, flat, present)
            }
        }

        let mut store = LocalTierEngine::new();
        let mut port = RecordingPort {
            auth: Authority::default(),
            calls: Vec::new(),
        };
        let mut sess = PutEndSession::new("r-m1b", "n0", "m");
        sess.put_start(b"h0".to_vec(), b"KV:h0".to_vec());
        sess.commit_through(&mut store, &mut port).unwrap();

        let barrier_idx = port
            .calls
            .iter()
            .rposition(|c| *c == "barrier")
            .expect("barrier called");
        let minus_idx = port
            .calls
            .iter()
            .rposition(|c| *c == "wb-1")
            .expect("WRITEBACK -1 called");
        assert!(
            barrier_idx < minus_idx,
            "WRITEBACK -1 必须在 barrier 之后 (M1(B))，got {:?}",
            port.calls
        );
    }

    #[test]
    fn pipeline_tick_apply_drops_victim_l0_on_cp() {
        let mut store = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        let mut auth = Authority::default();
        for (id, h, bytes) in [("r-a", b"a", b"A"), ("r-b", b"b", b"B")] {
            let mut s = PutEndSession::new(id, "n0", "m");
            s.put_start(h.to_vec(), bytes.to_vec());
            let mut port = AuthorityPort { auth: &mut auth };
            s.commit_through(&mut store, &mut port).unwrap();
        }
        let mut pipe = TierPipeline::new(store, BandwidthPool::new(1 << 20));
        pipe.enqueue(PipelineAction::Promote {
            hash: b"a".to_vec(),
        });
        let (n, ev) = pipe.tick(1);
        assert_eq!(n, 1);
        {
            let mut port = AuthorityPort { auth: &mut auth };
            apply_location_events(&mut port, "m", "n0", &ev).unwrap();
        }
        assert!(auth.has_l0_on("m", b"a", "n0"));

        pipe.enqueue(PipelineAction::Promote {
            hash: b"b".to_vec(),
        });
        let (n2, ev2) = pipe.tick(1);
        assert_eq!(n2, 1);
        {
            let mut port = AuthorityPort { auth: &mut auth };
            apply_location_events(&mut port, "m", "n0", &ev2).unwrap();
        }
        assert!(auth.has_l0_on("m", b"b", "n0"));
        assert!(!auth.has_l0_on("m", b"a", "n0"));
        let (_, _, all_local_a) = auth.lookup_prefix("m", &[b"a".to_vec()], "n0");
        assert!(!all_local_a);
    }
}
