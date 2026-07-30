//! Agent → ControlPlane 端口（P4.3）。
//!
//! 进程内 [`AuthorityPort`] 供单测 / 同进程联调；真 tonic 客户端可另实现本 trait。
//! `TierPipeline::tick` 返回的 [`lake_tiered_store::LocationEvent`] 经
//! [`apply_location_events`] 刷到 CP（pipeline 本身不持 CP 句柄）。
//! P4.8:`Moved` → `relocate_in_view`；`DefragMove` → pipeline enqueue。

use lake_controlplane::Authority;
use lake_proto::lake::*;
use lake_tiered_store::{LocalTier, LocationEvent, PipelineAction, TierPipeline};

/// Subset of ControlPlane RPCs used by PutEnd / tier publish.
pub trait ControlPlanePort {
    /// Quota preflight **before** durable flush.
    ///
    /// Mirrors proto `ControlPlaneService.AdmitRegisterBlocks` (P4.6 方案 A).
    /// In-process [`AuthorityPort`] calls `Authority::preflight_register` directly;
    /// tonic clients should call the same RPC over the wire.
    fn admit_register_blocks(&mut self, req: &RegisterBlocksRequest) -> Result<(), String>;
    fn register_blocks(&mut self, req: RegisterBlocksRequest) -> Result<(), String>;
    fn report_refs(&mut self, deltas: &[RefDelta]) -> Result<(), String>;
    fn request_barrier(&mut self, req: RequestBarrierRequest) -> Result<(), String>;
    #[allow(clippy::too_many_arguments)] // mirrors Authority::publish_location
    fn publish_location(
        &mut self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
        tier: Tier,
        node_id: &str,
        present: bool,
    ) -> Result<(), String>;
    fn set_l3_present(
        &mut self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
        present: bool,
    ) -> Result<(), String>;
    /// P4.8: update segment/offset after defrag Moved.
    #[allow(clippy::too_many_arguments)] // mirrors Authority::relocate_in_view
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
    ) -> Result<(), String>;
}

/// In-process Authority (no gRPC).
pub struct AuthorityPort<'a> {
    pub auth: &'a mut Authority,
}

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
        .ok_or_else(|| "AdmitRegister: no KVBlockID".to_string())?;
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
        return Err("AdmitRegister: no block hashes".into());
    }
    Ok(AdmitKeys {
        model_id,
        revision,
        pool_kind,
        hashes,
    })
}

