//! 存储控制面:位置视图权威(进程内存)。
//!
//! P4.2:Dynamo `BlockRegistry` + `PositionalRadixTree` + `InactiveIndex` 薄驱动。
//! P4.5:`RegisterModel` / `DeregisterModel`——每 `(model_id, revision)` 一命名空间。
//! P4.6:按命名空间软/硬配额 + 借用 + `BackpressureSignal` + `AdmitRegisterBlocks`。
//! P4.7:冷块 GC / 孤儿 TTL / 节点 reconcile / `CheckpointStore` 内存 mock。
//! P4.8:碎片整理计划(`TriggerDefrag`/`PauseBackground`)+ Location segment/offset。
//! P4.9:一致性哈希分片(`GetShardMap`/`JoinShardNode`/`DrainShardNode`)。
//! 参考:`registry/mod.rs::register_sequence_hash`；Mooncake `ClearInvalidHandles` /
//! `put_start_discard_timeout` / `MetadataShard`；LMCache `QuotaManager`；
//! Mooncake allocator(无 compaction)。
//! 关键差异:节点级 reconcile 兜底 writeback 泄漏(非会话 TTL)；冷块留 L2/L3；
//! checkpoint 非 etcd(P6)；主动 defrag 经 BandwidthPool；分片只出迁移计划(字节 P5)。

mod authority;
mod checkpoint;
mod defrag;
mod hash_chain;
mod quota;
mod reconcile;
mod shard;
mod tier;

use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status, Streaming};

pub use authority::{Authority, NamespaceKey, RegisterStatus};
pub use checkpoint::{CheckpointStore, MemoryCheckpointStore};
pub use defrag::DEFAULT_DEFRAG_SLOT_BYTES;
pub use lake_proto::lake::*;
pub use reconcile::DEFAULT_ORPHAN_TTL_MS;
pub use shard::DEFAULT_VNODE_COUNT;

use control_plane_service_server::ControlPlaneService;

/// Keys needed for [`Authority::preflight_register`] / `AdmitRegisterBlocks` RPC.
struct AdmitKeys {
    model_id: String,
    revision: String,
    pool_kind: i32,
    hashes: Vec<Vec<u8>>,
}

fn admit_keys_from_request(req: &RegisterBlocksRequest) -> Result<AdmitKeys, String> {
    let model_id = req
        .blocks
        .iter()
        .find_map(|m| m.id.as_ref().map(|i| i.model_id.clone()))
        .ok_or_else(|| "AdmitRegisterBlocks: no KVBlockID".to_string())?;
    let revision = req
        .blocks
        .iter()
        .find_map(|m| m.id.as_ref().map(|i| i.revision.clone()))
        .unwrap_or_default();
    let pool_kind = req
        .blocks
        .iter()
        .find_map(|m| m.id.as_ref().map(|i| i.pool_kind))
        .unwrap_or(PoolKind::Target as i32);
    let hashes: Vec<Vec<u8>> = req
        .blocks
        .iter()
        .filter_map(|m| m.id.as_ref().map(|i| i.block_hash.clone()))
        .collect();
    if hashes.is_empty() {
        return Err("AdmitRegisterBlocks: no block hashes".into());
    }
    Ok(AdmitKeys {
        model_id,
        revision,
        pool_kind,
        hashes,
    })
}

#[derive(Clone, Default)]
pub struct ControlPlane {
    inner: Arc<Mutex<Authority>>,
    checkpoints: Arc<MemoryCheckpointStore>,
    /// Shared pause flag for promote/demote/GC/defrag (agent syncs BandwidthPool).
    background_paused: Arc<Mutex<bool>>,
    authority_poisoned_reported: Arc<AtomicBool>,
}

impl ControlPlane {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_checkpoint_store(store: MemoryCheckpointStore) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Authority::default())),
            checkpoints: Arc::new(store),
            background_paused: Arc::new(Mutex::new(false)),
            authority_poisoned_reported: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn background_paused(&self) -> bool {
        *self.background_paused.lock().unwrap()
    }

    pub fn set_background_paused(&self, paused: bool) {
        *self.background_paused.lock().unwrap() = paused;
    }

    pub fn authority_poisoned_reported(&self) -> bool {
        self.authority_poisoned_reported.load(Ordering::Relaxed)
    }
}

