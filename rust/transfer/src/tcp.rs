//! `TcpTransport` — Mooncake `TcpTransport` / `MC_FORCE_TCP` 的 Rust 站位。
//!
//! 单进程语义:段间 **CPU 拷贝**(非零拷贝),对齐 Mooncake TCP ASIO 路径。
//! 真跨机 gRPC 字节走 `TcpDataService` Put/Get;本实现先把段式寻址与批状态机做实,
//! P5 同 `Transport` trait 换 `RdmaTransport`。
//!
//! 参考:
//! - `transport.h::Transport::{allocateBatchID,submitTransfer,getTransferStatus}`
//! - `tcp_transport.cpp::TcpTransport`(CPU copy,单 Slice)
//! - `transfer_engine_impl.cpp` init:`MC_FORCE_TCP` → `installTransport("tcp")`

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use crate::error::{Result, TransferError};
use crate::location::validate_location_tier;
use crate::transport::{BatchId, SegmentId, TaskState, TaskStatus, TransferOp, Transport};

struct Segment {
    /// 段名(Mooncake openSegment name);寻址用 SegmentId,名仅观测。
    #[allow(dead_code)]
    name: String,
    buf: Vec<u8>,
}

struct Task {
    state: TaskState,
    bytes_done: u64,
}

struct Batch {
    tasks: Vec<Task>,
    submitted: bool,
    in_flight: bool,
}

/// TCP 退化传输引擎(进程内段 arena + 批状态)。
pub struct TcpTransport {
    next_segment: AtomicU64,
    next_batch: AtomicU64,
    segments: Mutex<HashMap<SegmentId, Segment>>,
    batches: Mutex<HashMap<BatchId, Batch>>,
}

impl Default for TcpTransport {
    fn default() -> Self {
        Self::new()
    }
}

impl TcpTransport {
    pub fn new() -> Self {
        Self {
            // 0 保留为非法;与 putend 占位 segment_id=1 可共存
            next_segment: AtomicU64::new(1),
            next_batch: AtomicU64::new(1),
            segments: Mutex::new(HashMap::new()),
            batches: Mutex::new(HashMap::new()),
        }
    }

    pub fn segment_capacity(&self, segment_id: SegmentId) -> Result<usize> {
        let segs = self.segments.lock().unwrap();
        segs.get(&segment_id)
            .map(|s| s.buf.len())
            .ok_or(TransferError::UnknownSegment(segment_id))
    }

    /// 观测用:当前未 free 的 batch 数(防泄漏回归测)。
    pub fn batch_count(&self) -> usize {
        self.batches.lock().unwrap().len()
    }

    /// 观测用:当前未 free 的 segment 数(Pull dest 泄漏回归测)。
    pub fn segment_count(&self) -> usize {
        self.segments.lock().unwrap().len()
    }
}

impl Transport for TcpTransport {
    fn open_segment(&self, name: &str, capacity: usize) -> Result<SegmentId> {
        let id = self.next_segment.fetch_add(1, Ordering::Relaxed);
        let mut segs = self.segments.lock().unwrap();
        segs.insert(
            id,
            Segment {
                name: name.to_string(),
                buf: vec![0u8; capacity],
            },
        );
        Ok(id)
    }

    fn free_segment(&self, segment_id: SegmentId) -> Result<()> {
        let mut segs = self.segments.lock().unwrap();
        segs.remove(&segment_id)
            .map(|_| ())
            .ok_or(TransferError::UnknownSegment(segment_id))
    }

    fn write_segment(&self, segment_id: SegmentId, offset: u64, data: &[u8]) -> Result<()> {
        let mut segs = self.segments.lock().unwrap();
        let seg = segs
            .get_mut(&segment_id)
            .ok_or(TransferError::UnknownSegment(segment_id))?;
        let start = offset as usize;
        let end = start
            .checked_add(data.len())
            .ok_or(TransferError::OutOfRange {
                segment_id,
                offset,
                length: data.len() as u64,
                capacity: seg.buf.len(),
            })?;
        if end > seg.buf.len() {
            return Err(TransferError::OutOfRange {
                segment_id,
                offset,
                length: data.len() as u64,
                capacity: seg.buf.len(),
            });
        }
        seg.buf[start..end].copy_from_slice(data);
        Ok(())
    }

