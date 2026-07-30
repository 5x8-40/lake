//! `Transport` trait — 抄 Mooncake `transport.h::Transport` API 形态。
//!
//! 生命周期(对齐 `TransferEngine`):
//! ```text
//! open_segment / register_memory
//!   → allocate_batch_id(N)
//!   → submit_transfer(batch_id, {TransferOp...})
//!   → get_transfer_status(batch_id, task_id)
//!   → free_batch_id(batch_id)
//! ```
//!
//! 参考:`3rdparty/mooncake/.../include/transport/transport.h`
//! (`allocateBatchID` / `submitTransfer` / `getTransferStatus` / `freeBatchID`)。

use lake_proto::lake::Location;

use crate::error::Result;

pub type SegmentId = u64;
pub type BatchId = u64;

/// 单条传输(对齐 Mooncake `TransferRequest`,opcode 固定 WRITE)。
#[derive(Clone, Debug)]
pub struct TransferOp {
    pub source: Location,
    pub target_segment_id: SegmentId,
    pub target_offset: u64,
    pub length: u64,
}

/// 任务状态(对齐 Mooncake `TransferStatusEnum` + proto `TransferStatusResponse.State`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TaskState {
    Pending,
    InFlight,
    Done,
    Failed,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TaskStatus {
    pub state: TaskState,
    pub bytes_done: u64,
}

/// 传输后端抽象。P4=`TcpTransport`;P5=`RdmaTransport`。
pub trait Transport: Send + Sync {
    /// 打开/分配一段可寻址缓冲区(仿 Mooncake `openSegment` + 本地 register)。
    fn open_segment(&self, name: &str, capacity: usize) -> Result<SegmentId>;

    /// 释放段(Pull dest / 临时缓冲);对齐 batch 的 freeBatchID 生命周期。
    fn free_segment(&self, segment_id: SegmentId) -> Result<()>;

    /// 把字节写入段内 offset(仿 registerLocalMemory 后的本地填充 / TCP payload 落点)。
    fn write_segment(&self, segment_id: SegmentId, offset: u64, data: &[u8]) -> Result<()>;

    /// 读段内字节(单测 / Pull 落点校验)。
    fn read_segment(&self, segment_id: SegmentId, offset: u64, length: usize) -> Result<Vec<u8>>;

    fn allocate_batch_id(&self, batch_size: usize) -> Result<BatchId>;

    fn free_batch_id(&self, batch_id: BatchId) -> Result<()>;

    /// 提交一批传输。成功后各 task 至少进入 InFlight/Done。
    fn submit_transfer(&self, batch_id: BatchId, ops: &[TransferOp]) -> Result<()>;

    fn get_transfer_status(&self, batch_id: BatchId, task_id: u64) -> Result<TaskStatus>;
}