impl ControlPlane {
    fn lock_authority(&self) -> Result<MutexGuard<'_, Authority>, ()> {
        self.inner.lock().map_err(|_| {
            if !self
                .authority_poisoned_reported
                .swap(true, Ordering::Relaxed)
            {
                eprintln!("controlplane authority lock poisoned; returning INTERNAL until restart");
            }
        })
    }

    fn lock_authority_status(_: ()) -> Status {
        Status::internal("controlplane authority unavailable")
    }
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
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        let (blocks, hit_length, all_local_hit) = auth.lookup_prefix(
            &req.model_id,
            &req.revision,
            req.pool_kind,
            &req.prefix_hashes,
            &req.requester_node_id,
        );
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
        let auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        let blocks = auth.locate(&req.ids);
        Ok(Response::new(LocateResponse { blocks }))
    }

    async fn admit_register_blocks(
        &self,
        request: Request<RegisterBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let keys = match admit_keys_from_request(&req) {
            Ok(v) => v,
            Err(e) => {
                return Ok(Response::new(Ack {
                    ok: false,
                    err: e,
                    backpressure: None,
                }));
            }
        };
        let auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.preflight_register(&keys.model_id, &keys.revision, keys.pool_kind, &keys.hashes)
        {
            Ok(RegisterStatus::Accepted) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Ok(RegisterStatus::RejectedHardQuota(bp)) => Ok(Response::new(Ack {
                ok: false,
                err: format!(
                    "AdmitRegisterBlocks: admission rejected reason={} deficit={}",
                    bp.reason, bp.deficit_bytes
                ),
                backpressure: Some(bp),
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn register_blocks(
        &self,
        request: Request<RegisterBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.register(&req.node_id, &req.prefix_hashes, req.blocks) {
            Ok(RegisterStatus::Accepted) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Ok(RegisterStatus::RejectedHardQuota(bp)) => Ok(Response::new(Ack {
                ok: false,
                err: format!(
                    "RegisterBlocks: admission rejected reason={} deficit={}",
                    bp.reason, bp.deficit_bytes
                ),
                backpressure: Some(bp),
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
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
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.report_refs(&deltas) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn request_barrier(
        &self,
        request: Request<RequestBarrierRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        // P4.3: agent must flush L2 + ReportRef(WRITEBACK,-1) before this call.
        match auth.complete_barrier(&req.request_id, &req.node_id) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
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

    async fn register_model(
        &self,
        request: Request<RegisterModelRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let Some(model) = req.model else {
            return Ok(Response::new(Ack {
                ok: false,
                err: "RegisterModel: model required".into(),
                backpressure: None,
            }));
        };
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.register_model(model) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn deregister_model(
        &self,
        request: Request<DeregisterModelRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.deregister_model(&req.model_id, &req.revision) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn set_model_quota(
        &self,
        request: Request<SetModelQuotaRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let Some(quota) = req.quota else {
            return Ok(Response::new(Ack {
                ok: false,
                err: "SetModelQuota: quota required".into(),
                backpressure: None,
            }));
        };
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.set_model_quota(&req.model_id, &req.revision, quota) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn get_model_quota(
        &self,
        request: Request<GetModelQuotaRequest>,
    ) -> Result<Response<GetModelQuotaResponse>, Status> {
        let req = request.into_inner();
        let auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.get_model_quota(&req.model_id, &req.revision) {
            Ok(resp) => Ok(Response::new(resp)),
            Err(e) => Ok(Response::new(GetModelQuotaResponse {
                quota: None,
                used_bytes: 0,
                borrowed_bytes: 0,
                backpressure: None,
                ok: false,
                err: e,
            })),
        }
    }

    async fn reconcile_orphans(
        &self,
        request: Request<ReconcileOrphansRequest>,
    ) -> Result<Response<ReconcileOrphansResponse>, Status> {
        let req = request.into_inner();
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.reconcile_orphans(&req) {
            Ok(resp) => Ok(Response::new(resp)),
            Err(e) => Ok(Response::new(ReconcileOrphansResponse {
                discarded: vec![],
                cold_stripped: vec![],
                refs_cleared: 0,
                ok: false,
                err: e,
            })),
        }
    }

    async fn discard_blocks(
        &self,
        request: Request<DiscardBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.discard_blocks(&req.ids) {
            Ok(_) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn save_checkpoint(
        &self,
        _request: Request<SaveCheckpointRequest>,
    ) -> Result<Response<SaveCheckpointResponse>, Status> {
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        auth.checkpoint_seq = auth.checkpoint_seq.saturating_add(1);
        let snap = auth.export_snapshot(auth.checkpoint_seq);
        drop(auth);
        match self.checkpoints.save(snap.clone()) {
            Ok(()) => Ok(Response::new(SaveCheckpointResponse {
                snapshot: Some(snap),
                ok: true,
                err: String::new(),
            })),
            Err(e) => Ok(Response::new(SaveCheckpointResponse {
                snapshot: None,
                ok: false,
                err: e,
            })),
        }
    }

    async fn restore_checkpoint(
        &self,
        request: Request<RestoreCheckpointRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let snap = if let Some(s) = req.snapshot {
            s
        } else {
            match self.checkpoints.load() {
                Ok(Some(s)) => s,
                Ok(None) => {
                    return Ok(Response::new(Ack {
                        ok: false,
                        err: "RestoreCheckpoint: store empty".into(),
                        backpressure: None,
                    }));
                }
                Err(e) => {
                    return Ok(Response::new(Ack {
                        ok: false,
                        err: e,
                        backpressure: None,
                    }));
                }
            }
        };
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.import_snapshot(&snap) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn trigger_defrag(
        &self,
        request: Request<TriggerDefragRequest>,
    ) -> Result<Response<TriggerDefragResponse>, Status> {
        let req = request.into_inner();
        let mode = DefragMode::try_from(req.mode).unwrap_or(DefragMode::Both);
        let auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.plan_defrag(
            &req.model_id,
            &req.revision,
            req.pool_kind,
            mode,
            req.slot_bytes,
        ) {
            Ok(moves) => {
                let planned_moves = moves.len() as u32;
                Ok(Response::new(TriggerDefragResponse {
                    moves,
                    planned_moves,
                    ok: true,
                    err: String::new(),
                }))
            }
            Err(e) => Ok(Response::new(TriggerDefragResponse {
                moves: vec![],
                planned_moves: 0,
                ok: false,
                err: e,
            })),
        }
    }

    async fn pause_background(
        &self,
        request: Request<PauseBackgroundRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        self.set_background_paused(req.paused);
        Ok(Response::new(Ack {
            ok: true,
            err: String::new(),
            backpressure: None,
        }))
    }

    async fn get_shard_map(
        &self,
        _request: Request<GetShardMapRequest>,
    ) -> Result<Response<GetShardMapResponse>, Status> {
        let auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        Ok(Response::new(GetShardMapResponse {
            map: Some(auth.shard_map()),
            ok: true,
            err: String::new(),
        }))
    }

    async fn join_shard_node(
        &self,
        request: Request<JoinShardNodeRequest>,
    ) -> Result<Response<JoinShardNodeResponse>, Status> {
        let req = request.into_inner();
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.join_shard_node(&req.node_id, req.vnode_count) {
            Ok((map, migrations)) => {
                let migration_count = migrations.len() as u32;
                Ok(Response::new(JoinShardNodeResponse {
                    map: Some(map),
                    migrations,
                    migration_count,
                    ok: true,
                    err: String::new(),
                }))
            }
            Err(e) => Ok(Response::new(JoinShardNodeResponse {
                map: None,
                migrations: vec![],
                migration_count: 0,
                ok: false,
                err: e,
            })),
        }
    }

    async fn drain_shard_node(
        &self,
        request: Request<DrainShardNodeRequest>,
    ) -> Result<Response<DrainShardNodeResponse>, Status> {
        let req = request.into_inner();
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.drain_shard_node(&req.node_id) {
            Ok((map, migrations, push_l2)) => {
                let migration_count = migrations.len() as u32;
                Ok(Response::new(DrainShardNodeResponse {
                    map: Some(map),
                    migrations,
                    push_l2,
                    migration_count,
                    ok: true,
                    err: String::new(),
                }))
            }
            Err(e) => Ok(Response::new(DrainShardNodeResponse {
                map: None,
                migrations: vec![],
                push_l2: vec![],
                migration_count: 0,
                ok: false,
                err: e,
            })),
        }
    }

    async fn remove_shard_node(
        &self,
        request: Request<RemoveShardNodeRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self.lock_authority().map_err(Self::lock_authority_status)?;
        match auth.remove_shard_node(&req.node_id) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }
}

#[allow(dead_code)]
type _CpServer = lake_proto::lake::control_plane_service_server::ControlPlaneServiceServer<()>;
#[allow(dead_code)]
const _ANCHOR: fn() = || {
    let _ = RegisterBlocksRequest::default();
    let _ = LookupPrefixRequest::default();
    let _ = RegisterModelRequest::default();
    let _ = DeregisterModelRequest::default();
    let _ = SetModelQuotaRequest::default();
    let _ = GetModelQuotaRequest::default();
    let _ = BackpressureSignal::default();
    let _ = RefDelta::default();
    // AdmitRegisterBlocks reuses RegisterBlocksRequest on the wire.
    let _ = RegisterBlocksRequest::default();
};

#[cfg(test)]
mod tests {
    use super::*;

    fn ensure_model(auth: &mut Authority, model: &str) {
        ensure_model_rev(auth, model, "");
    }

    fn ensure_model_rev(auth: &mut Authority, model: &str, revision: &str) {
        auth.register_model(ModelDescriptor {
            model_id: model.into(),
            revision: revision.into(),
            num_layers: 32,
            block_spec: Some(BlockSpec {
                block_tokens: 128,
                bytes_per_block: 0,
            }),
            hash_algo: HashAlgo::HashSha256256 as i32,
            quota: Some(Quota {
                soft_bytes: 0,
                hard_bytes: 0,
                borrow_enabled: false,
            }),
        })
        .unwrap();
    }

    fn meta(model: &str, hash: &[u8]) -> BlockMeta {
        meta_rev(model, "", hash)
    }

    fn meta_rev(model: &str, revision: &str, hash: &[u8]) -> BlockMeta {
        BlockMeta {
            id: Some(KvBlockId {
                model_id: model.into(),
                block_hash: hash.to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: revision.into(),
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
        }
    }

    fn prefix(hashes: &[&[u8]]) -> Vec<Vec<u8>> {
        hashes.iter().map(|h| h.to_vec()).collect()
    }

    fn delta(model: &str, flat: &[u8], d: i32) -> RefDelta {
        delta_rev(model, "", flat, d)
    }

    fn delta_rev(model: &str, revision: &str, flat: &[u8], d: i32) -> RefDelta {
        RefDelta {
            id: Some(KvBlockId {
                model_id: model.into(),
                block_hash: flat.to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: revision.into(),
            }),
            kind: RefKind::Request as i32,
            delta: d,
            node_id: "n0".into(),
        }
    }

    #[test]
    fn lookup_prefix_contiguous_then_gap() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        auth.register(
            "n0",
            &full,
            vec![meta("m", b"h0"), meta("m", b"h1"), meta("m", b"h2")],
        )
        .unwrap();
        let (blocks, hit, local) = auth.lookup_prefix(
            "m",
            "",
            PoolKind::Target as i32,
            &prefix(&[b"h0", b"gap", b"h2"]),
            "n0",
        );
        assert_eq!(hit, 1);
        assert_eq!(blocks.len(), 1);
        assert!(!local);
    }

    #[test]
    fn lookup_prefix_full_hit_not_local_without_l0() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"a", b"b"]);
        auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"b")])
            .unwrap();
        let (_, hit, local) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 2);
        assert!(!local);
    }

    #[test]
    fn cross_model_isolation() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m1");
        ensure_model(&mut auth, "m2");
        let full = prefix(&[b"shared"]);
        auth.register("n0", &full, vec![meta("m1", b"shared")])
            .unwrap();
        auth.register("n0", &full, vec![meta("m2", b"shared")])
            .unwrap();
        let (_, hit1, _) = auth.lookup_prefix("m1", "", PoolKind::Target as i32, &full, "n0");
        let (_, hit2, _) = auth.lookup_prefix("m2", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit1, 1);
        assert_eq!(hit2, 1);
        let (_, miss, _) = auth.lookup_prefix(
            "m1",
            "",
            PoolKind::Target as i32,
            &prefix(&[b"other"]),
            "n0",
        );
        assert_eq!(miss, 0);
    }

    #[test]
    fn register_requires_prefix_hashes() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let err = auth.register("n0", &[], vec![meta("m", b"a")]).unwrap_err();
        assert!(err.contains("prefix_hashes"));
    }

    #[test]
    fn register_requires_registered_model() {
        let mut auth = Authority::default();
        let full = prefix(&[b"a"]);
        let err = auth
            .register("n0", &full, vec![meta("m", b"a")])
            .unwrap_err();
        assert!(err.contains("not registered"));
    }

    #[test]
    fn register_miss_suffix_with_full_chain() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
            .unwrap();
        auth.register("n0", &full, vec![meta("m", b"h2")]).unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 3);
    }

    #[test]
    fn register_rejects_non_contiguous_subset() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        let err = auth
            .register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h2")])
            .unwrap_err();
        assert!(err.contains("contiguous"));
    }

    #[test]
    fn lookup_stops_on_lineage_mismatch() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let chain = prefix(&[b"A", b"B"]);
        auth.register("n0", &chain, vec![meta("m", b"A"), meta("m", b"B")])
            .unwrap();
        let (blocks, hit, _) =
            auth.lookup_prefix("m", "", PoolKind::Target as i32, &prefix(&[b"B"]), "n0");
        assert_eq!(hit, 0);
        assert!(blocks.is_empty());
    }

    #[test]
    fn ref_freeze_and_evict() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"x"]);
        auth.register("n0", &full, vec![meta("m", b"x")]).unwrap();

        let d = delta("m", b"x", 1);
        auth.report_ref(&d).unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"x"), 1);
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);

        let mut d0 = d.clone();
        d0.delta = -1;
        auth.report_ref(&d0).unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"x"), 0);
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);

        auth.report_ref(&d).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);
        auth.report_ref(&d0).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);

        let n = auth.evict_n("m", "", PoolKind::Target as i32, 1);
        assert_eq!(n, 1);
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 0);
    }

    #[test]
    fn report_ref_batch_all_or_nothing() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"p"]);
        auth.register("n0", &full, vec![meta("m", b"p")]).unwrap();
        let plus = delta("m", b"p", 1);
        auth.report_ref(&plus).unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"p"), 1);

        let mut minus = plus.clone();
        minus.delta = -1;
        let unknown = delta("m", b"missing", -1);
        let err = auth.report_refs(&[minus, unknown]).unwrap_err();
        assert!(err.contains("unknown block_hash") || err.contains("batch"));
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"p"),
            1,
            "failed batch must not apply prefix deltas"
        );
    }

    #[test]
    fn ref_gt_zero_not_evicted() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"y"]);
        auth.register("n0", &full, vec![meta("m", b"y")]).unwrap();
        auth.report_ref(&delta("m", b"y", 1)).unwrap();
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 10), 0);
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
    }

    #[test]
    fn frequency_evicts_colder_leaf_first() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let cold = prefix(&[b"cold"]);
        let hot = prefix(&[b"hot"]);
        auth.register("n0", &cold, vec![meta("m", b"cold")])
            .unwrap();
        auth.register("n0", &hot, vec![meta("m", b"hot")]).unwrap();
        for _ in 0..64 {
            let _ = auth.lookup_prefix("m", "", PoolKind::Target as i32, &hot, "n0");
        }
        for flat in [b"cold".as_slice(), b"hot".as_slice()] {
            auth.report_ref(&delta("m", flat, 1)).unwrap();
            auth.report_ref(&delta("m", flat, -1)).unwrap();
        }
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 2);
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let (_, cold_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &cold, "n0");
        let (_, hot_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &hot, "n0");
        assert_eq!(cold_hit, 0, "colder leaf should be Frequency victim");
        assert_eq!(hot_hit, 1, "touched hot leaf should survive first allocate");
    }

    #[test]
    fn authority_evicts_leaf_before_prefix_parent() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let chain = prefix(&[b"parent", b"child"]);
        auth.register(
            "n0",
            &chain,
            vec![meta("m", b"parent"), meta("m", b"child")],
        )
        .unwrap();
        for flat in [b"parent".as_slice(), b"child".as_slice()] {
            auth.report_ref(&delta("m", flat, 1)).unwrap();
            auth.report_ref(&delta("m", flat, -1)).unwrap();
        }
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let (_, parent_only, _) = auth.lookup_prefix(
            "m",
            "",
            PoolKind::Target as i32,
            &prefix(&[b"parent"]),
            "n0",
        );
        assert_eq!(parent_only, 1, "prefix parent must survive first evict");
        let (_, chain_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &chain, "n0");
        assert_eq!(chain_hit, 1, "leaf gone → gap after parent");
    }

    #[test]
    fn inactive_cap_skip_insert_no_zombie() {
        let cap = 4;
        let mut auth = Authority::with_inactive_cap(cap);
        ensure_model(&mut auth, "m");
        let n = cap * 2;
        let mut flats: Vec<Vec<u8>> = Vec::with_capacity(n);
        for i in 0..n {
            let flat = format!("leaf{i:02}").into_bytes();
            let full = prefix(&[flat.as_slice()]);
            auth.register("n0", &full, vec![meta("m", &flat)]).unwrap();
            auth.report_ref(&delta("m", &flat, 1)).unwrap();
            auth.report_ref(&delta("m", &flat, -1)).unwrap();
            flats.push(flat);
            assert!(
                auth.inactive_len("m", "", PoolKind::Target as i32) <= cap,
                "inactive must stay ≤ cap after insert #{i}"
            );
        }
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), cap);
        let (_, early_hit, _) = auth.lookup_prefix(
            "m",
            "",
            PoolKind::Target as i32,
            &prefix(&[flats[0].as_slice()]),
            "n0",
        );
        assert_eq!(early_hit, 1, "skip-insert must not drop view entries");
        let removed = auth.evict_n("m", "", PoolKind::Target as i32, cap);
        assert_eq!(removed, cap, "allocate must clear all inactive at cap");
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);
    }

    #[test]
    fn report_refs_mid_batch_must_not_panic_or_drop_peer() {
        let mut auth = Authority::with_inactive_cap(1);
        ensure_model(&mut auth, "m");
        let held = prefix(&[b"held"]);
        let cand = prefix(&[b"cand"]);
        auth.register("n0", &held, vec![meta("m", b"held")])
            .unwrap();
        auth.register("n0", &cand, vec![meta("m", b"cand")])
            .unwrap();

        auth.report_ref(&delta("m", b"cand", 1)).unwrap();
        auth.report_ref(&delta("m", b"cand", -1)).unwrap();
        auth.report_ref(&delta("m", b"held", 1)).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"held"),
            1
        );
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"cand"),
            0
        );

        auth.report_refs(&[delta("m", b"held", -1), delta("m", b"cand", 1)])
            .unwrap();
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"held"),
            0
        );
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"cand"),
            1
        );
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);
        let (_, cand_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &cand, "n0");
        assert_eq!(cand_hit, 1, "peer must not be pressure-evicted mid-batch");
    }

    #[test]
    fn writeback_ref_blocks_evict_until_cleared() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"wb0"]);
        auth.register("n0", &full, vec![meta("m", b"wb0")]).unwrap();
        let mut plus = delta("m", b"wb0", 1);
        plus.kind = RefKind::Writeback as i32;
        auth.report_ref(&plus).unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"wb0"), 1);
        assert_eq!(
            auth.evict_n("m", "", PoolKind::Target as i32, 10),
            0,
            "writeback must freeze"
        );

        let mut minus = plus.clone();
        minus.delta = -1;
        auth.report_ref(&minus).unwrap();
        auth.complete_barrier("req-wb", "n0").unwrap();
        assert!(auth.barrier_completed("req-wb"));
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 0);
    }

    #[test]
    fn request_barrier_requires_ids() {
        let mut auth = Authority::default();
        assert!(auth.complete_barrier("", "n0").is_err());
        assert!(auth.complete_barrier("r", "").is_err());
    }

    #[test]
    fn report_ref_underflow_is_rejected() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"under"]);
        auth.register("n0", &full, vec![meta("m", b"under")])
            .unwrap();
        let minus = delta("m", b"under", -1);
        let err = auth.report_ref(&minus).unwrap_err();
        assert!(err.contains("underflow"));
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"under"),
            0
        );
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);
    }

    #[test]
    fn report_refs_underflow_is_all_or_nothing() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"ok", b"bad"]);
        auth.register("n0", &full, vec![meta("m", b"ok"), meta("m", b"bad")])
            .unwrap();
        let err = auth
            .report_refs(&[delta("m", b"ok", 1), delta("m", b"bad", -1)])
            .unwrap_err();
        assert!(err.contains("underflow"));
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"ok"), 0);
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"bad"), 0);
    }

    #[test]
    fn report_refs_node_underflow_is_all_or_nothing() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"a", b"b"]);
        auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"b")])
            .unwrap();
        auth.report_ref(&delta("m", b"a", 1)).unwrap();

        let mut wrong_node_release = delta("m", b"a", -1);
        wrong_node_release.node_id = "n1".into();
        let err = auth
            .report_refs(&[delta("m", b"b", 1), wrong_node_release])
            .unwrap_err();
        assert!(err.contains("node_ref underflow"));
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"a"), 1);
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"b"), 0);
    }

    #[test]
    fn inactive_duplicate_insert_does_not_panic() {
        use kvbm_logical::{FrequencyTrackingCapacity, InactiveIndex, LineageBackend};
        let tracker = FrequencyTrackingCapacity::Small.create_tracker();
        let mut inactive =
            LineageBackend::with_frequency(4, [3, 8, 15], std::sync::Arc::clone(&tracker) as _)
                .unwrap();
        let seq = super::hash_chain::lineage_from_prefix(&prefix(&[b"dup"]))[0];
        inactive.insert(seq, 1);
        inactive.insert(seq, 1);
        assert!(inactive.has(seq));
    }

    #[tokio::test]
    async fn poisoned_authority_lock_returns_status() {
        let cp = ControlPlane::default();
        let inner = cp.inner.clone();
        let _ = std::panic::catch_unwind(move || {
            let _guard = inner.lock().unwrap();
            panic!("poison authority");
        });
        let err = cp
            .locate(Request::new(LocateRequest { ids: Vec::new() }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::Internal);
        assert!(cp.authority_poisoned_reported());
    }

    #[test]
    fn register_rejects_invented_l2() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"bare"]);
        let mut m = meta("m", b"bare");
        m.locations.clear();
        assert!(auth.register("n0", &full, vec![m]).is_err());
    }

    #[test]
    fn publish_l0_enables_local_hit() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"loc"]);
        auth.register("n0", &full, vec![meta("m", b"loc")]).unwrap();
        auth.publish_location(
            "m",
            "",
            PoolKind::Target as i32,
            b"loc",
            Tier::L0,
            "n0",
            true,
        )
        .unwrap();
        assert!(auth.has_l0_on("m", "", PoolKind::Target as i32, b"loc", "n0"));
        let (_, _, all_local) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert!(all_local);
        auth.publish_location(
            "m",
            "",
            PoolKind::Target as i32,
            b"loc",
            Tier::L0,
            "n0",
            false,
        )
        .unwrap();
        let (_, _, all_local) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert!(!all_local);
    }

    /// P4.5: two models, same prefix hash → no cross-namespace hit.
    #[test]
    fn p45_two_models_same_hash_no_crosstalk() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "llama");
        ensure_model(&mut auth, "qwen");
        let full = prefix(&[b"samehash"]);
        auth.register("n0", &full, vec![meta("llama", b"samehash")])
            .unwrap();
        // qwen has no blocks yet
        let (_, hit_q, _) = auth.lookup_prefix("qwen", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit_q, 0);
        auth.register("n0", &full, vec![meta("qwen", b"samehash")])
            .unwrap();
        let (_, hit_l, _) = auth.lookup_prefix("llama", "", PoolKind::Target as i32, &full, "n0");
        let (_, hit_q, _) = auth.lookup_prefix("qwen", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit_l, 1);
        assert_eq!(hit_q, 1);
        auth.deregister_model("llama", "").unwrap();
        let (_, hit_l, _) = auth.lookup_prefix("llama", "", PoolKind::Target as i32, &full, "n0");
        let (_, hit_q, _) = auth.lookup_prefix("qwen", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit_l, 0, "llama cascaded away");
        assert_eq!(hit_q, 1, "qwen untouched");
    }

    /// P4.5: DeregisterModel clears radix/locations for that namespace.
    #[test]
    fn p45_deregister_cascades_view() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"a", b"b"]);
        auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"b")])
            .unwrap();
        assert_eq!(auth.block_count("m", ""), 2);
        auth.deregister_model("m", "").unwrap();
        assert!(!auth.has_namespace("m", ""));
        assert_eq!(auth.block_count("m", ""), 0);
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 0);
        // re-register model + blocks works on a fresh namespace
        ensure_model(&mut auth, "m");
        auth.register("n0", &full, vec![meta("m", b"a")]).unwrap();
        let (_, hit, _) =
            auth.lookup_prefix("m", "", PoolKind::Target as i32, &prefix(&[b"a"]), "n0");
        assert_eq!(hit, 1);
    }

    /// P4.5: new revision = new namespace; old revision remains until deregistered.
    #[test]
    fn p45_revision_isolation_and_invalidate() {
        let mut auth = Authority::default();
        ensure_model_rev(&mut auth, "m", "r1");
        ensure_model_rev(&mut auth, "m", "r2");
        let full = prefix(&[b"pfx"]);
        auth.register("n0", &full, vec![meta_rev("m", "r1", b"pfx")])
            .unwrap();
        auth.register("n0", &full, vec![meta_rev("m", "r2", b"pfx")])
            .unwrap();
        let (_, h1, _) = auth.lookup_prefix("m", "r1", PoolKind::Target as i32, &full, "n0");
        let (_, h2, _) = auth.lookup_prefix("m", "r2", PoolKind::Target as i32, &full, "n0");
        assert_eq!(h1, 1);
        assert_eq!(h2, 1);
        // miss across revision
        let (_, h_cross, _) = auth.lookup_prefix("m", "r1", PoolKind::Target as i32, &full, "n0");
        assert_eq!(h_cross, 1);
        auth.deregister_model("m", "r1").unwrap();
        let (_, h1, _) = auth.lookup_prefix("m", "r1", PoolKind::Target as i32, &full, "n0");
        let (_, h2, _) = auth.lookup_prefix("m", "r2", PoolKind::Target as i32, &full, "n0");
        assert_eq!(h1, 0, "r1 invalidated");
        assert_eq!(h2, 1, "r2 still live");
        assert!(auth.has_namespace("m", "r2"));
        assert!(!auth.has_namespace("m", "r1"));
    }

    #[test]
    fn register_model_idempotent_keeps_blocks() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"keep"]);
        auth.register("n0", &full, vec![meta("m", b"keep")])
            .unwrap();
        // Same identity + quota bump is ok.
        auth.register_model(ModelDescriptor {
            model_id: "m".into(),
            revision: String::new(),
            num_layers: 32,
            block_spec: Some(BlockSpec {
                block_tokens: 128,
                bytes_per_block: 0,
            }),
            hash_algo: HashAlgo::HashSha256256 as i32,
            quota: Some(Quota {
                soft_bytes: 1,
                hard_bytes: 2,
                borrow_enabled: true,
            }),
        })
        .unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
        let q = auth
            .model_descriptor("m", "")
            .and_then(|d| d.quota)
            .unwrap();
        assert_eq!(q.soft_bytes, 1);
        assert_eq!(q.hard_bytes, 2);
        assert!(q.borrow_enabled);
    }

    #[test]
    fn register_model_rejects_immutable_change() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let err = auth
            .register_model(ModelDescriptor {
                model_id: "m".into(),
                revision: String::new(),
                num_layers: 64, // changed
                block_spec: Some(BlockSpec {
                    block_tokens: 128,
                    bytes_per_block: 0,
                }),
                hash_algo: HashAlgo::HashSha256256 as i32,
                quota: None,
            })
            .unwrap_err();
        assert!(err.contains("immutable") || err.contains("new revision"));
        // blocks still addressable under original descriptor
        let full = prefix(&[b"x"]);
        auth.register("n0", &full, vec![meta("m", b"x")]).unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
    }

    /// Same (model, revision, hash) under TARGET vs DRAFT must not crosstalk.
    #[test]
    fn p45_target_draft_same_hash_no_crosstalk() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"shared"]);
        auth.register("n0", &full, vec![meta("m", b"shared")])
            .unwrap();
        let mut draft = meta("m", b"shared");
        draft.id.as_mut().unwrap().pool_kind = PoolKind::Draft as i32;
        auth.register("n0", &full, vec![draft]).unwrap();

        let (_, t_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        let (_, d_hit, _) = auth.lookup_prefix("m", "", PoolKind::Draft as i32, &full, "n0");
        assert_eq!(t_hit, 1);
        assert_eq!(d_hit, 1);

        // Locate by pool_kind
        let t_id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"shared".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        let mut d_id = t_id.clone();
        d_id.pool_kind = PoolKind::Draft as i32;
        assert_eq!(auth.locate(std::slice::from_ref(&t_id)).len(), 1);
        assert_eq!(auth.locate(std::slice::from_ref(&d_id)).len(), 1);

        // Evict TARGET only
        auth.report_ref(&delta("m", b"shared", 1)).unwrap();
        auth.report_ref(&delta("m", b"shared", -1)).unwrap();
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let (_, t_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        let (_, d_hit, _) = auth.lookup_prefix("m", "", PoolKind::Draft as i32, &full, "n0");
        assert_eq!(t_hit, 0, "target evicted");
        assert_eq!(d_hit, 1, "draft untouched");
    }

    // --- P4.6: soft/hard quota + borrow + backpressure ---

    fn ensure_model_quota(
        auth: &mut Authority,
        model: &str,
        soft: i64,
        hard: i64,
        borrow: bool,
        bpb: u64,
    ) {
        auth.register_model(ModelDescriptor {
            model_id: model.into(),
            revision: String::new(),
            num_layers: 32,
            block_spec: Some(BlockSpec {
                block_tokens: 128,
                bytes_per_block: bpb,
            }),
            hash_algo: HashAlgo::HashSha256256 as i32,
            quota: Some(Quota {
                soft_bytes: soft,
                hard_bytes: hard,
                borrow_enabled: borrow,
            }),
        })
        .unwrap();
    }

    /// A hits hard quota → RejectedHardQuota; B unaffected.
    #[test]
    fn p46_hard_quota_a_does_not_affect_b() {
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "A", 100, 200, false, 100);
        ensure_model_quota(&mut auth, "B", 100, 200, false, 100);

        // A: 2 blocks = 200 = hard; third rejected
        assert_eq!(
            auth.register("n0", &prefix(&[b"a0"]), vec![meta("A", b"a0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(
            auth.register("n0", &prefix(&[b"a1"]), vec![meta("A", b"a1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        match auth
            .register("n0", &prefix(&[b"a2"]), vec![meta("A", b"a2")])
            .unwrap()
        {
            RegisterStatus::RejectedHardQuota(bp) => {
                assert_eq!(bp.model_id, "A");
                assert_eq!(bp.reason, "HARD_QUOTA");
                assert!(bp.deficit_bytes > 0);
            }
            other => panic!("expected RejectedHardQuota, got {other:?}"),
        }
        assert_eq!(auth.used_bytes("A", ""), 200);
        assert_eq!(auth.block_count("A", ""), 2);

        // B still writes
        assert_eq!(
            auth.register("n0", &prefix(&[b"b0"]), vec![meta("B", b"b0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(auth.used_bytes("B", ""), 100);
        let (_, hit, _) =
            auth.lookup_prefix("B", "", PoolKind::Target as i32, &prefix(&[b"b0"]), "n0");
        assert_eq!(hit, 1);
    }

    /// Borrow over soft from pool free; reclaim when another borrower needs space.
    #[test]
    fn p46_borrow_and_reclaim() {
        let mut auth = Authority::default();
        auth.set_pool_capacity_bytes(300);
        // Both may borrow; capacity forces reclaim of A's over-soft bytes for B.
        ensure_model_quota(&mut auth, "A", 100, 300, true, 100);
        ensure_model_quota(&mut auth, "B", 100, 300, true, 100);

        // A: soft + one borrowed block (used=200)
        assert_eq!(
            auth.register("n0", &prefix(&[b"a0"]), vec![meta("A", b"a0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(
            auth.register("n0", &prefix(&[b"a1"]), vec![meta("A", b"a1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        let snap = auth.get_model_quota("A", "").unwrap();
        assert_eq!(snap.used_bytes, 200);
        assert_eq!(snap.borrowed_bytes, 100);

        // Make A's borrowed block inactive (reclaimable)
        auth.report_ref(&delta("A", b"a1", 1)).unwrap();
        auth.report_ref(&delta("A", b"a1", -1)).unwrap();

        // B takes soft (pool free → 0)
        assert_eq!(
            auth.register("n0", &prefix(&[b"b0"]), vec![meta("B", b"b0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        // B second block needs borrow but free=0 → reclaim A's over-soft
        assert_eq!(
            auth.register("n0", &prefix(&[b"b1"]), vec![meta("B", b"b1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert!(auth.used_bytes("A", "") <= 100, "A borrow reclaimed");
        assert_eq!(auth.used_bytes("B", ""), 200);
        let (_, a_hit, _) =
            auth.lookup_prefix("A", "", PoolKind::Target as i32, &prefix(&[b"a1"]), "n0");
        assert_eq!(a_hit, 0, "reclaimed a1");
    }

    /// Under hard quota but out of shared pool capacity must not masquerade as HARD_QUOTA.
    #[test]
    fn p46_pool_capacity_reject_is_not_hard_quota() {
        let mut auth = Authority::default();
        auth.set_pool_capacity_bytes(200);
        ensure_model_quota(&mut auth, "m", 100, 300, true, 100);

        assert_eq!(
            auth.register("n0", &prefix(&[b"a0"]), vec![meta("m", b"a0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(
            auth.register("n0", &prefix(&[b"a1"]), vec![meta("m", b"a1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        match auth
            .register("n0", &prefix(&[b"a2"]), vec![meta("m", b"a2")])
            .unwrap()
        {
            RegisterStatus::RejectedHardQuota(bp) => {
                assert_eq!(bp.reason, "POOL_CAPACITY");
                assert_eq!(bp.hard_bytes, 300);
                assert!(bp.deficit_bytes > 0);
            }
            other => panic!("expected capacity reject, got {other:?}"),
        }
        assert_eq!(auth.block_count("m", ""), 2);
    }

    /// SetModelQuota / GetModelQuota + hard backpressure on RegisterBlocks.
    #[test]
    fn p46_set_get_quota_and_backpressure_signal() {
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "m", 0, 0, false, 50); // unlimited initially
        assert_eq!(
            auth.register("n0", &prefix(&[b"x"]), vec![meta("m", b"x")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        auth.set_model_quota(
            "m",
            "",
            Quota {
                soft_bytes: 50,
                hard_bytes: 50,
                borrow_enabled: false,
            },
        )
        .unwrap();
        let g = auth.get_model_quota("m", "").unwrap();
        assert_eq!(g.used_bytes, 50);
        assert_eq!(g.quota.as_ref().unwrap().hard_bytes, 50);

        match auth
            .register("n0", &prefix(&[b"y"]), vec![meta("m", b"y")])
            .unwrap()
        {
            RegisterStatus::RejectedHardQuota(bp) => {
                assert_eq!(bp.reason, "HARD_QUOTA");
                assert_eq!(bp.hard_bytes, 50);
            }
            other => panic!("expected backpressure reject, got {other:?}"),
        }
    }

    /// Hard reject must not evict existing reusable inactive blocks (review #1).
    #[test]
    fn p46_hard_reject_is_side_effect_free() {
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "m", 200, 200, false, 100);
        assert_eq!(
            auth.register("n0", &prefix(&[b"a0"]), vec![meta("m", b"a0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(
            auth.register("n0", &prefix(&[b"a1"]), vec![meta("m", b"a1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        // a1 inactive (reusable); lower hard so even full inactive eviction cannot fit.
        auth.set_model_quota(
            "m",
            "",
            Quota {
                soft_bytes: 100,
                hard_bytes: 100,
                borrow_enabled: false,
            },
        )
        .unwrap();
        auth.report_ref(&delta("m", b"a1", 1)).unwrap();
        auth.report_ref(&delta("m", b"a1", -1)).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);

        match auth
            .register("n0", &prefix(&[b"a2"]), vec![meta("m", b"a2")])
            .unwrap()
        {
            RegisterStatus::RejectedHardQuota(_) => {}
            other => panic!("expected RejectedHardQuota, got {other:?}"),
        }
        // a1 must still be lookup-able (not evicted on the reject path).
        let (_, hit, _) =
            auth.lookup_prefix("m", "", PoolKind::Target as i32, &prefix(&[b"a1"]), "n0");
        assert_eq!(hit, 1, "hard reject must not evict existing blocks");
        assert_eq!(auth.block_count("m", ""), 2);
        assert_eq!(auth.used_bytes("m", ""), 200);
    }

    /// RegisterModel shares SetModelQuota validator (review #3).
    #[test]
    fn p46_register_model_rejects_invalid_quota() {
        let mut auth = Authority::default();
        let err = auth
            .register_model(ModelDescriptor {
                model_id: "bad".into(),
                revision: String::new(),
                num_layers: 1,
                block_spec: Some(BlockSpec {
                    block_tokens: 128,
                    bytes_per_block: 1,
                }),
                hash_algo: HashAlgo::HashSha256256 as i32,
                quota: Some(Quota {
                    soft_bytes: 200,
                    hard_bytes: 100,
                    borrow_enabled: false,
                }),
            })
            .unwrap_err();
        assert!(err.contains("soft_bytes") || err.contains("quota"));

        ensure_model_quota(&mut auth, "ok", 100, 200, false, 1);
        let err = auth
            .register_model(ModelDescriptor {
                model_id: "ok".into(),
                revision: String::new(),
                num_layers: 32,
                block_spec: Some(BlockSpec {
                    block_tokens: 128,
                    bytes_per_block: 1,
                }),
                hash_algo: HashAlgo::HashSha256256 as i32,
                quota: Some(Quota {
                    soft_bytes: -1,
                    hard_bytes: 0,
                    borrow_enabled: false,
                }),
            })
            .unwrap_err();
        assert!(err.contains("non-negative") || err.contains("quota"));
    }

    // --- P4.7: GC / orphan / reconcile / checkpoint ---

    fn meta_l0_l2(model: &str, hash: &[u8]) -> BlockMeta {
        let mut m = meta(model, hash);
        m.locations.push(Location {
            tier: Tier::L0 as i32,
            node_id: "n0".into(),
            segment_id: 1,
            offset: 0,
        });
        m
    }

    /// Cold GC strips L0/L1 but keeps radix + L2 (durable backing).
    #[test]
    fn p47_cold_gc_strips_l0_keeps_l2() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"c0"]);
        auth.register("n0", &full, vec![meta_l0_l2("m", b"c0")])
            .unwrap();
        assert!(auth.has_l0_on("m", "", PoolKind::Target as i32, b"c0", "n0"));
        // Make inactive
        auth.report_ref(&delta("m", b"c0", 1)).unwrap();
        auth.report_ref(&delta("m", b"c0", -1)).unwrap();

        let (stripped, discarded) = auth.gc_cold("m", "", PoolKind::Target as i32, 1).unwrap();
        assert_eq!(stripped.len(), 1);
        assert!(discarded.is_empty());
        assert!(!auth.has_l0_on("m", "", PoolKind::Target as i32, b"c0", "n0"));
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1, "radix + L2 must remain");
    }

    /// Orphan TTL → metadata discard (Mooncake zombie analogue).
    #[test]
    fn p47_orphan_ttl_discard() {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NOW: AtomicU64 = AtomicU64::new(1_000);
        fn fake_now() -> u64 {
            NOW.load(Ordering::SeqCst)
        }

        let mut auth = Authority::default();
        auth.set_now_ms_fn(fake_now);
        ensure_model(&mut auth, "m");
        auth.report_orphans(&[OrphanReport {
            node_id: "n0".into(),
            ids: vec![KvBlockId {
                model_id: "m".into(),
                block_hash: b"zombie".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            }],
            marked_at_ms: 1_000,
        }])
        .unwrap();

        // Not yet expired
        let early = auth.sweep_orphans(30_000);
        assert!(early.is_empty());

        NOW.store(1_000 + 30_000, Ordering::SeqCst);
        let gone = auth.sweep_orphans(30_000);
        assert_eq!(gone.len(), 1);
        assert_eq!(gone[0].block_hash, b"zombie");
    }

    /// Dead-node reconcile clears writeback-held refs (P4.3 leak subset).
    #[test]
    fn p47_dead_node_clears_writeback_ref() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"wb"]);
        auth.register("n0", &full, vec![meta("m", b"wb")]).unwrap();
        // Simulate WRITEBACK+1 then agent crash (no -1).
        auth.report_ref(&RefDelta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"wb".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            }),
            kind: RefKind::Writeback as i32,
            delta: 1,
            node_id: "n0".into(),
        })
        .unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"wb"), 1);
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 0);

        let (cleared, _) = auth.reconcile_dead_node("n0").unwrap();
        assert_eq!(cleared, 1);
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"wb"), 0);
        // Now evictable
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
    }

    /// H4: dead-node reconcile must decrement the L0 presence marker per
    /// dead node, not only when no node holds L0. With a blanket `!any(L0)`
    /// guard the shadow count stays inflated when sibling nodes still hold
    /// L0, so the final dead node leaves count=1 though no L0 remains.
    #[test]
    fn p47_dead_node_l0_presence_precise() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let pk = PoolKind::Target as i32;
        auth.register("n0", &prefix(&[b"l0"]), vec![meta("m", b"l0")])
            .unwrap();
        // Two nodes each hold an L0 replica → presence count = 2.
        auth.publish_location("m", "", pk, b"l0", Tier::L0, "n0", true)
            .unwrap();
        auth.publish_location("m", "", pk, b"l0", Tier::L0, "n1", true)
            .unwrap();
        assert!(auth.has_l0_presence("m", "", pk, b"l0"));
        assert!(auth.has_l0_on("m", "", pk, b"l0", "n0"));
        assert!(auth.has_l0_on("m", "", pk, b"l0", "n1"));

        // Dead n0: count 2→1. n1 still holds L0 → shadow must stay true.
        auth.reconcile_dead_node("n0").unwrap();
        assert!(
            auth.has_l0_presence("m", "", pk, b"l0"),
            "one L0 replica remains on n1"
        );
        assert!(!auth.has_l0_on("m", "", pk, b"l0", "n0"));
        assert!(auth.has_l0_on("m", "", pk, b"l0", "n1"));

        // Dead n1: count 1→0. No L0 left → shadow must read false.
        auth.reconcile_dead_node("n1").unwrap();
        assert!(
            !auth.has_l0_presence("m", "", pk, b"l0"),
            "no L0 replica remains; shadow must be cleared"
        );
        assert!(!auth.has_l0_on("m", "", pk, b"l0", "n1"));
    }

    /// Checkpoint save/restore rebuilds namespace + blocks (metadata before bytes).
    #[test]
    fn p47_checkpoint_roundtrip() {
        let store = MemoryCheckpointStore::new();
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"p"]);
        auth.register("n0", &full, vec![meta("m", b"p")]).unwrap();
        let snap = auth.export_snapshot(1);
        store.save(snap.clone()).unwrap();

        let mut auth2 = Authority::default();
        auth2
            .import_snapshot(&store.load().unwrap().unwrap())
            .unwrap();
        assert!(auth2.has_namespace("m", ""));
        let (_, hit, _) = auth2.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
    }

    /// Multi-block prefix lineage survives checkpoint restore (not flat-root register).
    #[test]
    fn p47_checkpoint_multilineage_restore() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1"]);
        auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
            .unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 2);

        let snap = auth.export_snapshot(2);
        assert_eq!(snap.blocks.len(), 2);
        assert!(
            snap.blocks.iter().any(|b| b.prefix_chain.len() == 2),
            "child block must export full prefix_chain"
        );

        let mut auth2 = Authority::default();
        auth2.import_snapshot(&snap).unwrap();
        let (_, hit2, _) = auth2.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit2, 2, "restore must keep seq_hash lineage for h0→h1");
    }

    /// Restore must fail loudly if the snapshot's model quota rejects a block.
    #[test]
    fn p47_checkpoint_restore_rejects_quota_loss() {
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "m", 0, 0, false, 100);
        auth.register("n0", &prefix(&[b"q"]), vec![meta("m", b"q")])
            .unwrap();
        let mut snap = auth.export_snapshot(3);
        snap.models[0].quota = Some(Quota {
            soft_bytes: 50,
            hard_bytes: 50,
            borrow_enabled: false,
        });

        let mut restored = Authority::default();
        let err = restored.import_snapshot(&snap).unwrap_err();
        assert!(
            err.contains("import_snapshot register rejected"),
            "restore must not report success after dropping a block: {err}"
        );
        assert_eq!(restored.block_count("m", ""), 0);
    }

    /// Durable cold peel re-inserts into inactive for later reclaim accounting.
    #[test]
    fn p47_cold_gc_reinserts_durable_inactive() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"c0"]);
        auth.register("n0", &full, vec![meta_l0_l2("m", b"c0")])
            .unwrap();
        auth.report_ref(&delta("m", b"c0", 1)).unwrap();
        auth.report_ref(&delta("m", b"c0", -1)).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);

        let (stripped, discarded) = auth.gc_cold("m", "", PoolKind::Target as i32, 1).unwrap();
        assert_eq!(stripped.len(), 1);
        assert!(discarded.is_empty());
        assert_eq!(
            auth.inactive_len("m", "", PoolKind::Target as i32),
            1,
            "durable victim must return to inactive after L0/L1 peel"
        );
        // Second GC still sees the block (no-op peel; still reclaimable).
        let (stripped2, _) = auth.gc_cold("m", "", PoolKind::Target as i32, 1).unwrap();
        assert_eq!(stripped2.len(), 1);
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);
    }

    /// Stale orphan mark must not TTL-kill a block that later registered.
    #[test]
    fn p47_orphan_cleared_on_register() {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NOW: AtomicU64 = AtomicU64::new(1_000);
        fn fake_now() -> u64 {
            NOW.load(Ordering::SeqCst)
        }

        let mut auth = Authority::default();
        auth.set_now_ms_fn(fake_now);
        ensure_model(&mut auth, "m");
        let id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"late".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        auth.report_orphans(&[OrphanReport {
            node_id: "n0".into(),
            ids: vec![id.clone()],
            marked_at_ms: 1_000,
        }])
        .unwrap();

        // PutEnd wins before TTL.
        let full = prefix(&[b"late"]);
        auth.register("n0", &full, vec![meta("m", b"late")])
            .unwrap();

        NOW.store(1_000 + 30_000, Ordering::SeqCst);
        let gone = auth.sweep_orphans(30_000);
        assert!(
            gone.is_empty(),
            "registered block must not be discarded by stale orphan TTL"
        );
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
    }

    /// Explicit discard must not remove globally referenced blocks.
    #[test]
    fn p47_discard_rejects_refed_block() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"live"]);
        auth.register("n0", &full, vec![meta("m", b"live")])
            .unwrap();
        auth.report_ref(&delta("m", b"live", 1)).unwrap();

        let id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"live".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        let err = auth.discard_blocks(&[id]).unwrap_err();
        assert!(err.contains("global_refs"));
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1, "refed block must remain visible");
    }

    // --- P4.8: defrag plan + placement ---

    fn meta_l2_at(model: &str, hash: &[u8], seg: u64, off: u64) -> BlockMeta {
        let mut m = meta(model, hash);
        m.locations[0].segment_id = seg;
        m.locations[0].offset = off;
        m
    }

    fn l2_offset(auth: &Authority, model: &str, hash: &[u8]) -> (u64, u64) {
        let id = KvBlockId {
            model_id: model.into(),
            block_hash: hash.to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        let metas = auth.locate(&[id]);
        let loc = metas[0]
            .locations
            .iter()
            .find(|l| l.tier == Tier::L2 as i32)
            .expect("L2");
        (loc.segment_id, loc.offset)
    }

    /// Compact plan detects non-dense L2 offsets in a segment.
    #[test]
    fn p48_plan_compact_on_holes() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 100u64;
        auth.register(
            "n0",
            &prefix(&[b"a", b"b", b"c"]),
            vec![
                meta_l2_at("m", b"a", 1, 0),
                meta_l2_at("m", b"b", 1, 200),
                meta_l2_at("m", b"c", 1, 300),
            ],
        )
        .unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Compact, slot)
            .unwrap();
        assert_eq!(moves.len(), 1);
        assert!(moves[0].compact_segment);
        assert_eq!(moves[0].segment_id, 1);
    }

    /// Compacting a mixed segment would move ref>0 slots; skip the whole segment.
    #[test]
    fn p48_plan_compact_skips_segment_with_refed_block() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 100u64;
        auth.register(
            "n0",
            &prefix(&[b"a", b"b", b"c"]),
            vec![
                meta_l2_at("m", b"a", 1, 0),
                meta_l2_at("m", b"b", 1, 200),
                meta_l2_at("m", b"c", 1, 300),
            ],
        )
        .unwrap();
        auth.report_ref(&delta("m", b"b", 1)).unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Compact, slot)
            .unwrap();
        assert!(
            moves.is_empty(),
            "segment containing ref>0 block must not be compacted: {moves:?}"
        );
    }

    /// Co-locate plan packs a scattered prefix chain onto one segment (same node).
    #[test]
    fn p48_plan_colocate_prefix_chain() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 64u64;
        auth.register(
            "n0",
            &prefix(&[b"h0", b"h1"]),
            vec![meta_l2_at("m", b"h0", 1, 0), meta_l2_at("m", b"h1", 2, 0)],
        )
        .unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Colocate, slot)
            .unwrap();
        assert!(
            moves
                .iter()
                .any(|m| !m.compact_segment && m.to_segment == 1),
            "expected co-locate onto seg1; got {moves:?}"
        );
        assert!(moves.iter().all(|m| m.node_id == "n0"));
    }

    /// Cross-node prefix scatter must NOT yield local CoLocateMove (no Transfer yet).
    #[test]
    fn p48_plan_colocate_skips_cross_node() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 64u64;
        let mut m0 = meta_l2_at("m", b"h0", 1, 0);
        m0.locations[0].node_id = "n0".into();
        let mut m1 = meta_l2_at("m", b"h1", 1, 0);
        m1.locations[0].node_id = "n1".into();
        auth.register("n0", &prefix(&[b"h0", b"h1"]), vec![m0, m1])
            .unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Colocate, slot)
            .unwrap();
        assert!(
            moves.is_empty(),
            "cross-node co-locate deferred to P5; got {moves:?}"
        );
    }

    /// Co-locate must not target a slot held by an unrelated block (arena would fail).
    #[test]
    fn p48_plan_colocate_avoids_occupied_slots() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 64u64;
        // Foreign block owns seg1/off0 — naive pack onto members[0].seg at base 0 would collide.
        auth.register("n0", &prefix(&[b"x"]), vec![meta_l2_at("m", b"x", 1, 0)])
            .unwrap();
        auth.register(
            "n0",
            &prefix(&[b"h0", b"h1"]),
            vec![
                meta_l2_at("m", b"h0", 1, 128), // not dense at 0
                meta_l2_at("m", b"h1", 2, 0),
            ],
        )
        .unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Colocate, slot)
            .unwrap();
        assert!(
            !moves.is_empty(),
            "should still find a free pack target; got empty"
        );
        for m in &moves {
            assert!(
                !(m.to_segment == 1 && m.to_offset == 0),
                "must not target foreign-occupied seg1/0; got {moves:?}"
            );
        }
        // Destinations must not collide with foreign x.
        let (x_seg, x_off) = l2_offset(&auth, "m", b"x");
        for m in &moves {
            assert!(
                !(m.to_segment == x_seg && m.to_offset == x_off),
                "move onto foreign slot; {m:?}"
            );
        }
    }

    // --- P4.9: consistent-hash sharding ---

    #[test]
    fn p49_shard_membership_get_map() {
        let mut auth = Authority::default();
        auth.join_shard_node("n0", 16).unwrap();
        auth.join_shard_node("n1", 16).unwrap();
        let map = auth.shard_map();
        assert_eq!(map.nodes.len(), 2);
        assert!(map.generation >= 2);
        assert!(auth.shard_owner(b"any-key").is_some());
    }

    #[test]
    fn p49_expand_minimal_migration() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.join_shard_node("n0", 32).unwrap();
        auth.join_shard_node("n1", 32).unwrap();
        // Register many blocks so join n2 produces measurable (but bounded) moves.
        for i in 0..80u32 {
            let h = format!("b{i:03}").into_bytes();
            auth.register("n0", &prefix(&[&h]), vec![meta("m", &h)])
                .unwrap();
        }
        let (_map, migs) = auth.join_shard_node("n2", 32).unwrap();
        assert!(
            !migs.is_empty() && migs.len() < 80,
            "expand should move a subset, got {}",
            migs.len()
        );
        assert!(migs.iter().all(|m| m.to_node == "n2"));
        assert!(migs
            .iter()
            .all(|m| m.from_node == "n0" || m.from_node == "n1"));
        assert!(migs.iter().all(|m| !m.push_l2_first));
    }

    #[test]
    fn p49_drain_push_l2_and_remove() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.join_shard_node("n0", 32).unwrap();
        auth.join_shard_node("n1", 32).unwrap();
        // Place blocks with L2 on n1.
        for i in 0..20u32 {
            let h = format!("d{i:02}").into_bytes();
            let mut m = meta("m", &h);
            m.locations[0].node_id = "n1".into();
            auth.register("n1", &prefix(&[&h]), vec![m]).unwrap();
        }
        let (map, migs, push) = auth.drain_shard_node("n1").unwrap();
        assert!(map.nodes.iter().any(|n| n.node_id == "n1" && n.draining));
        assert!(!push.is_empty(), "Drain must list L2 push candidates");
        assert!(migs.iter().all(|m| m.from_node == "n1" && m.push_l2_first));
        // After drain, no key should own on n1.
        for id in &push {
            assert_ne!(auth.shard_owner(&id.block_hash).as_deref(), Some("n1"));
        }
        // Ownership remap ≠ physical done: remove must fail while L2 remains.
        let err = auth.remove_shard_node("n1").unwrap_err();
        assert!(
            err.contains("placement"),
            "expected placement gate, got {err}"
        );
        // Simulate migration completion: clear n1 L2 from the location view.
        for id in &push {
            auth.publish_location(
                &id.model_id,
                &id.revision,
                id.pool_kind,
                &id.block_hash,
                Tier::L2,
                "n1",
                false,
            )
            .unwrap();
        }
        auth.remove_shard_node("n1").unwrap();
        let map2 = auth.shard_map();
        assert!(!map2.nodes.iter().any(|n| n.node_id == "n1"));
    }

    /// RemoveShardNode must not succeed solely because ownership left the ring.
    #[test]
    fn p49_remove_refuses_while_l2_present() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.join_shard_node("n0", 16).unwrap();
        auth.join_shard_node("n1", 16).unwrap();
        let mut m = meta("m", b"stuck");
        m.locations[0].node_id = "n1".into();
        auth.register("n1", &prefix(&[b"stuck"]), vec![m]).unwrap();
        auth.drain_shard_node("n1").unwrap();
        assert!(auth
            .remove_shard_node("n1")
            .unwrap_err()
            .contains("placement"));
        // Still in shard map as draining.
        assert!(auth
            .shard_map()
            .nodes
            .iter()
            .any(|n| n.node_id == "n1" && n.draining));
    }

    /// Drain must keep TARGET and DRAFT blocks separate even if their flat hash matches.
    #[test]
    fn p49_drain_keeps_pool_kind_identities() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.join_shard_node("n0", 16).unwrap();
        auth.join_shard_node("n1", 16).unwrap();

        let mut target = meta("m", b"same");
        target.locations[0].node_id = "n1".into();
        auth.register("n1", &prefix(&[b"same"]), vec![target])
            .unwrap();

        let mut draft = meta("m", b"same");
        draft.id.as_mut().unwrap().pool_kind = PoolKind::Draft as i32;
        draft.locations[0].node_id = "n1".into();
        auth.register("n1", &prefix(&[b"same"]), vec![draft])
            .unwrap();

        let (_map, _migs, push) = auth.drain_shard_node("n1").unwrap();
        let same: Vec<_> = push
            .iter()
            .filter(|id| id.model_id == "m" && id.block_hash == b"same".to_vec())
            .map(|id| id.pool_kind)
            .collect();
        assert_eq!(
            same.len(),
            2,
            "TARGET and DRAFT with same hash must both be pushed: {push:?}"
        );
        assert!(same.contains(&(PoolKind::Target as i32)));
        assert!(same.contains(&(PoolKind::Draft as i32)));
    }

    /// relocate_in_view updates segment/offset; PauseBackground flips flag.
    #[test]
    fn p48_relocate_and_pause_flag() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.register("n0", &prefix(&[b"p"]), vec![meta("m", b"p")])
            .unwrap();
        auth.relocate_in_view(
            "m",
            "",
            PoolKind::Target as i32,
            b"p",
            Tier::L2,
            "n0",
            7,
            128,
        )
        .unwrap();
        assert_eq!(l2_offset(&auth, "m", b"p"), (7, 128));

        let cp = ControlPlane::new();
        assert!(!cp.background_paused());
        cp.set_background_paused(true);
        assert!(cp.background_paused());
        cp.set_background_paused(false);
        assert!(!cp.background_paused());
    }
}
