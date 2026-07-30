//! 传输错误。

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransferError {
    /// `Location.tier` 违反硬约束(须 ∈ {L0,L1,L2};L3 用 `l3_present`)。
    InvalidTier(i32),
    UnknownBatch(u64),
    UnknownTask {
        batch_id: u64,
        task_id: u64,
    },
    UnknownSegment(u64),
    OutOfRange {
        segment_id: u64,
        offset: u64,
        length: u64,
        capacity: usize,
    },
    BatchFull,
    BatchInFlight(u64),
    BatchSubmitted(u64),
    EmptyBatch,
    Other(String),
}

impl fmt::Display for TransferError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidTier(t) => write!(
                f,
                "Location.tier={t} rejected (hard constraint: L0/L1/L2 only; L3 uses l3_present)"
            ),
            Self::UnknownBatch(id) => write!(f, "unknown batch_id={id}"),
            Self::UnknownTask { batch_id, task_id } => {
                write!(f, "unknown task batch_id={batch_id} task_id={task_id}")
            }
            Self::UnknownSegment(id) => write!(f, "unknown segment_id={id}"),
            Self::OutOfRange {
                segment_id,
                offset,
                length,
                capacity,
            } => write!(
                f,
                "segment {segment_id} OOB offset={offset} length={length} capacity={capacity}"
            ),
            Self::BatchFull => write!(f, "batch task slots exhausted"),
            Self::BatchInFlight(id) => write!(f, "batch_id={id} is in flight"),
            Self::BatchSubmitted(id) => write!(f, "batch_id={id} already submitted"),
            Self::EmptyBatch => write!(f, "empty transfer batch"),
            Self::Other(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for TransferError {}

impl From<TransferError> for tonic::Status {
    fn from(e: TransferError) -> Self {
        match e {
            TransferError::InvalidTier(_) => tonic::Status::invalid_argument(e.to_string()),
            TransferError::UnknownBatch(_)
            | TransferError::UnknownTask { .. }
            | TransferError::UnknownSegment(_) => tonic::Status::not_found(e.to_string()),
            TransferError::OutOfRange { .. }
            | TransferError::BatchFull
            | TransferError::BatchInFlight(_)
            | TransferError::BatchSubmitted(_)
            | TransferError::EmptyBatch => tonic::Status::failed_precondition(e.to_string()),
            TransferError::Other(_) => tonic::Status::internal(e.to_string()),
        }
    }
}

pub type Result<T> = std::result::Result<T, TransferError>;
