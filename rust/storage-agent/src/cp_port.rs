//! Agent → ControlPlane 端口（P4.3）。
//!
//! 进程内 [`AuthorityPort`] 供单测 / 同进程联调；真 tonic 客户端可另实现本 trait。
//! `TierPipeline::tick` 返回的 [`lake_tiered_store::LocationEvent`] 经
//! [`apply_location_events`] 刷到 CP（pipeline 本身不持 CP 句柄）。

use lake_controlplane::Authority;
use lake_proto::lake::*;
use lake_tiered_store::{LocalTier, LocationEvent};

/// Subset of ControlPlane RPCs used by PutEnd / tier publish.
pub trait ControlPlanePort {
    fn register_blocks(&mut self, req: RegisterBlocksRequest) -> Result<(), String>;
    fn report_refs(&mut self, deltas: &[RefDelta]) -> Result<(), String>;
    fn request_barrier(&mut self, req: RequestBarrierRequest) -> Result<(), String>;
    fn publish_location(
        &mut self,
        model_id: &str,
        flat: &[u8],
        tier: Tier,
        node_id: &str,
        present: bool,
    ) -> Result<(), String>;
    fn set_l3_present(&mut self, model_id: &str, flat: &[u8], present: bool) -> Result<(), String>;
}

/// In-process Authority (no gRPC).
pub struct AuthorityPort<'a> {
    pub auth: &'a mut Authority,
}

impl ControlPlanePort for AuthorityPort<'_> {
    fn register_blocks(&mut self, req: RegisterBlocksRequest) -> Result<(), String> {
        self.auth
            .register(&req.node_id, &req.prefix_hashes, req.blocks)
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
        flat: &[u8],
        tier: Tier,
        node_id: &str,
        present: bool,
    ) -> Result<(), String> {
        self.auth
            .publish_location(model_id, flat, tier, node_id, present)
    }

    fn set_l3_present(&mut self, model_id: &str, flat: &[u8], present: bool) -> Result<(), String> {
        self.auth.set_l3_present(model_id, flat, present)
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
    node_id: &str,
    events: &[LocationEvent],
) -> Result<(), String> {
    for ev in events {
        match ev {
            LocationEvent::Present { hash, tier } => match tier {
                LocalTier::L0 => cp.publish_location(model_id, hash, Tier::L0, node_id, true)?,
                // Reserved: no P4.3 producer emits L1 (see `ensure_l1_room`).
                LocalTier::L1 => cp.publish_location(model_id, hash, Tier::L1, node_id, true)?,
                LocalTier::L2 => cp.publish_location(model_id, hash, Tier::L2, node_id, true)?,
                LocalTier::L3 => cp.set_l3_present(model_id, hash, true)?,
            },
            LocationEvent::Absent { hash, tier } => match tier {
                LocalTier::L0 => cp.publish_location(model_id, hash, Tier::L0, node_id, false)?,
                LocalTier::L1 => cp.publish_location(model_id, hash, Tier::L1, node_id, false)?,
                LocalTier::L2 => cp.publish_location(model_id, hash, Tier::L2, node_id, false)?,
                LocalTier::L3 => cp.set_l3_present(model_id, hash, false)?,
            },
        }
    }
    Ok(())
}