    fn read_segment(&self, segment_id: SegmentId, offset: u64, length: usize) -> Result<Vec<u8>> {
        let segs = self.segments.lock().unwrap();
        let seg = segs
            .get(&segment_id)
            .ok_or(TransferError::UnknownSegment(segment_id))?;
        let start = offset as usize;
        let end = start.checked_add(length).ok_or(TransferError::OutOfRange {
            segment_id,
            offset,
            length: length as u64,
            capacity: seg.buf.len(),
        })?;
        if end > seg.buf.len() {
            return Err(TransferError::OutOfRange {
                segment_id,
                offset,
                length: length as u64,
                capacity: seg.buf.len(),
            });
        }
        Ok(seg.buf[start..end].to_vec())
    }

    fn allocate_batch_id(&self, batch_size: usize) -> Result<BatchId> {
        if batch_size == 0 {
            return Err(TransferError::EmptyBatch);
        }
        let id = self.next_batch.fetch_add(1, Ordering::Relaxed);
        let tasks = (0..batch_size)
            .map(|_| Task {
                state: TaskState::Pending,
                bytes_done: 0,
            })
            .collect();
        self.batches.lock().unwrap().insert(
            id,
            Batch {
                tasks,
                submitted: false,
                in_flight: false,
            },
        );
        Ok(id)
    }

    fn free_batch_id(&self, batch_id: BatchId) -> Result<()> {
        let mut batches = self.batches.lock().unwrap();
        let Some(batch) = batches.get(&batch_id) else {
            return Err(TransferError::UnknownBatch(batch_id));
        };
        if batch.in_flight {
            return Err(TransferError::BatchInFlight(batch_id));
        }
        batches.remove(&batch_id);
        Ok(())
    }

    fn submit_transfer(&self, batch_id: BatchId, ops: &[TransferOp]) -> Result<()> {
        if ops.is_empty() {
            return Err(TransferError::EmptyBatch);
        }
        for op in ops {
            validate_location_tier(&op.source)?;
        }

        // 1) 先 claim batch(未知 / 超容 / in-flight)——**禁止**在校验失败前改写目标段。
        {
            let mut batches = self.batches.lock().unwrap();
            let batch = batches
                .get_mut(&batch_id)
                .ok_or(TransferError::UnknownBatch(batch_id))?;
            if ops.len() > batch.tasks.len() {
                return Err(TransferError::BatchFull);
            }
            if batch.submitted {
                return Err(TransferError::BatchSubmitted(batch_id));
            }
            if batch.in_flight {
                return Err(TransferError::BatchInFlight(batch_id));
            }
            batch.in_flight = true;
        }

        // 2) 再校验全部源/目标范围并完成 CPU 拷贝。整个 segment 校验+拷贝
        // 放在同一把锁下,避免并发 free_segment 在校验后删除段导致 panic。
        let copy_result: Result<()> = (|| {
            let mut segs = self.segments.lock().unwrap();
            for op in ops {
                let src = segs
                    .get(&op.source.segment_id)
                    .ok_or(TransferError::UnknownSegment(op.source.segment_id))?;
                let dst = segs
                    .get(&op.target_segment_id)
                    .ok_or(TransferError::UnknownSegment(op.target_segment_id))?;
                let src_end = (op.source.offset as usize)
                    .checked_add(op.length as usize)
                    .ok_or(TransferError::OutOfRange {
                        segment_id: op.source.segment_id,
                        offset: op.source.offset,
                        length: op.length,
                        capacity: src.buf.len(),
                    })?;
                if src_end > src.buf.len() {
                    return Err(TransferError::OutOfRange {
                        segment_id: op.source.segment_id,
                        offset: op.source.offset,
                        length: op.length,
                        capacity: src.buf.len(),
                    });
                }
                let dst_end = (op.target_offset as usize)
                    .checked_add(op.length as usize)
                    .ok_or(TransferError::OutOfRange {
                        segment_id: op.target_segment_id,
                        offset: op.target_offset,
                        length: op.length,
                        capacity: dst.buf.len(),
                    })?;
                if dst_end > dst.buf.len() {
                    return Err(TransferError::OutOfRange {
                        segment_id: op.target_segment_id,
                        offset: op.target_offset,
                        length: op.length,
                        capacity: dst.buf.len(),
                    });
                }
            }

            // TCP 语义是 CPU 拷贝;先 snapshot source payload,同段重叠也安全。
            let payloads: Result<Vec<Vec<u8>>> = ops
                .iter()
                .map(|op| {
                    let src = segs
                        .get(&op.source.segment_id)
                        .ok_or(TransferError::UnknownSegment(op.source.segment_id))?;
                    let start = op.source.offset as usize;
                    let end = start + op.length as usize;
                    Ok(src.buf[start..end].to_vec())
                })
                .collect();
            let payloads = payloads?;
            for (op, data) in ops.iter().zip(payloads.iter()) {
                let dst = segs
                    .get_mut(&op.target_segment_id)
                    .ok_or(TransferError::UnknownSegment(op.target_segment_id))?;
                let start = op.target_offset as usize;
                let end = start + data.len();
                dst.buf[start..end].copy_from_slice(data);
            }
            Ok(())
        })();
        if let Err(e) = copy_result {
            if let Some(batch) = self.batches.lock().unwrap().get_mut(&batch_id) {
                batch.in_flight = false;
            }
            return Err(e);
        }

        // 3) 更新批状态(batch 已在步骤 1 校验存在且容量足够)。
        let mut batches = self.batches.lock().unwrap();
        let batch = batches
            .get_mut(&batch_id)
            .ok_or(TransferError::UnknownBatch(batch_id))?;
        for (i, op) in ops.iter().enumerate() {
            batch.tasks[i] = Task {
                state: TaskState::Done,
                bytes_done: op.length,
            };
        }
        // 未使用的预分配槽保持 Pending
        batch.submitted = true;
        batch.in_flight = false;
        Ok(())
    }