impl ControlPlanePort for AuthorityPort<'_> {
    fn admit_register_blocks(&mut self, req: &RegisterBlocksRequest) -> Result<(), String> {
        use lake_controlplane::RegisterStatus;
        let keys = admit_keys_from_request(req)?;
        match self.auth.preflight_register(
            &keys.model_id,
            &keys.revision,
            keys.pool_kind,
            &keys.hashes,
        )? {
            RegisterStatus::Accepted => Ok(()),
            RegisterStatus::RejectedHardQuota(bp) => Err(format!(
                "AdmitRegister: admission rejected reason={} deficit={}",
                bp.reason, bp.deficit_bytes
            )),
        }
    }

    fn register_blocks(&mut self, req: RegisterBlocksRequest) -> Result<(), String> {
        use lake_controlplane::RegisterStatus;
        match self
            .auth
            .register(&req.node_id, &req.prefix_hashes, req.blocks)?
        {
            RegisterStatus::Accepted => Ok(()),
            RegisterStatus::RejectedHardQuota(bp) => Err(format!(
                "RegisterBlocks: admission rejected reason={} deficit={}",
                bp.reason, bp.deficit_bytes
            )),
        }
    }

    fn report_refs(&mut self, deltas: &[RefDelta]) -> Result<(), String> {
        self.auth.report_refs(deltas)
    }

    fn request_barrier(&mut self, req: RequestBarrierRequest) -> Result<(), String> {
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

/// Apply pipeline location hints to the controlplane view.
///
/// L1 arms are currently **dead** on the producer side: P4.3 `TierPipeline` /
/// PutEnd never emit L1 Present/Absent (`ensure_l1_room` drops L1 silently).
/// Kept so a future L1 presence publisher can reuse this path without API churn.
pub fn apply_location_events<P: ControlPlanePort>(
    cp: &mut P,
    model_id: &str,
    revision: &str,
    pool_kind: i32,
    node_id: &str,
    events: &[LocationEvent],
) -> Result<(), String> {
    for ev in events {
        match ev {
            LocationEvent::Present { hash, tier } => match tier {
                LocalTier::L0 => cp.publish_location(
                    model_id,
                    revision,
                    pool_kind,
                    hash,
                    Tier::L0,
                    node_id,
                    true,
                )?,
                LocalTier::L1 => cp.publish_location(
                    model_id,
                    revision,
                    pool_kind,
                    hash,
                    Tier::L1,
                    node_id,
                    true,
                )?,
                LocalTier::L2 => cp.publish_location(
                    model_id,
                    revision,
                    pool_kind,
                    hash,
                    Tier::L2,
                    node_id,
                    true,
                )?,
                LocalTier::L3 => cp.set_l3_present(model_id, revision, pool_kind, hash, true)?,
            },
            LocationEvent::Absent { hash, tier } => match tier {
                LocalTier::L0 => cp.publish_location(
                    model_id,
                    revision,
                    pool_kind,
                    hash,
                    Tier::L0,
                    node_id,
                    false,
                )?,
                LocalTier::L1 => cp.publish_location(
                    model_id,
                    revision,
                    pool_kind,
                    hash,
                    Tier::L1,
                    node_id,
                    false,
                )?,
                LocalTier::L2 => cp.publish_location(
                    model_id,
                    revision,
                    pool_kind,
                    hash,
                    Tier::L2,
                    node_id,
                    false,
                )?,
                LocalTier::L3 => cp.set_l3_present(model_id, revision, pool_kind, hash, false)?,
            },
            LocationEvent::Moved {
                hash,
                tier,
                segment_id,
                offset,
                node_id: move_node,
            } => {
                let n = if move_node.is_empty() {
                    node_id
                } else {
                    move_node.as_str()
                };
                let wire_tier = match tier {
                    LocalTier::L0 => Tier::L0,
                    LocalTier::L1 => Tier::L1,
                    LocalTier::L2 => Tier::L2,
                    LocalTier::L3 => {
                        // L3 has no segment coords — ignore.
                        continue;
                    }
                };
                cp.relocate_in_view(
                    model_id,
                    revision,
                    pool_kind,
                    hash,
                    wire_tier,
                    n,
                    *segment_id,
                    *offset,
                )?;
            }
        }
    }
    Ok(())
}

/// Enqueue CP-planned defrag moves onto the tier pipeline.
///
/// Skips moves whose `node_id` ≠ `pipe.node_id` (P4.8: local execution only;
/// cross-node co-locate needs Transfer — defer P5).
pub fn enqueue_defrag_moves(pipe: &mut TierPipeline, moves: &[DefragMove]) {
    for m in moves {
        if !m.node_id.is_empty() && m.node_id != pipe.node_id {
            continue;
        }
        if m.compact_segment {
            pipe.enqueue(PipelineAction::CompactSegment {
                segment_id: m.segment_id,
            });
        } else if let Some(id) = &m.id {
            pipe.enqueue(PipelineAction::CoLocateMove {
                hash: id.block_hash.clone(),
                dest_segment: m.to_segment,
                dest_offset: m.to_offset,
            });
        }
    }
}

/// Sync CP `PauseBackground` onto the shared [`lake_tiered_store::BandwidthPool`].
pub fn sync_background_pause(pipe: &mut TierPipeline, paused: bool) {
    if paused {
        pipe.bandwidth.pause();
    } else {
        pipe.bandwidth.resume();
    }
}
