//! 存储控制面:位置视图权威(进程内存)。
//!
//! P4.2:Dynamo `BlockRegistry` + `PositionalRadixTree` + `InactiveIndex` 薄驱动。
//! 参考:`registry/mod.rs::register_sequence_hash` / `match_sequence_hash`；
//! `InactiveIndex` + `LineageBackend::with_frequency`（叶子 + TinyLFU）。
//! 关键差异:不用 BlockManager/BlockStore；EventsManager 不接线；
//! presence 与 Authority 同锁 → 进程内线性一致。

mod authority;
mod hash_chain;
mod tier;

use std::pin::Pin;
use std::sync::{Arc, Mutex};

use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status, Streaming};

pub use authority::Authority;
pub use lake_proto::lake::*;

use control_plane_service_server::ControlPlaneService;

#[derive(Clone, Default)]
pub struct ControlPlane {
    inner: Arc<Mutex<Authority>>,
}

#[tonic::async_trait]
impl ControlPlaneService for ControlPlane {
    type SubscribeViewStream =
        Pin<Box<dyn tokio_stream::Stream<Item = Result<ViewUpdate, Status>> + Send + 'static>>;

    async fn subscribe_view(
        &self,
        _request: Request<SubscribeRequest>,
    ) -> Result<Response<Self::SubscribeViewStream>, Status> {
        Err(Status::unimplemented(
            "SubscribeView 未实现;Router 冷路径用 LookupPrefix",
        ))
    }

    async fn lookup_prefix(
        &self,
        request: Request<LookupPrefixRequest>,
    ) -> Result<Response<LookupPrefixResponse>, Status> {
        let req = request.into_inner();
        let mut auth = self.inner.lock().unwrap();
        let (blocks, hit_length, all_local_hit) =
            auth.lookup_prefix(&req.model_id, &req.prefix_hashes, &req.requester_node_id);
        Ok(Response::new(LookupPrefixResponse {
            blocks,
            hit_length,
            all_local_hit,
        }))
    }

    async fn locate(
        &self,
        request: Request<LocateRequest>,
    ) -> Result<Response<LocateResponse>, Status> {
        let req = request.into_inner();
        let auth = self.inner.lock().unwrap();
        let blocks = auth.locate(&req.ids);
        Ok(Response::new(LocateResponse { blocks }))
    }

    async fn register_blocks(
        &self,
        request: Request<RegisterBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self.inner.lock().unwrap();
        match auth.register(&req.node_id, &req.prefix_hashes, req.blocks) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
            })),
            Err(e) => Ok(Response::new(Ack { ok: false, err: e })),
        }
    }

    async fn report_ref(
        &self,
        request: Request<Streaming<RefDelta>>,
    ) -> Result<Response<Ack>, Status> {
        let mut stream = request.into_inner();
        let mut deltas = Vec::new();
        while let Some(delta) = stream.message().await? {
            deltas.push(delta);
        }
        let mut auth = self.inner.lock().unwrap();
        match auth.report_refs(&deltas) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
            })),
            Err(e) => Ok(Response::new(Ack { ok: false, err: e })),
        }
    }

    async fn request_barrier(
        &self,
        _request: Request<RequestBarrierRequest>,
    ) -> Result<Response<Ack>, Status> {
        Ok(Response::new(Ack {
            ok: true,
            err: String::new(),
        }))
    }

    type LeaseStream =
        Pin<Box<dyn tokio_stream::Stream<Item = Result<LeaseAck, Status>> + Send + 'static>>;

    async fn lease(
        &self,
        _request: Request<Streaming<LeaseHeartbeat>>,
    ) -> Result<Response<Self::LeaseStream>, Status> {
        let (tx, rx) = mpsc::channel(1);
        drop(tx);
        let stream: Self::LeaseStream = Box::pin(ReceiverStream::new(rx));
        Ok(Response::new(stream))
    }
}

#[allow(dead_code)]
type _CpServer = lake_proto::lake::control_plane_service_server::ControlPlaneServiceServer<()>;
#[allow(dead_code)]
const _ANCHOR: fn() = || {
    let _ = RegisterBlocksRequest::default();
    let _ = LookupPrefixRequest::default();
    let _ = RefDelta::default();
};