    fn get_transfer_status(&self, batch_id: BatchId, task_id: u64) -> Result<TaskStatus> {
        let batches = self.batches.lock().unwrap();
        let batch = batches
            .get(&batch_id)
            .ok_or(TransferError::UnknownBatch(batch_id))?;
        let task = batch
            .tasks
            .get(task_id as usize)
            .ok_or(TransferError::UnknownTask { batch_id, task_id })?;
        Ok(TaskStatus {
            state: task.state,
            bytes_done: task.bytes_done,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lake_proto::lake::{Location, Tier};

    fn loc(seg: u64, off: u64, tier: Tier) -> Location {
        Location {
            tier: tier as i32,
            node_id: "n0".into(),
            segment_id: seg,
            offset: off,
        }
    }

    #[test]
    fn submit_then_status_copies_bytes() {
        let t = TcpTransport::new();
        let src = t.open_segment("src", 64).unwrap();
        let dst = t.open_segment("dst", 64).unwrap();
        t.write_segment(src, 0, b"hello-kv-block").unwrap();

        let batch = t.allocate_batch_id(1).unwrap();
        t.submit_transfer(
            batch,
            &[TransferOp {
                source: loc(src, 0, Tier::L2),
                target_segment_id: dst,
                target_offset: 8,
                length: 14,
            }],
        )
        .unwrap();

        let st = t.get_transfer_status(batch, 0).unwrap();
        assert_eq!(st.state, TaskState::Done);
        assert_eq!(st.bytes_done, 14);
        assert_eq!(&t.read_segment(dst, 8, 14).unwrap(), b"hello-kv-block");
        t.free_batch_id(batch).unwrap();
    }

    #[test]
    fn reject_tier_l3_on_submit() {
        let t = TcpTransport::new();
        let src = t.open_segment("src", 16).unwrap();
        let dst = t.open_segment("dst", 16).unwrap();
        t.write_segment(src, 0, b"0123456789abcdef").unwrap();
        let batch = t.allocate_batch_id(1).unwrap();
        let err = t
            .submit_transfer(
                batch,
                &[TransferOp {
                    source: loc(src, 0, Tier::L3),
                    target_segment_id: dst,
                    target_offset: 0,
                    length: 8,
                }],
            )
            .unwrap_err();
        assert!(matches!(err, TransferError::InvalidTier(_)));
    }

    #[test]
    fn failed_submit_does_not_mutate_dst() {
        // review #32:未知 batch / BatchFull 不得先写目标段。
        let t = TcpTransport::new();
        let src = t.open_segment("src", 16).unwrap();
        let dst = t.open_segment("dst", 16).unwrap();
        t.write_segment(src, 0, b"PAYLOAD!!!!!!!!!").unwrap();
        t.write_segment(dst, 0, b"............XXXX").unwrap();
        let before = t.read_segment(dst, 0, 16).unwrap();

        let err = t
            .submit_transfer(
                999_999, // unknown batch
                &[TransferOp {
                    source: loc(src, 0, Tier::L2),
                    target_segment_id: dst,
                    target_offset: 0,
                    length: 8,
                }],
            )
            .unwrap_err();
        assert!(matches!(err, TransferError::UnknownBatch(999_999)));
        assert_eq!(t.read_segment(dst, 0, 16).unwrap(), before);

        let batch = t.allocate_batch_id(1).unwrap();
        let err = t
            .submit_transfer(
                batch,
                &[
                    TransferOp {
                        source: loc(src, 0, Tier::L2),
                        target_segment_id: dst,
                        target_offset: 0,
                        length: 4,
                    },
                    TransferOp {
                        source: loc(src, 4, Tier::L2),
                        target_segment_id: dst,
                        target_offset: 4,
                        length: 4,
                    },
                ],
            )
            .unwrap_err();
        assert!(matches!(err, TransferError::BatchFull));
        assert_eq!(t.read_segment(dst, 0, 16).unwrap(), before);
    }

    #[test]
    fn failed_submit_after_claim_releases_batch() {
        let t = TcpTransport::new();
        let src = t.open_segment("src", 8).unwrap();
        let dst = t.open_segment("dst", 8).unwrap();
        t.write_segment(src, 0, b"PAYLOAD!").unwrap();
        t.write_segment(dst, 0, b"........").unwrap();
        let before = t.read_segment(dst, 0, 8).unwrap();

        let batch = t.allocate_batch_id(1).unwrap();
        let err = t
            .submit_transfer(
                batch,
                &[TransferOp {
                    source: loc(src, 0, Tier::L2),
                    target_segment_id: dst,
                    target_offset: 4,
                    length: 8,
                }],
            )
            .unwrap_err();
        assert!(matches!(err, TransferError::OutOfRange { .. }));
        assert_eq!(t.read_segment(dst, 0, 8).unwrap(), before);
        // Failure after claim must clear in_flight so caller can free the batch.
        t.free_batch_id(batch).unwrap();
    }

    #[test]
    fn free_batch_rejects_in_flight_batch() {
        let t = TcpTransport::new();
        let batch = t.allocate_batch_id(1).unwrap();
        {
            let mut batches = t.batches.lock().unwrap();
            batches.get_mut(&batch).unwrap().in_flight = true;
        }
        let err = t.free_batch_id(batch).unwrap_err();
        assert!(matches!(err, TransferError::BatchInFlight(id) if id == batch));
        assert_eq!(t.batch_count(), 1);
        {
            let mut batches = t.batches.lock().unwrap();
            batches.get_mut(&batch).unwrap().in_flight = false;
        }
        t.free_batch_id(batch).unwrap();
    }

    #[test]
    fn submitted_batch_cannot_be_submitted_again() {
        let t = TcpTransport::new();
        let src = t.open_segment("src", 8).unwrap();
        let dst = t.open_segment("dst", 8).unwrap();
        t.write_segment(src, 0, b"AAAA").unwrap();
        t.write_segment(dst, 0, b"........").unwrap();

        let batch = t.allocate_batch_id(1).unwrap();
        t.submit_transfer(
            batch,
            &[TransferOp {
                source: loc(src, 0, Tier::L2),
                target_segment_id: dst,
                target_offset: 0,
                length: 4,
            }],
        )
        .unwrap();
        assert_eq!(&t.read_segment(dst, 0, 8).unwrap(), b"AAAA....");

        t.write_segment(src, 0, b"BBBB").unwrap();
        let err = t
            .submit_transfer(
                batch,
                &[TransferOp {
                    source: loc(src, 0, Tier::L2),
                    target_segment_id: dst,
                    target_offset: 0,
                    length: 4,
                }],
            )
            .unwrap_err();
        assert!(matches!(err, TransferError::BatchSubmitted(id) if id == batch));
        assert_eq!(&t.read_segment(dst, 0, 8).unwrap(), b"AAAA....");
        let st = t.get_transfer_status(batch, 0).unwrap();
        assert_eq!(st.state, TaskState::Done);
        assert_eq!(st.bytes_done, 4);
    }
}
