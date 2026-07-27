//! L0–L3 分层缓存引擎（P4.3）。
//!
//! 参考：Dynamo kvbm-engine `OffloadPolicy` / `Pipeline`（demote-only）；
//! SGLang `hiradix_cache.py::_evict_write_back`；Mooncake PutEnd。
//! 关键差异：无 `BlockStore`；位置视图权威在 controlplane；本 crate 持不透明字节。
//! L2/L3 用进程内 HashMap 站位真 NVMe/对象存储（P4 单测判据；真介质 defer P5）。
//! 写回只落 L2；L3 仅 demote/cap（稳态 XOR）。Pipeline 经 [`LocationEvent`] 刷 CP。

mod bandwidth;
mod engine;
mod pipeline;
mod stats;

pub use bandwidth::BandwidthPool;
pub use engine::{LocalTier, LocalTierEngine, TierCaps, TierSideEffects};
pub use pipeline::{LocationEvent, PipelineAction, TierPipeline};
pub use stats::{AccessKind, HitStats, TierCost};

pub use lake_proto::lake::*;
