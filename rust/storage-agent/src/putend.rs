//! PutEnd 两阶段（P4.3）。
//!
//! **COMPLETE 语义**（Mooncake `PutStart`/`PutEnd` → `mark_complete`；#20；`storage-layer.md`）：
//! 1. PutStart 本地记账
//! 2. CP `admit_register_blocks`（配额 preflight；触硬则**不**写 durable）
//! 3. `put_durable` **只落 L2**（F4；不写 L1、不写 L3；**flush 在 pin 之前**以便 L2 cap XOR）
//! 4. 本地 `pin`（≈ SGLang `lock_ref`）
//! 5. `RegisterBlocks`（按真实 settle：L2 或 L3-only）= COMPLETE；失败则 `discard_settled` 回滚本会话 bytes
//! 6. CP `ReportRef(WRITEBACK,+1)` → `RequestBarrier` → `WRITEBACK,-1`（解冻晚于 barrier）
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
    /// P4.5: `(model_id, revision)` namespace; empty = default.
    pub revision: String,
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
        Self::with_revision(request_id, node_id, model_id, "")
    }

    pub fn with_revision(
        request_id: impl Into<String>,
        node_id: impl Into<String>,
        model_id: impl Into<String>,
        revision: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            node_id: node_id.into(),
            model_id: model_id.into(),
            revision: revision.into(),
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
                revision: self.revision.clone(),
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
                    // Prefer SegmentArena coords so CP view matches local layout
                    // (P4.8); fall back only if arena missed a settled L2.
                    let (segment_id, offset) = store
                        .l2_placement(h)
                        .map(|p| (p.segment_id, p.offset))
                        .unwrap_or((1, 0));
                    locations.push(Location {
                        tier: Tier::L2 as i32,
                        node_id: self.node_id.clone(),
                        segment_id,
                        offset,
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

    fn pool_kind(&self) -> i32 {
        self.blocks
            .first()
            .map(|b| b.id.pool_kind)
            .unwrap_or(PoolKind::Target as i32)
    }

    pub fn commit_through<P: ControlPlanePort>(
        &mut self,
        store: &mut LocalTierEngine,
        cp: &mut P,
    ) -> Result<(), String> {
        // Quota preflight **before** durable write (Mooncake PutStart / review #2).
        // Hard reject must not leave orphan L2/L3 bytes or trigger demotion.
        let admit_req = self.register_request(store);
        cp.admit_register_blocks(&admit_req)?;

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
        let discard_session = |store: &mut LocalTierEngine, blocks: &[PendingBlock]| {
            for b in blocks {
                let _ = store.unpin(&b.id.block_hash);
                store.discard_settled(&b.id.block_hash);
            }
        };

        // Cap demotions of prior blocks: L2→L3 XOR sync on CP.
        for h in &demoted {
            if !self.blocks.iter().any(|b| b.id.block_hash == *h) {
                let pk = self.pool_kind();
                if let Err(e) = cp.publish_location(
                    &self.model_id,
                    &self.revision,
                    pk,
                    h,
                    Tier::L2,
                    &self.node_id,
                    false,
                ) {
                    unpin_all(store, &self.blocks);
                    return Err(e);
                }
                if let Err(e) = cp.set_l3_present(&self.model_id, &self.revision, pk, h, true) {
                    unpin_all(store, &self.blocks);
                    return Err(e);
                }
            }
        }

        let reg = self.register_request(store);
        if let Err(e) = cp.register_blocks(reg) {
            // Belt-and-suspenders: register can still fail after preflight (race /
            // validation). Drop this session's durable bytes so tier is not polluted.
            discard_session(store, &self.blocks);
            for b in &mut self.blocks {
                b.durable = false;
            }
            return Err(e);
        }
        let pk = self.pool_kind();
        for b in &self.blocks {
            let h = b.id.block_hash.as_slice();
            // Self may have been cap-demoted to L3-only during flush.
            if store.l3_present(h) {
                if let Err(e) = cp.set_l3_present(&self.model_id, &self.revision, pk, h, true) {
                    unpin_all(store, &self.blocks);
                    return Err(e);
                }
            }
            if !store.is_l2_durable(h) {
                if let Err(e) = cp.publish_location(
                    &self.model_id,
                    &self.revision,
                    pk,
                    h,
                    Tier::L2,
                    &self.node_id,
                    false,
                ) {
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
            let rollback = cp.report_refs(&self.writeback_deltas(-1));
            if rollback.is_ok() {
                self.writeback_open = false;
            }
            unpin_all(store, &self.blocks);
            if let Err(rollback_err) = rollback {
                return Err(format!(
                    "{e}; writeback rollback failed after barrier error: {rollback_err}"
                ));
            }
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
    use crate::cp_port::{
        apply_location_events, enqueue_defrag_moves, sync_background_pause, AuthorityPort,
    };
    use lake_controlplane::Authority;
    use lake_tiered_store::{
        BandwidthPool, LocalTier, LocalTierEngine, PipelineAction, SegmentArena, TierCaps,
        TierPipeline,
    };

    fn ensure_model(auth: &mut Authority, model: &str) {
        auth.register_model(ModelDescriptor {
            model_id: model.into(),
            revision: String::new(),
            num_layers: 1,
            block_spec: Some(BlockSpec {
                block_tokens: 128,
                bytes_per_block: 0,
            }),
            hash_algo: HashAlgo::HashSha256256 as i32,
            quota: None,
        })
        .unwrap();
    }

    fn ensure_model_quota(auth: &mut Authority, model: &str, soft: i64, hard: i64, bpb: u64) {
        auth.register_model(ModelDescriptor {
            model_id: model.into(),
            revision: String::new(),
            num_layers: 1,
            block_spec: Some(BlockSpec {
                block_tokens: 128,
                bytes_per_block: bpb,
            }),
            hash_algo: HashAlgo::HashSha256256 as i32,
            quota: Some(Quota {
                soft_bytes: soft,
                hard_bytes: hard,
                borrow_enabled: false,
            }),
        })
        .unwrap();
    }

    /// Hard quota preflight runs before flush — no orphan tier bytes (review #2).
    #[test]
    fn hard_quota_preflight_skips_durable_write() {
        let mut store = LocalTierEngine::with_caps(TierCaps::default());
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "m", 100, 100, 100);
        // Fill hard with one block via PutEnd.
        {
            let mut s = PutEndSession::new("r0", "n0", "m");
            s.put_start(b"a0".to_vec(), vec![1u8; 100]);
            let mut port = AuthorityPort { auth: &mut auth };
            s.commit_through(&mut store, &mut port).unwrap();
        }
        assert!(store.is_settled(b"a0"));
        // Hold a0 so it is not inactive — hard cannot free room by eviction.
        auth.report_ref(&RefDelta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"a0".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            }),
            kind: RefKind::Request as i32,
            delta: 1,
            node_id: "n0".into(),
        })
        .unwrap();

        let mut sess = PutEndSession::new("r1", "n0", "m");
        sess.put_start(b"a1".to_vec(), vec![2u8; 100]);
        let err = {
            let mut port = AuthorityPort { auth: &mut auth };
            sess.commit_through(&mut store, &mut port).unwrap_err()
        };
        assert!(
            err.contains("hard quota") || err.contains("AdmitRegister"),
            "got {err}"
        );
        assert!(
            !store.is_settled(b"a1"),
            "rejected write must not leave durable bytes"
        );
        assert!(!sess.all_durable());
    }

    /// Register failure after preflight discards session durable bytes (review #2).
    #[test]
    fn register_failure_discards_durable_bytes() {
        struct FailRegister {
            auth: Authority,
        }
        impl ControlPlanePort for FailRegister {
            fn admit_register_blocks(&mut self, req: &RegisterBlocksRequest) -> Result<(), String> {
                use lake_controlplane::RegisterStatus;
                let hashes: Vec<Vec<u8>> = req
                    .blocks
                    .iter()
                    .filter_map(|m| m.id.as_ref().map(|i| i.block_hash.clone()))
                    .collect();
                match self.auth.preflight_register("m", "", 0, &hashes)? {
                    RegisterStatus::Accepted => Ok(()),
                    RegisterStatus::RejectedHardQuota(bp) => {
                        Err(format!("admit hard {}", bp.deficit_bytes))
                    }
                }
            }
            fn register_blocks(&mut self, _req: RegisterBlocksRequest) -> Result<(), String> {
                Err("inject: register boom".into())
            }
            fn report_refs(&mut self, _: &[RefDelta]) -> Result<(), String> {
                Ok(())
            }
            fn request_barrier(&mut self, _: RequestBarrierRequest) -> Result<(), String> {
                Ok(())
            }
            fn publish_location(
                &mut self,
                _: &str,
                _: &str,
                _: i32,
                _: &[u8],
                _: Tier,
                _: &str,
                _: bool,
            ) -> Result<(), String> {
                Ok(())
            }
            fn set_l3_present(
                &mut self,
                _: &str,
                _: &str,
                _: i32,
                _: &[u8],
                _: bool,
            ) -> Result<(), String> {
                Ok(())
            }
            fn relocate_in_view(
                &mut self,
                _: &str,
                _: &str,
                _: i32,
                _: &[u8],
                _: Tier,
                _: &str,
                _: u64,
                _: u64,
            ) -> Result<(), String> {
                Ok(())
            }
        }

        let mut store = LocalTierEngine::with_caps(TierCaps::default());
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let mut port = FailRegister { auth };
        let mut sess = PutEndSession::new("r-fail", "n0", "m");
        sess.put_start(b"z".to_vec(), b"Z".to_vec());
        let err = sess.commit_through(&mut store, &mut port).unwrap_err();
        assert!(err.contains("register boom"));
        assert!(!store.is_settled(b"z"));
        assert!(!sess.all_durable());
    }

    #[test]
    fn commit_through_l2_only_no_l3() {
        let mut store = LocalTierEngine::with_caps(TierCaps::default());
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
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
                revision: String::new(),
            }),
            kind: RefKind::Request as i32,
            delta: 1,
            node_id: "n0".into(),
        };
        auth.report_ref(&d).unwrap();
        let mut d0 = d;
        d0.delta = -1;
        auth.report_ref(&d0).unwrap();
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
    }

    #[test]
    fn promote_publishes_l0() {
        let mut store = LocalTierEngine::new();
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let mut sess = PutEndSession::new("r2", "n0", "m");
        sess.put_start(b"p".to_vec(), b"P".to_vec());
        {
            let mut port = AuthorityPort { auth: &mut auth };
            sess.commit_through(&mut store, &mut port).unwrap();
            store.promote_to_l0(b"p").unwrap();
            port.publish_location("m", "", PoolKind::Target as i32, b"p", Tier::L0, "n0", true)
                .unwrap();
        }
        assert_eq!(store.local_tier(b"p"), Some(LocalTier::L0));
        assert!(auth.has_l0_on("m", "", PoolKind::Target as i32, b"p", "n0"));
    }

    #[test]
    fn l2_cap_demote_syncs_l3_xor_on_cp() {
        let mut store = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
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
        assert!(!store.is_l2_durable(b"x"));
        assert!(store.l3_present(b"x"));
        assert!(store.is_l2_durable(b"y"));
        assert!(!store.l3_present(b"y"));

        let meta_x = auth
            .lookup_prefix("m", "", PoolKind::Target as i32, &[b"x".to_vec()], "n0")
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

    #[test]
    fn commit_published_block_always_settled() {
        let mut store = LocalTierEngine::new();
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let mut sess = PutEndSession::new("r-df", "n0", "m");
        sess.put_start(b"h0".to_vec(), b"KV:h0".to_vec());
        {
            let mut port = AuthorityPort { auth: &mut auth };
            sess.commit_through(&mut store, &mut port).unwrap();
        }
        let (metas, _hit, _all) =
            auth.lookup_prefix("m", "", PoolKind::Target as i32, &[b"h0".to_vec()], "n0");
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

    #[test]
    fn multi_block_putend_l2_cap_xor_without_pin_block() {
        let mut store = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
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

    #[test]
    fn writeback_minus_one_after_barrier() {
        struct RecordingPort {
            auth: Authority,
            calls: Vec<&'static str>,
        }
        impl ControlPlanePort for RecordingPort {
            fn admit_register_blocks(&mut self, req: &RegisterBlocksRequest) -> Result<(), String> {
                use lake_controlplane::RegisterStatus;
                self.calls.push("admit");
                let hashes: Vec<Vec<u8>> = req
                    .blocks
                    .iter()
                    .filter_map(|m| m.id.as_ref().map(|i| i.block_hash.clone()))
                    .collect();
                let model = req
                    .blocks
                    .iter()
                    .find_map(|m| m.id.as_ref().map(|i| i.model_id.clone()))
                    .unwrap_or_default();
                let rev = req
                    .blocks
                    .iter()
                    .find_map(|m| m.id.as_ref().map(|i| i.revision.clone()))
                    .unwrap_or_default();
                let pk = req
                    .blocks
                    .iter()
                    .find_map(|m| m.id.as_ref().map(|i| i.pool_kind))
                    .unwrap_or(0);
                match self.auth.preflight_register(&model, &rev, pk, &hashes)? {
                    RegisterStatus::Accepted => Ok(()),
                    RegisterStatus::RejectedHardQuota(bp) => Err(format!(
                        "AdmitRegister: hard quota exceeded (deficit={})",
                        bp.deficit_bytes
                    )),
                }
            }
            fn register_blocks(&mut self, req: RegisterBlocksRequest) -> Result<(), String> {
                use lake_controlplane::RegisterStatus;
                self.calls.push("register");
                match self
                    .auth
                    .register(&req.node_id, &req.prefix_hashes, req.blocks)?
                {
                    RegisterStatus::Accepted => Ok(()),
                    RegisterStatus::RejectedHardQuota(bp) => Err(format!(
                        "RegisterBlocks: hard quota exceeded (deficit={})",
                        bp.deficit_bytes
                    )),
                }
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
                revision: &str,
                pool_kind: i32,
                flat: &[u8],
                tier: Tier,
                node_id: &str,
                present: bool,
            ) -> Result<(), String> {
                self.calls.push("publish");
                self.auth
                    .publish_location(model_id, revision, pool_kind, flat, tier, node_id, present)
            }
            fn set_l3_present(
                &mut self,
                model_id: &str,
                revision: &str,
                pool_kind: i32,
                flat: &[u8],
                present: bool,
            ) -> Result<(), String> {
                self.calls.push("l3");
                self.auth
                    .set_l3_present(model_id, revision, pool_kind, flat, present)
            }
            fn relocate_in_view(
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
                self.calls.push("relocate");
                self.auth.relocate_in_view(
                    model_id, revision, pool_kind, flat, tier, node_id, segment_id, offset,
                )
            }
        }

        let mut store = LocalTierEngine::new();
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let mut port = RecordingPort {
            auth,
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
    fn barrier_error_surfaces_writeback_rollback_failure() {
        struct FailingRollbackPort {
            auth: Authority,
        }
        impl ControlPlanePort for FailingRollbackPort {
            fn admit_register_blocks(
                &mut self,
                _req: &RegisterBlocksRequest,
            ) -> Result<(), String> {
                Ok(())
            }

            fn register_blocks(&mut self, req: RegisterBlocksRequest) -> Result<(), String> {
                self.auth
                    .register(&req.node_id, &req.prefix_hashes, req.blocks)
                    .map(|_| ())
            }
            fn report_refs(&mut self, deltas: &[RefDelta]) -> Result<(), String> {
                if deltas.iter().any(|d| d.delta < 0) {
                    return Err("rollback unavailable".into());
                }
                self.auth.report_refs(deltas)
            }
            fn request_barrier(&mut self, _req: RequestBarrierRequest) -> Result<(), String> {
                Err("barrier unavailable".into())
            }
            fn publish_location(
                &mut self,
                model_id: &str,
                revision: &str,
                pool_kind: i32,
                flat: &[u8],
                tier: Tier,
                node_id: &str,
                present: bool,
            ) -> Result<(), String> {
                self.auth
                    .publish_location(model_id, revision, pool_kind, flat, tier, node_id, present)
            }
            fn set_l3_present(
                &mut self,
                model_id: &str,
                revision: &str,
                pool_kind: i32,
                flat: &[u8],
                present: bool,
            ) -> Result<(), String> {
                self.auth
                    .set_l3_present(model_id, revision, pool_kind, flat, present)
            }
            fn relocate_in_view(
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
                self.auth.relocate_in_view(
                    model_id, revision, pool_kind, flat, tier, node_id, segment_id, offset,
                )
            }
        }

        let mut store = LocalTierEngine::new();
        let mut port = FailingRollbackPort {
            auth: Authority::default(),
        };
        ensure_model(&mut port.auth, "m");
        let mut sess = PutEndSession::new("r-rollback", "n0", "m");
        sess.put_start(b"h0".to_vec(), b"KV:h0".to_vec());
        let err = sess.commit_through(&mut store, &mut port).unwrap_err();
        assert!(err.contains("barrier unavailable"));
        assert!(err.contains("writeback rollback failed"));
        assert_eq!(
            port.auth
                .global_ref("m", "", PoolKind::Target as i32, b"h0"),
            1
        );
        assert!(sess.writeback_open);
    }

    #[test]
    fn pipeline_tick_apply_drops_victim_l0_on_cp() {
        let mut store = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
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
            apply_location_events(&mut port, "m", "", PoolKind::Target as i32, "n0", &ev).unwrap();
        }
        assert!(auth.has_l0_on("m", "", PoolKind::Target as i32, b"a", "n0"));

        pipe.enqueue(PipelineAction::Promote {
            hash: b"b".to_vec(),
        });
        let (n2, ev2) = pipe.tick(1);
        assert_eq!(n2, 1);
        {
            let mut port = AuthorityPort { auth: &mut auth };
            apply_location_events(&mut port, "m", "", PoolKind::Target as i32, "n0", &ev2).unwrap();
        }
        assert!(auth.has_l0_on("m", "", PoolKind::Target as i32, b"b", "n0"));
        assert!(!auth.has_l0_on("m", "", PoolKind::Target as i32, b"a", "n0"));
        let (_, _, all_local_a) =
            auth.lookup_prefix("m", "", PoolKind::Target as i32, &[b"a".to_vec()], "n0");
        assert!(!all_local_a);
    }

    /// PutEnd RegisterBlocks must carry SegmentArena coords (not fixed 1,0).
    #[test]
    fn p48_register_request_uses_arena_placement() {
        let mut store = LocalTierEngine::with_caps(TierCaps::default());
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let mut sess = PutEndSession::new("r-place", "n0", "m");
        sess.put_start(b"a".to_vec(), b"A".to_vec());
        sess.put_start(b"b".to_vec(), b"B".to_vec());
        // Flush like commit_through durable path.
        for b in &mut sess.blocks {
            store.put_durable(&b.id.block_hash, &b.bytes).unwrap();
            b.durable = true;
        }
        let p_a = store.l2_placement(b"a").expect("a placed");
        let p_b = store.l2_placement(b"b").expect("b placed");
        assert_ne!(
            (p_a.segment_id, p_a.offset),
            (p_b.segment_id, p_b.offset),
            "two blocks must not share the same arena slot"
        );

        let req = sess.register_request(&store);
        assert_eq!(req.blocks.len(), 2);
        let mut coords = Vec::new();
        for m in &req.blocks {
            let loc = m
                .locations
                .iter()
                .find(|l| l.tier == Tier::L2 as i32)
                .expect("L2 loc");
            coords.push((
                m.id.as_ref().unwrap().block_hash.clone(),
                loc.segment_id,
                loc.offset,
            ));
        }
        coords.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(coords[0].1, p_a.segment_id);
        assert_eq!(coords[0].2, p_a.offset);
        assert_eq!(coords[1].1, p_b.segment_id);
        assert_eq!(coords[1].2, p_b.offset);

        // Full PutEnd path: CP view must match arena after register.
        let mut sess2 = PutEndSession::new("r-place2", "n0", "m");
        sess2.put_start(b"c".to_vec(), vec![1, 2, 3]);
        sess2.put_start(b"d".to_vec(), vec![4, 5, 6]);
        {
            let mut port = AuthorityPort { auth: &mut auth };
            sess2.commit_through(&mut store, &mut port).unwrap();
        }
        let pc = store.l2_placement(b"c").unwrap();
        let pd = store.l2_placement(b"d").unwrap();
        let metas = auth.locate(&[
            KvBlockId {
                model_id: "m".into(),
                block_hash: b"c".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            },
            KvBlockId {
                model_id: "m".into(),
                block_hash: b"d".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            },
        ]);
        let loc_c = metas[0]
            .locations
            .iter()
            .find(|l| l.tier == Tier::L2 as i32)
            .unwrap();
        let loc_d = metas[1]
            .locations
            .iter()
            .find(|l| l.tier == Tier::L2 as i32)
            .unwrap();
        assert_eq!((loc_c.segment_id, loc_c.offset), (pc.segment_id, pc.offset));
        assert_eq!((loc_d.segment_id, loc_d.offset), (pd.segment_id, pd.offset));
        assert_ne!(
            (loc_c.segment_id, loc_c.offset),
            (loc_d.segment_id, loc_d.offset)
        );
    }

    /// P4.8: plan colocate → pipeline → Moved updates CP locations.
    #[test]
    fn p48_colocate_plan_tick_updates_cp() {
        let slot = 64u64;
        let arena = SegmentArena::new(slot, 8);
        let mut store = LocalTierEngine::with_caps_arena(TierCaps::default(), arena);
        store.put_durable(b"h0", b"0").unwrap();
        store.put_durable(b"h1", b"1").unwrap();
        let _ = store.l2_arena.free(b"h0");
        let _ = store.l2_arena.free(b"h1");
        store.l2_arena.place_at(b"h0", 1, 0).unwrap();
        store.l2_arena.place_at(b"h1", 2, 0).unwrap();

        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let m0 = BlockMeta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"h0".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            }),
            block_kind: BlockKind::TType as i32,
            locations: vec![Location {
                tier: Tier::L2 as i32,
                node_id: "n0".into(),
                segment_id: 1,
                offset: 0,
            }],
            l3_present: false,
            ref_count: 1,
        };
        let mut m1 = m0.clone();
        m1.id.as_mut().unwrap().block_hash = b"h1".to_vec();
        m1.locations[0].segment_id = 2;
        m1.locations[0].offset = 0;
        auth.register("n0", &[b"h0".to_vec(), b"h1".to_vec()], vec![m0, m1])
            .unwrap();

        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Colocate, slot)
            .unwrap();
        assert!(!moves.is_empty());

        let mut pipe = TierPipeline::new(store, BandwidthPool::new(1 << 20)).with_node_id("n0");
        sync_background_pause(&mut pipe, true);
        enqueue_defrag_moves(&mut pipe, &moves);
        let (n_paused, _) = pipe.tick(8);
        assert_eq!(n_paused, 0, "paused bandwidth must block defrag");
        sync_background_pause(&mut pipe, false);
        pipe.bandwidth.reset_window();
        let (n, ev) = pipe.tick(8);
        assert!(n >= 1);
        {
            let mut port = AuthorityPort { auth: &mut auth };
            apply_location_events(&mut port, "m", "", PoolKind::Target as i32, "n0", &ev).unwrap();
        }
        let metas = auth.locate(&[KvBlockId {
            model_id: "m".into(),
            block_hash: b"h1".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        }]);
        let loc = metas[0]
            .locations
            .iter()
            .find(|l| l.tier == Tier::L2 as i32)
            .unwrap();
        assert_eq!(loc.segment_id, 1);
        assert_eq!(loc.offset, slot);
    }
}
