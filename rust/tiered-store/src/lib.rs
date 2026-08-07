//! L0–L3 分层缓存引擎（P4.3）+ 段压实/共置（P4.8）。
//!
//! 参考：Dynamo kvbm-engine `OffloadPolicy` / `Pipeline`（demote-only）；
//! SGLang `hiradix_cache.py::_evict_write_back`；Mooncake PutEnd /
//! `OffsetBufferAllocator`（无 compaction——lake 主动整理）。
//! 关键差异：无 `BlockStore`；位置视图权威在 controlplane；本 crate 持不透明字节。
//! L2/L3 用进程内 HashMap 站位真 NVMe/对象存储（P4 单测判据；真介质 defer P5）。
//! 写回只落 L2；L3 仅 demote/cap（稳态 XOR）。Pipeline 经 [`LocationEvent`] 刷 CP。
//! P4.8:[`SegmentArena`] + `CompactSegment`/`CoLocateMove` 共享 [`BandwidthPool`]。

mod bandwidth;
mod engine;
mod pipeline;
mod segment;
mod stats;
mod writeback;

pub use bandwidth::BandwidthPool;
pub use engine::{BeginPromote, LocalTier, LocalTierEngine, TierCaps, TierSideEffects};
pub use pipeline::{LocationEvent, PipelineAction, TierPipeline};
pub use segment::{Placement, Relocate, SegmentArena, DEFAULT_SLOT_BYTES};
pub use stats::{AccessKind, HitStats, TierCost};
pub use writeback::WritebackBatcher;

pub use lake_proto::lake::*;
