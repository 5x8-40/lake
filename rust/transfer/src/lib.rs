//! KV 传输层(P4.4)。
//!
//! - [`Transport`] trait:抄 Mooncake `transport.h` API
//!   (`allocateBatchID` / `submitTransfer` / `getTransferStatus` / `openSegment`)
//! - [`TcpTransport`]:TCP 退化(进程内段拷贝;= `MC_FORCE_TCP` 站位)
//! - [`TransferServer`]:`TransferService` gRPC 接线
//! - [`InMemoryByteStore`]:内容寻址字节(与 `TcpDataService` / Pull 共用形态)
//!
//! 真 RDMA → P5 `RdmaTransport`(同 trait)。
//!
//! 参考:`docs/research/mooncake/transfer-engine.md`。

mod bytes;
mod error;
mod location;
mod service;
mod tcp;
mod transport;

pub use bytes::InMemoryByteStore;
pub use error::{Result, TransferError};
pub use location::validate_location_tier;
pub use service::TransferServer;
pub use tcp::TcpTransport;
pub use transport::{BatchId, SegmentId, TaskState, TaskStatus, TransferOp, Transport};

pub use lake_proto::lake::*;