#[cfg(test)]
mod tests {
    use super::*;

    fn meta(model: &str, hash: &[u8]) -> BlockMeta {
        BlockMeta {
            id: Some(KvBlockId {
                model_id: model.into(),
                block_hash: hash.to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
            }),
            block_kind: BlockKind::TType as i32,
            locations: vec![],
            l3_present: false,
            ref_count: 1,
        }
    }

    fn prefix(hashes: &[&[u8]]) -> Vec<Vec<u8>> {
        hashes.iter().map(|h| h.to_vec()).collect()
    }

    #[test]
    fn lookup_prefix_contiguous_then_gap() {
        let mut auth = Authority::default();
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        auth.register(
            "n0",
            &full,
            vec![meta("m", b"h0"), meta("m", b"h1"), meta("m", b"h2")],
        )
        .unwrap();
        let (blocks, hit, local) = auth.lookup_prefix("m", &prefix(&[b"h0", b"gap", b"h2"]), "n0");
        assert_eq!(hit, 1);
        assert_eq!(blocks.len(), 1);
        assert!(!local);
    }

    #[test]
    fn lookup_prefix_full_hit_not_local_without_l0() {
        let mut auth = Authority::default();
        let full = prefix(&[b"a", b"b"]);
        auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"b")])
            .unwrap();
        let (_, hit, local) = auth.lookup_prefix("m", &full, "n0");
        assert_eq!(hit, 2);
        assert!(!local);
    }

    #[test]
    fn cross_model_isolation() {
        let mut auth = Authority::default();
        let full = prefix(&[b"shared"]);
        auth.register("n0", &full, vec![meta("m1", b"shared")])
            .unwrap();
        auth.register("n0", &full, vec![meta("m2", b"shared")])
            .unwrap();
        let (_, hit1, _) = auth.lookup_prefix("m1", &full, "n0");
        let (_, hit2, _) = auth.lookup_prefix("m2", &full, "n0");
        assert_eq!(hit1, 1);
        assert_eq!(hit2, 1);
        // miss suffix only for m1 should not affect m2
        let (_, miss, _) = auth.lookup_prefix("m1", &prefix(&[b"other"]), "n0");
        assert_eq!(miss, 0);
    }

    #[test]
    fn register_requires_prefix_hashes() {
        let mut auth = Authority::default();
        let err = auth.register("n0", &[], vec![meta("m", b"a")]).unwrap_err();
        assert!(err.contains("prefix_hashes"));
    }

    #[test]
    fn register_miss_suffix_with_full_chain() {
        let mut auth = Authority::default();
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
            .unwrap();
        // miss suffix only
        auth.register("n0", &full, vec![meta("m", b"h2")]).unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", &full, "n0");
        assert_eq!(hit, 3);
    }

    #[test]
    fn register_rejects_non_contiguous_subset() {
        let mut auth = Authority::default();
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        let err = auth
            .register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h2")])
            .unwrap_err();
        assert!(err.contains("contiguous"));
    }

    #[test]
    fn lookup_stops_on_lineage_mismatch() {
        let mut auth = Authority::default();
        let chain = prefix(&[b"A", b"B"]);
        auth.register("n0", &chain, vec![meta("m", b"A"), meta("m", b"B")])
            .unwrap();
        // Same flat "B" but as a root → different PositionalLineageHash; must not hit.
        let (blocks, hit, _) = auth.lookup_prefix("m", &prefix(&[b"B"]), "n0");
        assert_eq!(hit, 0);
        assert!(blocks.is_empty());
    }

    #[test]
    fn ref_freeze_and_evict() {
        let mut auth = Authority::default();
        let full = prefix(&[b"x"]);
        auth.register("n0", &full, vec![meta("m", b"x")]).unwrap();

        let d = RefDelta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"x".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
            }),
            kind: RefKind::Request as i32,
            delta: 1,
            node_id: "n0".into(),
        };
        auth.report_ref(&d).unwrap();
        assert_eq!(auth.global_ref("m", b"x"), 1);
        assert_eq!(auth.inactive_len("m"), 0);

        // still held → evict should remove 0 from view if we force-insert...
        // first drop ref
        let mut d0 = d.clone();
        d0.delta = -1;
        auth.report_ref(&d0).unwrap();
        assert_eq!(auth.global_ref("m", b"x"), 0);
        assert_eq!(auth.inactive_len("m"), 1);

        // ref>0 again removes from inactive
        auth.report_ref(&d).unwrap();
        assert_eq!(auth.inactive_len("m"), 0);
        auth.report_ref(&d0).unwrap();
        assert_eq!(auth.inactive_len("m"), 1);

        let n = auth.evict_n("m", 1);
        assert_eq!(n, 1);
        let (_, hit, _) = auth.lookup_prefix("m", &full, "n0");
        assert_eq!(hit, 0);
    }

    #[test]
    fn report_ref_batch_all_or_nothing() {
        let mut auth = Authority::default();
        let full = prefix(&[b"p"]);
        auth.register("n0", &full, vec![meta("m", b"p")]).unwrap();
        let plus = RefDelta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"p".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
            }),
            kind: RefKind::Request as i32,
            delta: 1,
            node_id: "n0".into(),
        };
        auth.report_ref(&plus).unwrap();
        assert_eq!(auth.global_ref("m", b"p"), 1);

        let mut minus = plus.clone();
        minus.delta = -1;
        let unknown = RefDelta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"missing".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
            }),
            kind: RefKind::Request as i32,
            delta: -1,
            node_id: "n0".into(),
        };
        let err = auth.report_refs(&[minus, unknown]).unwrap_err();
        assert!(err.contains("unknown block_hash") || err.contains("batch"));
        assert_eq!(
            auth.global_ref("m", b"p"),
            1,
            "failed batch must not apply prefix deltas"
        );
    }

    #[test]
    fn ref_gt_zero_not_evicted() {
        let mut auth = Authority::default();
        let full = prefix(&[b"y"]);
        auth.register("n0", &full, vec![meta("m", b"y")]).unwrap();
        let d = RefDelta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"y".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
            }),
            kind: RefKind::Request as i32,
            delta: 1,
            node_id: "n0".into(),
        };
        auth.report_ref(&d).unwrap();
        // not in inactive while held
        assert_eq!(auth.evict_n("m", 10), 0);
        let (_, hit, _) = auth.lookup_prefix("m", &full, "n0");
        assert_eq!(hit, 1);
    }

    #[test]
    fn frequency_evicts_colder_leaf_first() {
        let mut auth = Authority::default();
        let cold = prefix(&[b"cold"]);
        let hot = prefix(&[b"hot"]);
        auth.register("n0", &cold, vec![meta("m", b"cold")])
            .unwrap();
        auth.register("n0", &hot, vec![meta("m", b"hot")]).unwrap();
        // bump TinyLFU for hot via LookupPrefix → match_sequence_hash(touch)
        for _ in 0..64 {
            let _ = auth.lookup_prefix("m", &hot, "n0");
        }
        for flat in [b"cold".as_slice(), b"hot".as_slice()] {
            let d = RefDelta {
                id: Some(KvBlockId {
                    model_id: "m".into(),
                    block_hash: flat.to_vec(),
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
        }
        assert_eq!(auth.inactive_len("m"), 2);
        assert_eq!(auth.evict_n("m", 1), 1);
        let (_, cold_hit, _) = auth.lookup_prefix("m", &cold, "n0");
        let (_, hot_hit, _) = auth.lookup_prefix("m", &hot, "n0");
        assert_eq!(cold_hit, 0, "colder leaf should be Frequency victim");
        assert_eq!(hot_hit, 1, "touched hot leaf should survive first allocate");
    }

    #[test]
    fn authority_evicts_leaf_before_prefix_parent() {
        let mut auth = Authority::default();
        let chain = prefix(&[b"parent", b"child"]);
        auth.register(
            "n0",
            &chain,
            vec![meta("m", b"parent"), meta("m", b"child")],
        )
        .unwrap();
        for flat in [b"parent".as_slice(), b"child".as_slice()] {
            let d = RefDelta {
                id: Some(KvBlockId {
                    model_id: "m".into(),
                    block_hash: flat.to_vec(),
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
        }
        assert_eq!(auth.evict_n("m", 1), 1);
        let (_, parent_only, _) = auth.lookup_prefix("m", &prefix(&[b"parent"]), "n0");
        assert_eq!(parent_only, 1, "prefix parent must survive first evict");
        let (_, chain_hit, _) = auth.lookup_prefix("m", &chain, "n0");
        assert_eq!(chain_hit, 1, "leaf gone → gap after parent");
    }

    /// Past inactive_cap, `report_ref` skips insert (Dynamo: insert ≠ allocate).
    /// Cap-resident leaves stay allocatable; over-cap blocks stay in the view
    /// at ref=0 but out of inactive (no silent Frequency drop / view zombie).
    #[test]
    fn inactive_cap_skip_insert_no_zombie() {
        let cap = 4;
        let mut auth = Authority::with_inactive_cap(cap);
        let n = cap * 2;
        let mut flats: Vec<Vec<u8>> = Vec::with_capacity(n);
        for i in 0..n {
            let flat = format!("leaf{i:02}").into_bytes();
            let full = prefix(&[flat.as_slice()]);
            auth.register("n0", &full, vec![meta("m", &flat)]).unwrap();
            let d = RefDelta {
                id: Some(KvBlockId {
                    model_id: "m".into(),
                    block_hash: flat.clone(),
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
            flats.push(flat);
            assert!(
                auth.inactive_len("m") <= cap,
                "inactive must stay ≤ cap after insert #{i}"
            );
        }
        assert_eq!(auth.inactive_len("m"), cap);
        // Early leaves still in the view (report_ref must not pressure-evict).
        let (_, early_hit, _) = auth.lookup_prefix("m", &prefix(&[flats[0].as_slice()]), "n0");
        assert_eq!(early_hit, 1, "skip-insert must not drop view entries");
        // Cap-resident inactive must all be allocatable (no zombie leaves).
        let removed = auth.evict_n("m", cap);
        assert_eq!(removed, cap, "allocate must clear all inactive at cap");
        assert_eq!(auth.inactive_len("m"), 0);
    }

    /// Mid-batch must not panic: previous ensure_inactive_room could delete a
    /// later delta's block and trip `expect` after the pre-check passed.
    #[test]
    fn report_refs_mid_batch_must_not_panic_or_drop_peer() {
        let mut auth = Authority::with_inactive_cap(1);
        let held = prefix(&[b"held"]);
        let cand = prefix(&[b"cand"]);
        auth.register("n0", &held, vec![meta("m", b"held")])
            .unwrap();
        auth.register("n0", &cand, vec![meta("m", b"cand")])
            .unwrap();

        let held_id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"held".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
        };
        let cand_id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"cand".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
        };
        // Fill inactive with cand; hold held at ref=1.
        for (id, plus_then_minus) in [(&cand_id, true), (&held_id, false)] {
            let plus = RefDelta {
                id: Some(id.clone()),
                kind: RefKind::Request as i32,
                delta: 1,
                node_id: "n0".into(),
            };
            auth.report_ref(&plus).unwrap();
            if plus_then_minus {
                let mut minus = plus.clone();
                minus.delta = -1;
                auth.report_ref(&minus).unwrap();
            }
        }
        assert_eq!(auth.inactive_len("m"), 1);
        assert_eq!(auth.global_ref("m", b"held"), 1);
        assert_eq!(auth.global_ref("m", b"cand"), 0);

        let held_minus = RefDelta {
            id: Some(held_id.clone()),
            kind: RefKind::Request as i32,
            delta: -1,
            node_id: "n0".into(),
        };
        let cand_plus = RefDelta {
            id: Some(cand_id.clone()),
            kind: RefKind::Request as i32,
            delta: 1,
            node_id: "n0".into(),
        };
        // Must not panic; both blocks remain addressable.
        auth.report_refs(&[held_minus, cand_plus]).unwrap();
        assert_eq!(auth.global_ref("m", b"held"), 0);
        assert_eq!(auth.global_ref("m", b"cand"), 1);
        // held:-1 skipped insert (cap full); cand:+1 took itself out → empty.
        assert_eq!(auth.inactive_len("m"), 0);
        let (_, cand_hit, _) = auth.lookup_prefix("m", &cand, "n0");
        assert_eq!(cand_hit, 1, "peer must not be pressure-evicted mid-batch");
    }
}
